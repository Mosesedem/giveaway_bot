"""Winner DM conversation: collect bank → verify → confirm → pay."""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ConversationKind, ConversationSession, Giveaway, PayoutStatus, Winner
from app.payments.payout_service import (
    apply_payout_details,
    execute_winner_payout,
    parse_bank_from_text,
    resolve_bank_code,
)


logger = logging.getLogger(__name__)


def get_winner_payout_session(db: Session, user_id: str) -> ConversationSession | None:
    return db.execute(
        select(ConversationSession).where(
            ConversationSession.user_id == user_id,
            ConversationSession.kind == ConversationKind.WINNER_PAYOUT,
        )
    ).scalars().first()


def handle_winner_dm(db: Session, user_id: str, text: str) -> str | None:
    session = get_winner_payout_session(db, user_id)
    if not session or not session.related_winner_id:
        return None

    winner = db.get(Winner, session.related_winner_id)
    giveaway = db.get(Giveaway, session.related_giveaway_id) if session.related_giveaway_id else None
    if not winner or not giveaway:
        return None

    normalized = text.strip()
    if normalized.upper() in {"YES", "CONFIRM", "OK", "PROCEED"} and winner.payout_status == PayoutStatus.READY:
        try:
            return execute_winner_payout(db, winner, giveaway)
        except Exception as exc:
            return f"Payout failed: {exc}. Our team will follow up."

    bank_code, account_number = parse_bank_from_text(normalized)
    if account_number:
        if not bank_code:
            # try to find bank name in text
            for token in normalized.split():
                code = resolve_bank_code(token)
                if code:
                    bank_code = code
                    break
        if bank_code:
            return apply_payout_details(db, winner, bank_code, account_number, bank_name=None)

    return (
        "Please send:\nBank: GTBank\nAccount: 0123456789\n\n"
        "Or reply YES after verification to receive payment."
    )


def open_payout_session(db: Session, winner: Winner, giveaway: Giveaway) -> ConversationSession:
    existing = get_winner_payout_session(db, winner.user_id)
    if existing:
        return existing
    from datetime import datetime, timedelta, timezone

    session = ConversationSession(
        kind=ConversationKind.WINNER_PAYOUT,
        user_id=winner.user_id,
        thread_tweet_id=winner.id,
        state="collecting_bank",
        related_giveaway_id=giveaway.id,
        related_winner_id=winner.id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    db.add(session)
    db.commit()
    return session