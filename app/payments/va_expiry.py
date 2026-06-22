"""Expire unfunded virtual accounts and notify hosts."""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FundingStatus, Giveaway, GiveawayStatus
from app.payments.money import format_ngn
from app.x_client import XClient
from app.x_exceptions import XClientError

logger = logging.getLogger(__name__)


def expire_unfunded_giveaways(db: Session, client: XClient) -> int:
    """Mark AWAITING_FUNDING giveaways past va_expires_at as expired; notify host."""
    now = datetime.now(timezone.utc)
    pending = db.execute(
        select(Giveaway).where(
            Giveaway.status == GiveawayStatus.AWAITING_FUNDING,
            Giveaway.funding_status == FundingStatus.AWAITING_PAYMENT,
            Giveaway.va_expires_at.isnot(None),
            Giveaway.va_expires_at <= now,
        )
    ).scalars().all()

    count = 0
    for giveaway in pending:
        giveaway.funding_status = FundingStatus.EXPIRED
        giveaway.status = GiveawayStatus.DRAFT
        db.commit()
        count += 1
        _notify_va_expired(client, giveaway)
    return count


def _notify_va_expired(client: XClient, giveaway: Giveaway) -> None:
    total = format_ngn(giveaway.amount_kobo or 0)
    msg = (
        f"Funding window expired for {giveaway.title}.\n"
        f"The virtual account for {total} is no longer active.\n"
        "Tweet @ us again to start a new giveaway."
    )
    tweet_id = giveaway.host_tweet_id or giveaway.conversation_id
    if tweet_id:
        try:
            client.create_reply(msg, in_reply_to_tweet_id=tweet_id)
        except XClientError as exc:
            logger.warning("VA expiry tweet failed: %s", exc)
    if giveaway.host_user_id:
        try:
            client.send_direct_message(
                giveaway.host_user_id,
                msg,
                dedup_key=f"va_expired:{giveaway.id}",
            )
        except XClientError as exc:
            logger.warning("VA expiry DM failed: %s", exc)