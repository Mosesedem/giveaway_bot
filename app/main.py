"""
FastAPI app: admin dashboard + background bot scheduler in one process.

Run locally with:
    ./run.sh
    # or: source venv/bin/activate && python -m uvicorn app.main:app --reload

The scheduler starts automatically on app startup and runs the bot loop
every BOT_CYCLE_SECONDS (default 90s). Dashboard routes use the same DB
session as the bot, so what you see is always current.
"""

import os
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from urllib.parse import urlencode

from fastapi import FastAPI, Depends, Request, Form, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.db import get_db, init_db, SessionLocal
from app.models import Giveaway, GiveawayStatus, Entry, Winner, Cursor, ProcessedTweet
from app.scheduler import start_scheduler, stop_scheduler, get_client
from app.bot_logic import collect_entries, pick_winners, notify_winner
from app.x_exceptions import XClientError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="app/templates")

LAST_CYCLE_AT: datetime | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if os.getenv("ENABLE_SCHEDULER", "true").lower() == "true":
        interval = int(os.getenv("BOT_CYCLE_SECONDS", "90"))
        start_scheduler(interval_seconds=interval)
    yield
    stop_scheduler()


app = FastAPI(title="Giveaway Bot Console", lifespan=lifespan)


def _giveaway_with_counts(db: Session, g: Giveaway):
    """Attach entry_count/winner_count attrs for template convenience."""
    g.entry_count = db.execute(
        select(func.count()).select_from(Entry).where(Entry.giveaway_id == g.id)
    ).scalar_one()
    g.winner_count = db.execute(
        select(func.count()).select_from(Winner).where(Winner.giveaway_id == g.id)
    ).scalar_one()
    return g


@app.get("/")
def overview(request: Request, db: Session = Depends(get_db)):
    giveaways = db.execute(
        select(Giveaway).order_by(Giveaway.created_at.desc()).limit(10)
    ).scalars().all()
    giveaways = [_giveaway_with_counts(db, g) for g in giveaways]

    active_count = db.execute(
        select(func.count()).select_from(Giveaway).where(Giveaway.status == GiveawayStatus.ACTIVE)
    ).scalar_one()
    total_entries = db.execute(select(func.count()).select_from(Entry)).scalar_one()
    pending_dm = db.execute(
        select(func.count()).select_from(Winner).where(Winner.status == "selected")
    ).scalar_one()

    stats = {
        "active": active_count,
        "total_entries": total_entries,
        "pending_dm": pending_dm,
        "last_cycle": LAST_CYCLE_AT.strftime("%H:%M:%S") if LAST_CYCLE_AT else None,
    }

    return templates.TemplateResponse(
        request,
        "overview.html",
        {"active": "overview", "giveaways": giveaways, "stats": stats},
    )


@app.get("/giveaways")
def giveaways_list(request: Request, db: Session = Depends(get_db)):
    giveaways = db.execute(select(Giveaway).order_by(Giveaway.created_at.desc())).scalars().all()
    giveaways = [_giveaway_with_counts(db, g) for g in giveaways]
    return templates.TemplateResponse(
        request, "giveaways_list.html", {"active": "giveaways", "giveaways": giveaways}
    )


@app.get("/giveaways/new")
def giveaway_new_form(request: Request):
    return templates.TemplateResponse(
        request, "giveaway_new.html", {"active": "giveaways"}
    )


@app.post("/giveaways/new")
def giveaway_create(
    title: str = Form(...),
    prize_description: str = Form(""),
    conversation_id: str = Form(""),
    num_winners: int = Form(1),
    db: Session = Depends(get_db),
):
    giveaway = Giveaway(
        title=title,
        prize_description=prize_description or None,
        conversation_id=conversation_id or None,
        num_winners=max(1, num_winners),
        status=GiveawayStatus.ACTIVE,
    )
    db.add(giveaway)
    db.commit()
    return RedirectResponse(f"/giveaways/{giveaway.id}", status_code=303)


@app.get("/giveaways/{giveaway_id}")
def giveaway_detail(
    giveaway_id: str,
    request: Request,
    db: Session = Depends(get_db),
    flash: str | None = None,
    flash_type: str | None = None,
):
    giveaway = db.get(Giveaway, giveaway_id)
    if not giveaway:
        raise HTTPException(404, "Giveaway not found")

    entries = db.execute(
        select(Entry).where(Entry.giveaway_id == giveaway_id).order_by(Entry.created_at.desc())
    ).scalars().all()
    winners = db.execute(
        select(Winner).where(Winner.giveaway_id == giveaway_id).order_by(Winner.selected_at.desc())
    ).scalars().all()

    return templates.TemplateResponse(
        request,
        "giveaway_detail.html",
        {
            "active": "giveaways",
            "giveaway": giveaway,
            "entries": entries,
            "winners": winners,
            "flash": flash,
            "flash_type": flash_type,
        },
    )


def _redirect_with_flash(giveaway_id: str, message: str, flash_type: str = "success") -> RedirectResponse:
    qs = urlencode({"flash": message, "flash_type": flash_type})
    return RedirectResponse(f"/giveaways/{giveaway_id}?{qs}", status_code=303)


@app.post("/giveaways/{giveaway_id}/collect")
def giveaway_collect(giveaway_id: str, db: Session = Depends(get_db)):
    giveaway = db.get(Giveaway, giveaway_id)
    if not giveaway:
        raise HTTPException(404, "Giveaway not found")
    client = get_client()
    try:
        added = collect_entries(db, client, giveaway)
    except XClientError as e:
        return _redirect_with_flash(giveaway_id, f"Couldn't reach X: {e}", "error")
    return _redirect_with_flash(giveaway_id, f"Collected {added} new entr{'y' if added == 1 else 'ies'}.")


@app.post("/giveaways/{giveaway_id}/pick-winners")
def giveaway_pick_winners(giveaway_id: str, db: Session = Depends(get_db)):
    giveaway = db.get(Giveaway, giveaway_id)
    if not giveaway:
        raise HTTPException(404, "Giveaway not found")
    winners = pick_winners(db, giveaway)
    if not winners:
        return _redirect_with_flash(giveaway_id, "No eligible entries to pick from.", "error")
    return _redirect_with_flash(giveaway_id, f"Selected {len(winners)} winner(s).")


@app.post("/giveaways/{giveaway_id}/winners/{winner_id}/notify")
def winner_notify(
    giveaway_id: str, winner_id: str, message: str = Form(...), db: Session = Depends(get_db)
):
    giveaway = db.get(Giveaway, giveaway_id)
    winner = db.get(Winner, winner_id)
    if not giveaway or not winner:
        raise HTTPException(404, "Not found")
    client = get_client()
    ok = notify_winner(client, giveaway, winner, db, message)
    if ok:
        return _redirect_with_flash(giveaway_id, f"DM sent to winner {winner.user_id}.")
    return _redirect_with_flash(giveaway_id, f"DM failed for {winner.user_id} — see status badge.", "error")


@app.get("/logs")
def logs(request: Request, db: Session = Depends(get_db)):
    cursors = db.execute(select(Cursor)).scalars().all()
    processed = db.execute(
        select(ProcessedTweet).order_by(ProcessedTweet.processed_at.desc()).limit(50)
    ).scalars().all()
    return templates.TemplateResponse(
        request, "logs.html", {"active": "logs", "cursors": cursors, "processed": processed}
    )


@app.get("/health")
def health():
    """Render's health check hits this — keep it dependency-free and fast."""
    return {"status": "ok"}
