"""Giveaway status transitions and collection eligibility."""

import logging
import os
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Giveaway, GiveawayStatus
from app.x_client import XClient
from app.x_exceptions import XClientError

logger = logging.getLogger(__name__)


def auto_pick_enabled() -> bool:
    return os.getenv("AUTO_PICK_WINNERS", "true").lower() == "true"


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


def closes_at_passed(giveaway: Giveaway) -> bool:
    if not giveaway.closes_at:
        return False
    closes = giveaway.closes_at
    if closes.tzinfo is None:
        closes = closes.replace(tzinfo=timezone.utc)
    return closes <= datetime.now(timezone.utc)


def auto_close_expired(db: Session, giveaway: Giveaway) -> bool:
    """Close giveaway if closes_at has passed. Returns True if closed."""
    if giveaway.status != GiveawayStatus.ACTIVE or not giveaway.closes_at:
        return False
    if not closes_at_passed(giveaway):
        return False
    giveaway.status = GiveawayStatus.CLOSED
    db.commit()
    return True


def close_giveaway(db: Session, giveaway: Giveaway) -> None:
    giveaway.status = GiveawayStatus.CLOSED
    if not giveaway.closes_at:
        giveaway.closes_at = datetime.now(timezone.utc)
    db.commit()


def complete_giveaway(db: Session, giveaway: Giveaway) -> None:
    giveaway.status = GiveawayStatus.COMPLETE
    db.commit()


def winners_picked_message(giveaway: Giveaway, winner_count: int, seed: int | None) -> str:
    seed_note = f" (audit seed: {seed})" if seed is not None else ""
    return (
        f"Entries closed for {giveaway.title}.\n"
        f"Randomly selected {winner_count} winner(s){seed_note}. "
        "Winners will receive a DM shortly!"
    )


def selection_ready_message(giveaway: Giveaway) -> str:
    return (
        f"Entries are now closed for {giveaway.title}.\n"
        f"Ready to pick {giveaway.num_winners} winner(s)! "
        "Use the dashboard to draw winners."
    )


def notify_host_after_close(
    db: Session,
    client: XClient,
    giveaway: Giveaway,
    message: str,
) -> bool:
    if giveaway.selection_notified_at:
        return False
    tweet_id = giveaway.host_tweet_id or giveaway.conversation_id
    if tweet_id:
        try:
            client.create_reply(message, in_reply_to_tweet_id=tweet_id)
        except XClientError as exc:
            logger.warning("Post-close tweet failed for %s: %s", giveaway.id, exc)
    if giveaway.host_user_id:
        try:
            client.send_direct_message(
                giveaway.host_user_id,
                message,
                dedup_key=f"selection_ready:{giveaway.id}",
            )
        except XClientError as exc:
            logger.warning("Post-close DM failed for %s: %s", giveaway.id, exc)
    giveaway.selection_notified_at = datetime.now(timezone.utc)
    db.commit()
    return True


def auto_finalize_closed_giveaway(
    db: Session,
    client: XClient,
    giveaway: Giveaway,
    pick_fn,
    enqueue_dms_fn,
) -> dict:
    """
    After close: random winner pick + public announce + queue winner DMs.
    pick_fn(db, giveaway, client=client) -> list[Winner]
    enqueue_dms_fn(db, giveaway) -> int queued
    """
    if giveaway.selection_notified_at:
        return {"skipped": True}

    if not auto_pick_enabled():
        notify_host_after_close(db, client, giveaway, selection_ready_message(giveaway))
        return {"auto_pick": False}

    winners = pick_fn(db, giveaway, client=client)
    if not winners:
        notify_host_after_close(
            db,
            client,
            giveaway,
            f"Entries closed for {giveaway.title}, but no eligible entries were found.",
        )
        return {"auto_pick": True, "winners": 0}

    queued = enqueue_dms_fn(db, giveaway)
    msg = winners_picked_message(giveaway, len(winners), giveaway.pick_seed)
    notify_host_after_close(db, client, giveaway, msg)
    return {"auto_pick": True, "winners": len(winners), "dms_queued": queued}