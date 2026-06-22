"""
Multi-turn conversation engine for deformed/incomplete giveaway requests.

Flow:
  mention "giveaway" → collect amount → winners → duration → create VA → reply funding instructions
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.command_parse import parse_start_command
from app.models import ConversationKind, ConversationSession, FundingStatus, Giveaway, GiveawayStatus
from app.payments.funding_service import funding_instructions_text, initiate_giveaway_funding, setup_giveaway_amounts
from app.payments.money import closes_at_from_duration, parse_duration_seconds, parse_ngn_to_kobo

logger = logging.getLogger(__name__)

GIVEAWAY_TRIGGER_RE = re.compile(r"\b(giveaway|start\s+giveaway|begin\s+giveaway)\b", re.I)
WINNERS_INLINE_RE = re.compile(r"winners?\s*:\s*(\d+)", re.I)


def _session_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=24)


def _load_draft(session: ConversationSession) -> dict:
    try:
        return json.loads(session.draft_json or "{}")
    except json.JSONDecodeError:
        return {}


def _save_draft(session: ConversationSession, draft: dict) -> None:
    session.draft_json = json.dumps(draft)


def get_active_session(db: Session, user_id: str, kind: ConversationKind) -> ConversationSession | None:
    return db.execute(
        select(ConversationSession)
        .where(
            ConversationSession.user_id == user_id,
            ConversationSession.kind == kind,
            ConversationSession.expires_at > datetime.now(timezone.utc),
        )
        .order_by(ConversationSession.updated_at.desc())
    ).scalars().first()


def _missing_fields(draft: dict) -> list[str]:
    missing = []
    if not draft.get("prize_pool_kobo") and not draft.get("amount_kobo"):
        missing.append("prize amount (e.g. ₦5000 or 50k)")
    if not draft.get("num_winners"):
        missing.append("number of winners (e.g. winners: 3)")
    if not draft.get("duration_seconds"):
        missing.append("entry duration (e.g. duration: 7 days or 48h)")
    return missing


def _apply_text_to_draft(draft: dict, text: str) -> None:
    amount = parse_ngn_to_kobo(text)
    if amount:
        draft["prize_pool_kobo"] = amount
        draft["amount_kobo"] = amount

    winners_match = WINNERS_INLINE_RE.search(text)
    if winners_match:
        draft["num_winners"] = int(winners_match.group(1))
    elif re.search(r"\b(\d+)\s*winners?\b", text, re.I):
        draft["num_winners"] = int(re.search(r"\b(\d+)\s*winners?\b", text, re.I).group(1))

    duration = parse_duration_seconds(text)
    if duration:
        draft["duration_seconds"] = duration

    if len(text) > 10 and not draft.get("title"):
        draft["title"] = text[:200]


def handle_giveaway_mention(
    db: Session,
    user_id: str,
    tweet_id: str,
    conversation_id: str,
    text: str,
    host_username: str | None,
) -> tuple[str | None, Giveaway | None]:
    parsed = parse_start_command(text)
    draft = {
        "title": parsed["title"],
        "prize_description": parsed.get("prize_description"),
        "num_winners": parsed.get("num_winners", 1),
        "host_tweet_id": tweet_id,
        "conversation_id": conversation_id,
        "host_username": host_username,
    }
    _apply_text_to_draft(draft, text)

    missing = _missing_fields(draft)
    if missing:
        session = get_active_session(db, user_id, ConversationKind.GIVEAWAY_SETUP)
        if session is None:
            session = ConversationSession(
                kind=ConversationKind.GIVEAWAY_SETUP,
                user_id=user_id,
                thread_tweet_id=tweet_id,
                state="collecting",
                expires_at=_session_expiry(),
            )
            db.add(session)
        _save_draft(session, draft)
        session.state = "collecting"
        session.thread_tweet_id = tweet_id
        session.expires_at = _session_expiry()
        db.commit()
        return (
            "Giveaway initiated — I need a few details:\n"
            + "\n".join(f"• {m}" for m in missing)
            + "\n\nReply to this thread with the missing info.",
            None,
        )

    return _finalize_giveaway(db, user_id, draft)


def continue_giveaway_session(
    db: Session,
    user_id: str,
    tweet_id: str,
    text: str,
) -> tuple[str | None, Giveaway | None]:
    session = get_active_session(db, user_id, ConversationKind.GIVEAWAY_SETUP)
    if not session:
        return None, None

    draft = _load_draft(session)
    _apply_text_to_draft(draft, text)

    missing = _missing_fields(draft)
    if missing:
        _save_draft(session, draft)
        db.commit()
        return "Still need:\n" + "\n".join(f"• {m}" for m in missing), None

    db.delete(session)
    db.commit()
    return _finalize_giveaway(db, user_id, draft)


def _finalize_giveaway(db: Session, user_id: str, draft: dict) -> tuple[str, Giveaway]:
    prize_pool = int(draft.get("prize_pool_kobo") or draft.get("amount_kobo"))
    duration_seconds = int(draft["duration_seconds"])
    closes_at = closes_at_from_duration(duration_seconds)

    giveaway = Giveaway(
        host_tweet_id=draft.get("host_tweet_id"),
        host_user_id=user_id,
        host_username=draft.get("host_username"),
        conversation_id=draft.get("conversation_id"),
        title=draft.get("title") or "Giveaway",
        prize_description=draft.get("prize_description"),
        num_winners=int(draft.get("num_winners") or 1),
        closes_at=closes_at,
        status=GiveawayStatus.DRAFT,
    )
    setup_giveaway_amounts(db, giveaway, prize_pool)
    db.add(giveaway)
    db.commit()

    restructure_from = draft.get("restructure_from")
    if restructure_from:
        old = db.get(Giveaway, restructure_from)
        if old:
            old.status = GiveawayStatus.DRAFT
            old.funding_status = FundingStatus.EXPIRED
            db.commit()

    initiate_giveaway_funding(db, giveaway)
    return funding_instructions_text(giveaway, db), giveaway