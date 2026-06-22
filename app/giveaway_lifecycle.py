"""Giveaway status transitions and collection eligibility."""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Giveaway, GiveawayStatus
from app.x_client import XClient
from app.x_exceptions import XClientError

logger = logging.getLogger(__name__)


def is_collecting_entries(giveaway: Giveaway) -> bool:
    if giveaway.status != GiveawayStatus.ACTIVE:
        return False
    if giveaway.closes_at:
        closes = giveaway.closes_at
        if closes.tzinfo is None:
            closes = closes.replace(tzinfo=timezone.utc)
        if closes <= datetime.now(timezone.utc):
            return False
    return True


def auto_close_expired(db: Session, giveaway: Giveaway) -> bool:
    """Close giveaway if closes_at has passed. Returns True if closed."""
    if giveaway.status != GiveawayStatus.ACTIVE or not giveaway.closes_at:
        return False
    closes = giveaway.closes_at
    if closes.tzinfo is None:
        closes = closes.replace(tzinfo=timezone.utc)
    if closes <= datetime.now(timezone.utc):
        giveaway.status = GiveawayStatus.CLOSED
        db.commit()
        return True
    return False


def close_giveaway(db: Session, giveaway: Giveaway) -> None:
    giveaway.status = GiveawayStatus.CLOSED
    if not giveaway.closes_at:
        giveaway.closes_at = datetime.now(timezone.utc)
    db.commit()


def complete_giveaway(db: Session, giveaway: Giveaway) -> None:
    giveaway.status = GiveawayStatus.COMPLETE
    db.commit()


def selection_ready_message(giveaway: Giveaway) -> str:
    return (
        f"Entries are now closed for {giveaway.title}.\n"
        f"Ready to pick {giveaway.num_winners} winner(s)! "
        "Use the dashboard to draw winners."
    )


def notify_selection_ready(db: Session, client: XClient, giveaway: Giveaway) -> bool:
    """Tweet + DM host once when entry period ends (closes_at passed)."""
    if giveaway.selection_notified_at:
        return False

    message = selection_ready_message(giveaway)
    tweet_id = giveaway.host_tweet_id or giveaway.conversation_id
    if tweet_id:
        try:
            client.create_reply(message, in_reply_to_tweet_id=tweet_id)
        except XClientError as exc:
            logger.warning("Selection-ready tweet failed for %s: %s", giveaway.id, exc)

    if giveaway.host_user_id:
        try:
            client.send_direct_message(
                giveaway.host_user_id,
                message,
                dedup_key=f"selection_ready:{giveaway.id}",
            )
        except XClientError as exc:
            logger.warning("Selection-ready DM failed for %s: %s", giveaway.id, exc)

    giveaway.selection_notified_at = datetime.now(timezone.utc)
    db.commit()
    return True