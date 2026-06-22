"""
Persistent DM queue with rate-limited background processing.

Dashboard actions enqueue DMs; the scheduler drains the queue in small
batches so large winner lists don't block on X rate limits.
"""

import logging
import os
import time
from datetime import datetime, timezone

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.models import DMQueueItem, DMQueueStatus, Winner, WinnerStatus, Giveaway
from app.x_client import XClient
from app.x_exceptions import XClientError, RateLimitExceeded, DirectMessageBlocked

logger = logging.getLogger(__name__)


def _batch_size() -> int:
    return max(1, int(os.getenv("DM_BATCH_SIZE", "3") or "3"))


def _interval_seconds() -> float:
    return max(1.0, float(os.getenv("DM_INTERVAL_SECONDS", "15") or "15"))


def _max_attempts() -> int:
    return max(1, int(os.getenv("DM_MAX_ATTEMPTS", "5") or "5"))


def enqueue_winner_dm(
    db: Session,
    winner: Winner,
    giveaway: Giveaway,
    message: str,
) -> DMQueueItem | None:
    """Add a winner DM to the queue. Returns None if an active item already exists."""
    existing = db.execute(
        select(DMQueueItem).where(
            DMQueueItem.winner_id == winner.id,
            DMQueueItem.status.in_([DMQueueStatus.PENDING, DMQueueStatus.PROCESSING]),
        )
    ).scalar_one_or_none()
    if existing:
        return None

    item = DMQueueItem(
        winner_id=winner.id,
        giveaway_id=giveaway.id,
        user_id=winner.user_id,
        message=message,
        status=DMQueueStatus.PENDING,
        max_attempts=_max_attempts(),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def enqueue_all_selected(
    db: Session,
    giveaway: Giveaway,
    message: str,
) -> tuple[int, int]:
    """Queue DMs for all winners still in 'selected' or retryable 'dm_failed' state."""
    winners = db.execute(
        select(Winner).where(
            Winner.giveaway_id == giveaway.id,
            Winner.status.in_([WinnerStatus.SELECTED, WinnerStatus.DM_FAILED]),
        )
    ).scalars().all()

    queued = 0
    skipped = 0
    for winner in winners:
        if enqueue_winner_dm(db, winner, giveaway, message):
            queued += 1
        else:
            skipped += 1
    return queued, skipped


def pending_count(db: Session, giveaway_id: str | None = None) -> int:
    stmt = select(func.count()).select_from(DMQueueItem).where(
        DMQueueItem.status == DMQueueStatus.PENDING
    )
    if giveaway_id:
        stmt = stmt.where(DMQueueItem.giveaway_id == giveaway_id)
    return db.execute(stmt).scalar_one()


def _mark_winner_notified(db: Session, winner: Winner) -> None:
    winner.status = WinnerStatus.NOTIFIED
    winner.notified_at = datetime.now(timezone.utc)
    db.commit()


def _mark_winner_failed(db: Session, winner: Winner, error: str) -> None:
    winner.status = WinnerStatus.DM_FAILED
    winner.notes = error
    db.commit()


def _process_item(db: Session, client: XClient, item: DMQueueItem) -> None:
    winner = db.get(Winner, item.winner_id)
    giveaway = db.get(Giveaway, item.giveaway_id)
    if not winner or not giveaway:
        item.status = DMQueueStatus.FAILED
        item.last_error = "winner or giveaway no longer exists"
        item.processed_at = datetime.now(timezone.utc)
        db.commit()
        return

    item.status = DMQueueStatus.PROCESSING
    item.attempts += 1
    db.commit()

    dedup_key = f"winner_notice:{giveaway.id}:{winner.user_id}"
    try:
        client.send_direct_message(winner.user_id, item.message, dedup_key=dedup_key)
        item.status = DMQueueStatus.SENT
        item.processed_at = datetime.now(timezone.utc)
        item.last_error = None
        _mark_winner_notified(db, winner)
        db.commit()
        logger.info(f"Queued DM sent to {winner.user_id} (queue item {item.id})")
    except RateLimitExceeded as exc:
        item.status = DMQueueStatus.PENDING
        item.last_error = str(exc)
        db.commit()
        logger.warning(f"DM queue rate limited for {winner.user_id}, will retry")
        raise
    except DirectMessageBlocked as exc:
        item.status = DMQueueStatus.FAILED
        item.last_error = str(exc)
        item.processed_at = datetime.now(timezone.utc)
        _mark_winner_failed(db, winner, str(exc))
        db.commit()
    except XClientError as exc:
        if item.attempts >= item.max_attempts:
            item.status = DMQueueStatus.FAILED
            item.processed_at = datetime.now(timezone.utc)
            _mark_winner_failed(db, winner, str(exc))
        else:
            item.status = DMQueueStatus.PENDING
            item.last_error = str(exc)
        db.commit()
        logger.error(f"DM queue error for {winner.user_id}: {exc}")


def process_dm_queue(db: Session, client: XClient) -> dict:
    """Drain up to DM_BATCH_SIZE pending items, spacing sends by DM_INTERVAL_SECONDS."""
    batch_size = _batch_size()
    interval = _interval_seconds()

    pending = db.execute(
        select(DMQueueItem)
        .where(DMQueueItem.status == DMQueueStatus.PENDING)
        .order_by(DMQueueItem.created_at.asc())
        .limit(batch_size)
    ).scalars().all()

    sent = 0
    failed = 0
    deferred = 0

    for idx, item in enumerate(pending):
        try:
            _process_item(db, client, item)
            if item.status == DMQueueStatus.SENT:
                sent += 1
            elif item.status == DMQueueStatus.FAILED:
                failed += 1
            elif item.status == DMQueueStatus.PENDING:
                deferred += 1
        except RateLimitExceeded:
            deferred += len(pending) - idx
            break

        if idx < len(pending) - 1:
            time.sleep(interval)

    return {
        "dm_queue_processed": len(pending),
        "dm_queue_sent": sent,
        "dm_queue_failed": failed,
        "dm_queue_deferred": deferred,
        "dm_queue_pending": pending_count(db),
    }