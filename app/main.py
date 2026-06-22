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
from app.dm_queue import enqueue_winner_dm, enqueue_all_selected, pending_count, list_recent, retry_item, cancel_item
from app.entry_validation import validation_config, validation_rules_enabled
from app.giveaway_lifecycle import close_giveaway, complete_giveaway, is_collecting_entries
from app.bot_replies import bot_replies_enabled
from app.payments.webhooks import handle_safehaven_webhook, handle_paystack_charge_success
from app.payments.funding_service import funding_receipt_text
from app.payments.webhook_security import verify_paystack_signature, verify_safehaven_webhook
from app.bot_logic import notify_host_funding_mismatch
from app.payments.settings import (
    paystack_enabled,
    set_setting,
    fee_config,
    seed_default_settings,
    FEE_MODES,
)
from app.models import PaymentEvent, SystemSetting
from app.x_client import x_credentials_configured, missing_credential_keys
from app.x_exceptions import XClientError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="app/templates")


def _seed_settings(db: Session):
    seed_default_settings(db)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        _seed_settings(db)
    finally:
        db.close()
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
def giveaway_new_form(
    request: Request,
    flash: str | None = None,
    flash_type: str | None = None,
    _: None = Depends(require_dashboard_auth),
):
    return templates.TemplateResponse(
        request,
        "giveaway_new.html",
        {
            "active": "giveaways",
            "fintech_mode": os.getenv("FINTECH_MODE", "true").lower() == "true",
            "flash": flash,
            "flash_type": flash_type,
        },
    )


@app.post("/giveaways/new")
def giveaway_create(
    title: str = Form(...),
    prize_description: str = Form(""),
    conversation_id: str = Form(""),
    host_user_id: str = Form(""),
    num_winners: int = Form(1),
    prize_pool_ngn: float = Form(0),
    duration_hours: float = Form(0),
    db: Session = Depends(get_db),
    _: None = Depends(require_dashboard_auth),
):
    from app.payments.funding_service import initiate_giveaway_funding, setup_giveaway_amounts
    from app.payments.money import closes_at_from_duration

    fintech = os.getenv("FINTECH_MODE", "true").lower() == "true"
    if fintech:
        if prize_pool_ngn <= 0 or duration_hours <= 0:
            return RedirectResponse(
                "/giveaways/new?flash=Prize+amount+and+duration+are+required&flash_type=error",
                status_code=303,
            )
        prize_kobo = int(round(prize_pool_ngn * 100))
        closes_at = closes_at_from_duration(int(duration_hours * 3600))
        giveaway = Giveaway(
            title=title,
            prize_description=prize_description or None,
            conversation_id=conversation_id or None,
            host_user_id=host_user_id or None,
            host_tweet_id=conversation_id or None,
            num_winners=max(1, num_winners),
            closes_at=closes_at,
            status=GiveawayStatus.DRAFT,
        )
        setup_giveaway_amounts(db, giveaway, prize_kobo)
        db.add(giveaway)
        db.commit()
        try:
            initiate_giveaway_funding(db, giveaway)
        except Exception as exc:
            return RedirectResponse(
                f"/giveaways/new?flash=Funding+setup+failed:+{exc}&flash_type=error",
                status_code=303,
            )
    else:
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
    filter: str = "all",
    _: None = Depends(require_dashboard_auth),
):
    giveaway = db.get(Giveaway, giveaway_id)
    if not giveaway:
        raise HTTPException(404, "Giveaway not found")

    entry_query = select(Entry).where(Entry.giveaway_id == giveaway_id)
    if filter == "valid":
        entry_query = entry_query.where(Entry.is_valid.is_(True))
    elif filter == "invalid":
        entry_query = entry_query.where(Entry.is_valid.is_(False))
    entries = db.execute(entry_query.order_by(Entry.created_at.desc())).scalars().all()
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
            "entry_filter": filter,
            "bot_replies_enabled": bot_replies_enabled(),
            "accepting_entries": is_collecting_entries(giveaway),
            "default_dm_message": (
                f"Congrats! You won the giveaway: {giveaway.title}. "
                "Reply with your bank details or preferred payout method."
            ),
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
    if not is_collecting_entries(giveaway):
        return _redirect_with_flash(giveaway_id, "Giveaway is closed — not collecting entries.", "error")
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


@app.post("/giveaways/{giveaway_id}/close")
def giveaway_close(giveaway_id: str, db: Session = Depends(get_db), _: None = Depends(require_dashboard_auth)):
    giveaway = db.get(Giveaway, giveaway_id)
    if not giveaway:
        raise HTTPException(404, "Giveaway not found")
    close_giveaway(db, giveaway)
    return _redirect_with_flash(giveaway_id, "Giveaway closed — no more entries will be collected.")


@app.post("/giveaways/{giveaway_id}/complete")
def giveaway_complete(giveaway_id: str, db: Session = Depends(get_db), _: None = Depends(require_dashboard_auth)):
    giveaway = db.get(Giveaway, giveaway_id)
    if not giveaway:
        raise HTTPException(404, "Giveaway not found")
    complete_giveaway(db, giveaway)
    return _redirect_with_flash(giveaway_id, "Giveaway marked complete.")


