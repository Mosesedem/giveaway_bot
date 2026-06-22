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
from urllib.parse import urlencode

from fastapi import FastAPI, Depends, Request, Form, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app import runtime
from app.auth import require_dashboard_auth
from app.db import get_db, init_db
from app.models import Giveaway, GiveawayStatus, Entry, Winner, WinnerStatus, Cursor, ProcessedTweet
from app.scheduler import start_scheduler, stop_scheduler, get_client
from app.bot_logic import collect_entries, pick_winners, notify_winner, revalidate_entries
from app.dm_queue import enqueue_winner_dm, enqueue_all_selected, pending_count
from app.entry_validation import validation_config, validation_rules_enabled
from app.x_client import x_credentials_configured, missing_credential_keys
from app.x_exceptions import XClientError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="app/templates")


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
def overview(request: Request, db: Session = Depends(get_db), _: None = Depends(require_dashboard_auth)):
    giveaways = db.execute(
        select(Giveaway).order_by(Giveaway.created_at.desc()).limit(10)
    ).scalars().all()
    giveaways = [_giveaway_with_counts(db, g) for g in giveaways]

    active_count = db.execute(
        select(func.count()).select_from(Giveaway).where(Giveaway.status == GiveawayStatus.ACTIVE)
    ).scalar_one()
    total_entries = db.execute(select(func.count()).select_from(Entry)).scalar_one()
    pending_dm = db.execute(
        select(func.count()).select_from(Winner).where(Winner.status == WinnerStatus.SELECTED)
    ).scalar_one()

    stats = {
        "active": active_count,
        "total_entries": total_entries,
        "pending_dm": pending_dm,
        "dm_queue_pending": pending_count(db),
        "last_cycle": runtime.last_cycle_at.strftime("%H:%M:%S UTC") if runtime.last_cycle_at else None,
        "last_cycle_error": runtime.last_cycle_error,
        "last_cycle_summary": runtime.last_cycle_summary,
        "x_configured": x_credentials_configured(),
        "validation_enabled": validation_rules_enabled(),
    }

    return templates.TemplateResponse(
        request,
        "overview.html",
        {"active": "overview", "giveaways": giveaways, "stats": stats},
    )


@app.get("/giveaways")
def giveaways_list(request: Request, db: Session = Depends(get_db), _: None = Depends(require_dashboard_auth)):
    giveaways = db.execute(select(Giveaway).order_by(Giveaway.created_at.desc())).scalars().all()
    giveaways = [_giveaway_with_counts(db, g) for g in giveaways]
    return templates.TemplateResponse(
        request, "giveaways_list.html", {"active": "giveaways", "giveaways": giveaways}
    )


@app.get("/giveaways/new")
def giveaway_new_form(request: Request, _: None = Depends(require_dashboard_auth)):
    return templates.TemplateResponse(
        request, "giveaway_new.html", {"active": "giveaways"}
    )


