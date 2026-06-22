"""Host confirmation when funding amount does not match expected total."""

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ConversationKind, ConversationSession, FundingStatus, Giveaway, GiveawayStatus, RefundStatus
from app.payments.funding_service import activate_giveaway, funding_receipt_text
from app.payments.money import format_ngn, parse_duration_seconds, parse_ngn_to_kobo, prize_per_winner
from app.payments.refund_service import (
    collect_refund_bank_details,
    overpaid_excess_kobo,
    refund_amount_for_restructure,
    refund_collect_prompt,
)
from app.conversation.intake import get_active_session, _load_draft, _save_draft, _session_expiry

logger = logging.getLogger(__name__)

PROCEED_RE = re.compile(r"\b(proceed|continue|accept|go\s+ahead)\b", re.I)
RESTRUCTURE_RE = re.compile(r"\b(restructure|adjust|change|redo|new\s+va)\b", re.I)
WINNERS_RE = re.compile(r"winners?\s*:\s*(\d+)", re.I)


def mismatch_prompt(giveaway: Giveaway) -> str:
    received = giveaway.amount_received_kobo or 0
    expected = giveaway.amount_kobo or 0
    pool = effective_prize_pool_kobo(giveaway, received)
    per_winner = prize_per_winner(pool, giveaway.num_winners)
    status = "less" if received < expected else "more"
    return (
        f"Payment received ({format_ngn(received)}) is {status} than expected ({format_ngn(expected)}).\n\n"
        f"If you PROCEED, the prize pool becomes {format_ngn(pool)} "
        f"({format_ngn(per_winner)} per winner × {giveaway.num_winners}).\n\n"
        "Reply PROCEED to start the giveaway with this amount,\n"
        "or RESTRUCTURE to change winners/prize and get a new account."
    )


def effective_prize_pool_kobo(giveaway: Giveaway, received_kobo: int | None = None) -> int:
    received = received_kobo if received_kobo is not None else (giveaway.amount_received_kobo or 0)
    fee = giveaway.transaction_fee_kobo or 0
    return max(0, received - fee)


def _active_host_sessions(db: Session, user_id: str) -> list[ConversationSession]:
    return db.execute(
        select(ConversationSession)
        .where(
            ConversationSession.user_id == user_id,
            ConversationSession.kind == ConversationKind.HOST_FUNDING,
            ConversationSession.expires_at > datetime.now(timezone.utc),
        )
        .order_by(ConversationSession.updated_at.desc())
    ).scalars().all()


def _pick_host_session(sessions: list[ConversationSession]) -> ConversationSession | None:
    if not sessions:
        return None
    for state in ("refund_collecting", "restructure_after_refund", "awaiting_decision"):
        match = next((s for s in sessions if s.state == state), None)
        if match:
            return match
    return sessions[0]


def open_host_funding_session(db: Session, giveaway: Giveaway) -> ConversationSession:
    existing = db.execute(
        select(ConversationSession).where(
            ConversationSession.kind == ConversationKind.HOST_FUNDING,
            ConversationSession.related_giveaway_id == giveaway.id,
        )
    ).scalars().first()
    if existing:
        return existing

    for stale in _active_host_sessions(db, giveaway.host_user_id or ""):
        if stale.related_giveaway_id != giveaway.id:
            db.delete(stale)

    session = ConversationSession(
        kind=ConversationKind.HOST_FUNDING,
        user_id=giveaway.host_user_id or "",
        thread_tweet_id=giveaway.host_tweet_id or giveaway.conversation_id or "",
        state="awaiting_decision",
        related_giveaway_id=giveaway.id,
        draft_json=json.dumps({"received_kobo": giveaway.amount_received_kobo}),
        expires_at=datetime.now(timezone.utc) + timedelta(days=3),
    )
    db.add(session)
    giveaway.funding_status = FundingStatus.PENDING_HOST_CONFIRMATION
    giveaway.status = GiveawayStatus.AWAITING_FUNDING
    db.commit()
    return session