@app.post("/giveaways/{giveaway_id}/pick-winners")
def giveaway_pick_winners(
    giveaway_id: str,
    seed: str = Form(""),
    db: Session = Depends(get_db),
    _: None = Depends(require_dashboard_auth),
):
    giveaway = db.get(Giveaway, giveaway_id)
    if not giveaway:
        raise HTTPException(404, "Giveaway not found")
    draw_seed = int(seed) if seed.strip().isdigit() else None
    try:
        client = get_client()
        winners = pick_winners(db, giveaway, seed=draw_seed, client=client)
    except ValueError as e:
        return _redirect_with_flash(giveaway_id, str(e), "error")
    if not winners:
        return _redirect_with_flash(giveaway_id, "No eligible entries to pick from.", "error")
    seed_note = f" (audit seed: {giveaway.pick_seed})" if giveaway.pick_seed is not None else ""
    return _redirect_with_flash(giveaway_id, f"Selected {len(winners)} winner(s){seed_note}.")


@app.post("/giveaways/{giveaway_id}/winners/{winner_id}/status")
def winner_set_status(
    giveaway_id: str,
    winner_id: str,
    status: str = Form(...),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    _: None = Depends(require_dashboard_auth),
):
    giveaway = db.get(Giveaway, giveaway_id)
    winner = db.get(Winner, winner_id)
    if not giveaway or not winner:
        raise HTTPException(404, "Not found")
    allowed = {WinnerStatus.CONFIRMED.value, WinnerStatus.PAID.value}
    if status not in allowed:
        return _redirect_with_flash(giveaway_id, f"Invalid status: {status}", "error")
    winner.status = WinnerStatus(status)
    if notes.strip():
        winner.notes = notes.strip()
    db.commit()
    return _redirect_with_flash(giveaway_id, f"Winner {winner.user_id} marked {status}.")


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


@app.get("/dm-queue")
def dm_queue_page(
    request: Request,
    db: Session = Depends(get_db),
    flash: str | None = None,
    flash_type: str | None = None,
    _: None = Depends(require_dashboard_auth),
):
    items = list_recent(db)
    return templates.TemplateResponse(
        request,
        "dm_queue.html",
        {
            "active": "dm_queue",
            "items": items,
            "pending_total": pending_count(db),
            "flash": flash,
            "flash_type": flash_type,
        },
    )


@app.post("/dm-queue/{item_id}/retry")
def dm_queue_retry(item_id: str, db: Session = Depends(get_db), _: None = Depends(require_dashboard_auth)):
    if retry_item(db, item_id):
        return RedirectResponse("/dm-queue?flash=Item+queued+for+retry&flash_type=success", status_code=303)
    return RedirectResponse("/dm-queue?flash=Could+not+retry+item&flash_type=error", status_code=303)


@app.post("/dm-queue/{item_id}/cancel")
def dm_queue_cancel(item_id: str, db: Session = Depends(get_db), _: None = Depends(require_dashboard_auth)):
    if cancel_item(db, item_id):
        return RedirectResponse("/dm-queue?flash=Queue+item+cancelled&flash_type=success", status_code=303)
    return RedirectResponse("/dm-queue?flash=Could+not+cancel+item&flash_type=error", status_code=303)


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


@app.post("/webhooks/safehaven/virtual-account")
async def webhook_safehaven_va(request: Request, db: Session = Depends(get_db)):
    verify_safehaven_webhook(request)
    payload = await request.json()
    giveaway, action = handle_safehaven_webhook(db, payload)
    if giveaway and action == "activated" and giveaway.host_tweet_id:
        data = payload.get("data") or payload
        amount_kobo = int(float(data.get("amount", 0)) * 100)
        ref = str(data.get("paymentReference") or giveaway.va_external_reference)
        receipt = funding_receipt_text(giveaway, ref, amount_kobo)
        try:
            get_client().create_reply(receipt, in_reply_to_tweet_id=giveaway.host_tweet_id)
        except Exception as exc:
            logger.warning("Could not post funding receipt: %s", exc)
    elif giveaway and action == "mismatch":
        try:
            notify_host_funding_mismatch(db, get_client(), giveaway)
        except Exception as exc:
            logger.warning("Could not notify host of funding mismatch: %s", exc)
    return {"ok": True, "giveaway_id": giveaway.id if giveaway else None, "action": action}