@app.post("/giveaways/new")
def giveaway_create(
    title: str = Form(...),
    prize_description: str = Form(""),
    conversation_id: str = Form(""),
    host_user_id: str = Form(""),
    num_winners: int = Form(1),
    db: Session = Depends(get_db),
    _: None = Depends(require_dashboard_auth),
):
    giveaway = Giveaway(
        title=title,
        prize_description=prize_description or None,
        conversation_id=conversation_id or None,
        host_user_id=host_user_id or None,
        host_tweet_id=conversation_id or None,
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
    _: None = Depends(require_dashboard_auth),
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
    dm_queue_pending = pending_count(db, giveaway_id)

    return templates.TemplateResponse(
        request,
        "giveaway_detail.html",
        {
            "active": "giveaways",
            "giveaway": giveaway,
            "entries": entries,
            "winners": winners,
            "dm_queue_pending": dm_queue_pending,
            "validation_rules": validation_config(),
            "flash": flash,
            "flash_type": flash_type,
        },
    )


def _redirect_with_flash(giveaway_id: str, message: str, flash_type: str = "success") -> RedirectResponse:
    qs = urlencode({"flash": message, "flash_type": flash_type})
    return RedirectResponse(f"/giveaways/{giveaway_id}?{qs}", status_code=303)


@app.post("/giveaways/{giveaway_id}/collect")
def giveaway_collect(giveaway_id: str, db: Session = Depends(get_db), _: None = Depends(require_dashboard_auth)):
    giveaway = db.get(Giveaway, giveaway_id)
    if not giveaway:
        raise HTTPException(404, "Giveaway not found")
    if not giveaway.conversation_id:
        return _redirect_with_flash(
            giveaway_id,
            "No conversation ID on this giveaway — add the host tweet/thread ID first.",
            "error",
        )
    try:
        client = get_client()
        added = collect_entries(db, client, giveaway)
    except ValueError as e:
        return _redirect_with_flash(giveaway_id, str(e), "error")
    except XClientError as e:
        return _redirect_with_flash(giveaway_id, f"Couldn't reach X: {e}", "error")
    return _redirect_with_flash(giveaway_id, f"Collected {added} new entr{'y' if added == 1 else 'ies'}.")


@app.post("/giveaways/{giveaway_id}/revalidate")
def giveaway_revalidate(giveaway_id: str, db: Session = Depends(get_db), _: None = Depends(require_dashboard_auth)):
    giveaway = db.get(Giveaway, giveaway_id)
    if not giveaway:
        raise HTTPException(404, "Giveaway not found")
    if not validation_rules_enabled():
        return _redirect_with_flash(giveaway_id, "No validation rules enabled in .env.", "error")
    try:
        client = get_client()
        valid, invalid = revalidate_entries(db, client, giveaway)
    except ValueError as e:
        return _redirect_with_flash(giveaway_id, str(e), "error")
    except XClientError as e:
        return _redirect_with_flash(giveaway_id, f"Couldn't reach X: {e}", "error")
    return _redirect_with_flash(
        giveaway_id,
        f"Revalidated: {valid} valid, {invalid} invalid.",
    )


@app.post("/giveaways/{giveaway_id}/pick-winners")
def giveaway_pick_winners(giveaway_id: str, db: Session = Depends(get_db), _: None = Depends(require_dashboard_auth)):
    giveaway = db.get(Giveaway, giveaway_id)
    if not giveaway:
        raise HTTPException(404, "Giveaway not found")
    winners = pick_winners(db, giveaway)
    if not winners:
        return _redirect_with_flash(giveaway_id, "No eligible entries to pick from.", "error")
    return _redirect_with_flash(giveaway_id, f"Selected {len(winners)} winner(s).")


@app.post("/giveaways/{giveaway_id}/winners/{winner_id}/notify")
def winner_notify(
    giveaway_id: str,
    winner_id: str,
    message: str = Form(...),
    queue: str = Form("false"),
    db: Session = Depends(get_db),
    _: None = Depends(require_dashboard_auth),
):
    giveaway = db.get(Giveaway, giveaway_id)
    winner = db.get(Winner, winner_id)
    if not giveaway or not winner:
        raise HTTPException(404, "Not found")

    if queue.lower() == "true":
        item = enqueue_winner_dm(db, winner, giveaway, message)
        if item:
            return _redirect_with_flash(giveaway_id, f"DM queued for {winner.user_id}.")
        return _redirect_with_flash(giveaway_id, f"DM already queued for {winner.user_id}.", "error")

    try:
        client = get_client()
        ok = notify_winner(client, giveaway, winner, db, message)
    except ValueError as e:
        return _redirect_with_flash(giveaway_id, str(e), "error")
    if ok:
        return _redirect_with_flash(giveaway_id, f"DM sent to winner {winner.user_id}.")
    return _redirect_with_flash(giveaway_id, f"DM failed for {winner.user_id} — see status badge.", "error")


@app.post("/giveaways/{giveaway_id}/queue-all-dms")
def giveaway_queue_all_dms(
    giveaway_id: str,
    message: str = Form(...),
    db: Session = Depends(get_db),
    _: None = Depends(require_dashboard_auth),
):
    giveaway = db.get(Giveaway, giveaway_id)
    if not giveaway:
        raise HTTPException(404, "Giveaway not found")
    queued, skipped = enqueue_all_selected(db, giveaway, message)
    if queued == 0:
        return _redirect_with_flash(giveaway_id, "No winners to queue (or all already queued).", "error")
    msg = f"Queued {queued} DM(s)"
    if skipped:
        msg += f" ({skipped} already in queue)"
    msg += ". The scheduler sends them in batches."
    return _redirect_with_flash(giveaway_id, msg)


@app.get("/logs")
def logs(request: Request, db: Session = Depends(get_db), _: None = Depends(require_dashboard_auth)):
    cursors = db.execute(select(Cursor)).scalars().all()
    processed = db.execute(
        select(ProcessedTweet).order_by(ProcessedTweet.processed_at.desc()).limit(50)
    ).scalars().all()
    return templates.TemplateResponse(
        request, "logs.html", {"active": "logs", "cursors": cursors, "processed": processed}
    )


@app.get("/health")
def health(db: Session = Depends(get_db)):
    """Render's health check hits this — keep it dependency-free and fast."""
    return {
        "status": "ok",
        "x_configured": x_credentials_configured(),
        "scheduler_enabled": os.getenv("ENABLE_SCHEDULER", "true").lower() == "true",
        "last_cycle_at": runtime.last_cycle_at.isoformat() if runtime.last_cycle_at else None,
        "last_cycle_error": runtime.last_cycle_error,
        "last_cycle_summary": runtime.last_cycle_summary,
        "dm_queue_pending": pending_count(db),
        "validation_enabled": validation_rules_enabled(),
    }


@app.get("/internal/wake")
def internal_wake(request: Request, db: Session = Depends(get_db)):
    """
    Keep-alive endpoint for Render cron (or any external pinger).

    Pings this every few minutes to prevent cold sleeps on free/starter tiers.
    Set CRON_WAKE_SECRET and pass Authorization: Bearer <secret>.
    """
    secret = os.getenv("CRON_WAKE_SECRET", "")
    if secret:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {secret}":
            raise HTTPException(401, "Invalid wake secret")

    interval = int(os.getenv("BOT_CYCLE_SECONDS", "90"))
    if os.getenv("ENABLE_SCHEDULER", "true").lower() == "true":
        start_scheduler(interval_seconds=interval)

    return {
        "awake": True,
        "scheduler_running": os.getenv("ENABLE_SCHEDULER", "true").lower() == "true",
        "last_cycle_at": runtime.last_cycle_at.isoformat() if runtime.last_cycle_at else None,
        "dm_queue_pending": pending_count(db),
    }


@app.get("/health/x")
def health_x():
    """Live X API connectivity check — use before your first real giveaway."""
    if not x_credentials_configured():
        raise HTTPException(
            503,
            detail={"ok": False, "missing": missing_credential_keys()},
        )
    try:
        identity = get_client().get_bot_identity()
        return {"ok": True, "username": identity["username"], "user_id": identity["user_id"]}
    except Exception as e:
        raise HTTPException(503, detail={"ok": False, "error": str(e)}) from e