def _begin_restructure_intake(
    db: Session,
    user_id: str,
    giveaway: Giveaway,
    session: ConversationSession,
    text: str,
) -> tuple[str, None]:
    draft = {
        "prize_pool_kobo": parse_ngn_to_kobo(text) or giveaway.prize_pool_kobo,
        "num_winners": int(WINNERS_RE.search(text).group(1)) if WINNERS_RE.search(text) else giveaway.num_winners,
        "duration_seconds": parse_duration_seconds(text),
        "host_tweet_id": giveaway.host_tweet_id,
        "conversation_id": giveaway.conversation_id,
        "host_username": giveaway.host_username,
        "title": giveaway.title,
        "prize_description": giveaway.prize_description,
        "restructure_from": giveaway.id,
    }
    setup_session = get_active_session(db, user_id, ConversationKind.GIVEAWAY_SETUP)
    if setup_session is None:
        setup_session = ConversationSession(
            kind=ConversationKind.GIVEAWAY_SETUP,
            user_id=user_id,
            thread_tweet_id=session.thread_tweet_id,
            state="restructuring",
            expires_at=_session_expiry(),
        )
        db.add(setup_session)
    _save_draft(setup_session, draft)
    setup_session.state = "restructuring"
    db.delete(session)
    giveaway.funding_status = FundingStatus.AWAITING_PAYMENT
    db.commit()

    missing = []
    if not draft.get("prize_pool_kobo"):
        missing.append("new prize amount (e.g. ₦5000)")
    if not draft.get("duration_seconds"):
        missing.append("duration (e.g. duration: 7 days)")

    if missing:
        return (
            "Let's restructure. Still need:\n"
            + "\n".join(f"• {m}" for m in missing),
            None,
        )
    return (
        "Let's restructure. Reply with:\n"
        "• new prize amount (e.g. ₦5000)\n"
        "• winners: N\n"
        "• duration: 7 days\n\n"
        "I'll generate a fresh virtual account.",
        None,
    )


def handle_host_funding_reply(
    db: Session,
    user_id: str,
    text: str,
) -> tuple[str | None, Giveaway | None]:
    session = _pick_host_session(_active_host_sessions(db, user_id))
    if not session or not session.related_giveaway_id:
        return None, None

    giveaway = db.get(Giveaway, session.related_giveaway_id)
    if not giveaway:
        return None, None

    normalized = text.strip()

    session.updated_at = datetime.now(timezone.utc)

    if session.state == "refund_collecting":
        if giveaway.refund_status == RefundStatus.COMPLETED:
            return _begin_restructure_intake(db, user_id, giveaway, session, normalized)
        try:
            reply = collect_refund_bank_details(db, giveaway, normalized)
        except Exception as exc:
            return f"Refund error: {exc}. Please resend bank details.", None
        if giveaway.refund_status == RefundStatus.COMPLETED:
            session.state = "restructure_after_refund"
            _save_draft(session, {"restructure_pending": True})
            db.commit()
        return reply, None

    if session.state == "restructure_after_refund" or (
        giveaway.refund_status == RefundStatus.COMPLETED
        and RESTRUCTURE_RE.search(normalized)
    ):
        return _begin_restructure_intake(db, user_id, giveaway, session, normalized)

    if PROCEED_RE.search(normalized):
        pool = effective_prize_pool_kobo(giveaway)
        if pool <= 0:
            return "Cannot proceed — amount does not cover the transaction fee.", None
        activate_giveaway(db, giveaway, pool)
        db.delete(session)
        db.commit()
        ref = giveaway.va_external_reference or giveaway.id
        return funding_receipt_text(giveaway, ref, giveaway.amount_received_kobo or 0), giveaway

    if RESTRUCTURE_RE.search(normalized) or parse_ngn_to_kobo(normalized) or WINNERS_RE.search(normalized):
        refund_due = refund_amount_for_restructure(giveaway)
        if refund_due > 0 and giveaway.refund_status != RefundStatus.COMPLETED:
            session.state = "refund_collecting"
            giveaway.refund_amount_kobo = refund_due
            giveaway.refund_status = RefundStatus.COLLECTING_BANK
            _save_draft(session, {"refund_kobo": refund_due, "full_refund": overpaid_excess_kobo(giveaway) == 0})
            db.commit()
            return refund_collect_prompt(refund_due, full=overpaid_excess_kobo(giveaway) == 0), None
        return _begin_restructure_intake(db, user_id, giveaway, session, normalized)

    return mismatch_prompt(giveaway), None