@app.post("/webhooks/paystack")
async def webhook_paystack(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    verify_paystack_signature(body, request.headers.get("x-paystack-signature"))
    import json
    payload = json.loads(body)
    event = payload.get("event", "")
    giveaway = None
    action = "ignored"
    if event in {"charge.success", "transfer.success"}:
        giveaway, action = handle_paystack_charge_success(db, payload)
    if giveaway and action == "activated" and giveaway.host_tweet_id:
        data = payload.get("data") or {}
        amount_kobo = int(data.get("amount") or 0)
        ref = str(data.get("reference") or giveaway.va_external_reference)
        receipt = funding_receipt_text(giveaway, ref, amount_kobo)
        try:
            get_client().create_reply(receipt, in_reply_to_tweet_id=giveaway.host_tweet_id)
        except Exception as exc:
            logger.warning("Could not post funding receipt: %s", exc)
    elif giveaway and action == "mismatch":
        try:
            notify_host_funding_mismatch(db, get_client(), giveaway)
        except Exception as exc:
            logger.warning("Could not notify host of funding mismatch: %s", exc)
    return {"ok": True, "action": action}


@app.get("/admin/settings")
def admin_settings(
    request: Request,
    db: Session = Depends(get_db),
    flash: str | None = None,
    flash_type: str | None = None,
    _: None = Depends(require_dashboard_auth),
):
    fees = fee_config(db)
    return templates.TemplateResponse(
        request,
        "admin_settings.html",
        {
            "active": "admin",
            "paystack_enabled": paystack_enabled(db),
            "fintech_mode": os.getenv("FINTECH_MODE", "true"),
            "safehaven_mock": os.getenv("SAFEHAVEN_MOCK", "false"),
            "paystack_mock": os.getenv("PAYSTACK_MOCK", "false"),
            "fee_mode": fees.mode,
            "fee_fixed_ngn": fees.fixed_kobo / 100,
            "fee_percent": fees.percent,
            "fee_modes": FEE_MODES,
            "flash": flash,
            "flash_type": flash_type,
        },
    )


@app.post("/admin/settings/paystack")
def admin_toggle_paystack(
    enabled: str = Form("false"),
    db: Session = Depends(get_db),
    _: None = Depends(require_dashboard_auth),
):
    set_setting(db, "paystack_enabled", "true" if enabled.lower() == "true" else "false")
    return RedirectResponse("/admin/settings?flash=Paystack+setting+updated&flash_type=success", status_code=303)


@app.post("/admin/settings/fees")
def admin_update_fees(
    fee_mode: str = Form(...),
    fee_fixed_ngn: float = Form(0),
    fee_percent: float = Form(0),
    db: Session = Depends(get_db),
    _: None = Depends(require_dashboard_auth),
):
    if fee_mode not in FEE_MODES:
        return RedirectResponse("/admin/settings?flash=Invalid+fee+mode&flash_type=error", status_code=303)
    fixed_kobo = max(0, int(round(fee_fixed_ngn * 100)))
    percent = max(0.0, float(fee_percent))
    set_setting(db, "transaction_fee_mode", fee_mode)
    set_setting(db, "transaction_fee_fixed_kobo", str(fixed_kobo))
    set_setting(db, "transaction_fee_percent", str(percent))
    return RedirectResponse("/admin/settings?flash=Transaction+fees+updated&flash_type=success", status_code=303)


@app.get("/payments")
def payments_log(request: Request, db: Session = Depends(get_db), _: None = Depends(require_dashboard_auth)):
    events = db.execute(
        select(PaymentEvent).order_by(PaymentEvent.created_at.desc()).limit(100)
    ).scalars().all()
    return templates.TemplateResponse(
        request,
        "payments.html",
        {"active": "payments", "events": events},
    )


@app.post("/giveaways/{giveaway_id}/refund-host")
def giveaway_refund_host(
    giveaway_id: str,
    bank_code: str = Form(...),
    account_number: str = Form(...),
    amount_ngn: float = Form(0),
    db: Session = Depends(get_db),
    _: None = Depends(require_dashboard_auth),
):
    from app.models import RefundStatus
    from app.payments.refund_service import process_host_refund_bank, refund_amount_for_restructure

    giveaway = db.get(Giveaway, giveaway_id)
    if not giveaway:
        raise HTTPException(404, "Giveaway not found")
    refund_kobo = int(round(amount_ngn * 100)) if amount_ngn > 0 else refund_amount_for_restructure(giveaway)
    if refund_kobo <= 0:
        return _redirect_with_flash(giveaway_id, "No refundable amount on this giveaway.", "error")
    giveaway.refund_amount_kobo = refund_kobo
    giveaway.refund_status = RefundStatus.COLLECTING_BANK
    db.commit()
    try:
        msg = process_host_refund_bank(db, giveaway, bank_code, account_number)
        return _redirect_with_flash(giveaway_id, msg)
    except Exception as exc:
        return _redirect_with_flash(giveaway_id, str(exc), "error")


@app.post("/giveaways/{giveaway_id}/winners/{winner_id}/payout")
def winner_payout_now(
    giveaway_id: str,
    winner_id: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_dashboard_auth),
):
    from app.payments.payout_service import execute_winner_payout

    giveaway = db.get(Giveaway, giveaway_id)
    winner = db.get(Winner, winner_id)
    if not giveaway or not winner:
        raise HTTPException(404, "Not found")
    try:
        msg = execute_winner_payout(db, winner, giveaway)
        return _redirect_with_flash(giveaway_id, msg)
    except Exception as exc:
        return _redirect_with_flash(giveaway_id, str(exc), "error")


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
