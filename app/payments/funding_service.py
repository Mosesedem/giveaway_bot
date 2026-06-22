"""Host funding flow: fee-inclusive VA per giveaway, webhook activation."""

import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FundingStatus, Giveaway, GiveawayStatus, PaymentEvent
from app.payments.exceptions import PaymentError
from app.payments.funding_provider import create_funding_va
from app.payments.money import format_ngn
from app.payments.settings import compute_transaction_fee_kobo, fee_config

logger = logging.getLogger(__name__)


def _webhook_base() -> str:
    return os.getenv("PUBLIC_BASE_URL", "http://localhost:6768").rstrip("/")


def setup_giveaway_amounts(db: Session, giveaway: Giveaway, prize_pool_kobo: int) -> None:
    """Split host prize from platform fee; amount_kobo = total transfer due."""
    giveaway.prize_pool_kobo = prize_pool_kobo
    giveaway.transaction_fee_kobo = compute_transaction_fee_kobo(prize_pool_kobo, db)
    giveaway.amount_kobo = prize_pool_kobo + giveaway.transaction_fee_kobo


def initiate_giveaway_funding(db: Session, giveaway: Giveaway) -> Giveaway:
    if not giveaway.prize_pool_kobo and giveaway.amount_kobo:
        setup_giveaway_amounts(db, giveaway, giveaway.amount_kobo)
    if not giveaway.amount_kobo:
        raise PaymentError("Giveaway amount is required before funding")

    external_ref = f"gw-{giveaway.id}"
    valid_for = int(os.getenv("VA_VALID_FOR_SECONDS", "86400"))
    callback = f"{_webhook_base()}/webhooks/safehaven/virtual-account"

    va, provider_name = create_funding_va(
        amount_kobo=giveaway.amount_kobo,
        external_reference=external_ref,
        callback_url=callback,
        valid_for_seconds=valid_for,
        giveaway_id=giveaway.id,
    )

    giveaway.status = GiveawayStatus.AWAITING_FUNDING
    giveaway.funding_status = FundingStatus.AWAITING_PAYMENT
    giveaway.payment_provider = provider_name
    giveaway.va_provider_id = va.provider_id
    giveaway.va_account_number = va.account_number
    giveaway.va_bank_name = va.bank_name
    giveaway.va_account_name = va.account_name
    giveaway.va_external_reference = va.external_reference
    giveaway.va_expires_at = datetime.now(timezone.utc) + timedelta(seconds=va.expires_in_seconds)
    db.commit()
    logger.info("VA created for giveaway %s via %s", giveaway.id, provider_name)
    return giveaway


def funding_instructions_text(giveaway: Giveaway, db: Session | None = None) -> str:
    prize = format_ngn(giveaway.prize_pool_kobo or 0)
    fee = format_ngn(giveaway.transaction_fee_kobo or 0)
    total = format_ngn(giveaway.amount_kobo or 0)
    fee_note = f" ({fee_config(db).describe()})" if db else ""
    duration_note = ""
    if giveaway.closes_at:
        duration_note = f"\nEntries close: {giveaway.closes_at.strftime('%d %b %Y %H:%M UTC')}"
    return (
        f"Giveaway initiated: {giveaway.title}\n\n"
        f"Prize pool: {prize} + fee {fee}{fee_note} = {total} total\n\n"
        f"Kindly transfer {total} to this giveaway account:\n"
        f"Bank: {giveaway.va_bank_name}\n"
        f"Account: {giveaway.va_account_number}\n"
        f"Name: {giveaway.va_account_name}\n\n"
        f"Winners: {giveaway.num_winners}.{duration_note}\n"
        "Entries open after payment is confirmed."
    )


def payment_already_processed(db: Session, payment_reference: str) -> bool:
    if not payment_reference:
        return False
    existing = db.execute(
        select(PaymentEvent).where(PaymentEvent.payment_reference == payment_reference)
    ).scalar_one_or_none()
    return existing is not None


def record_funding_event(
    db: Session,
    giveaway: Giveaway,
    provider: str,
    event_type: str,
    payload: dict,
    payment_reference: str | None = None,
) -> PaymentEvent | None:
    ref = payment_reference or payload.get("payment_reference")
    if ref and payment_already_processed(db, ref):
        logger.info("Skipping duplicate payment reference %s", ref)
        return None
    event = PaymentEvent(
        provider=provider,
        event_type=event_type,
        external_reference=giveaway.va_external_reference,
        payment_reference=ref,
        giveaway_id=giveaway.id,
        amount_kobo=payload.get("amount_kobo"),
        raw_json=str(payload),
    )
    db.add(event)
    return event


def activate_giveaway(db: Session, giveaway: Giveaway, prize_pool_kobo: int) -> None:
    """Mark giveaway live and bind payout source to its VA pool."""
    giveaway.prize_pool_kobo = prize_pool_kobo
    giveaway.payout_source_account = giveaway.va_account_number
    giveaway.funding_status = FundingStatus.FUNDED
    giveaway.status = GiveawayStatus.ACTIVE
    giveaway.funded_at = datetime.now(timezone.utc)
    db.commit()


def apply_funding_credit(
    db: Session,
    giveaway: Giveaway,
    amount_kobo: int,
    payment_reference: str,
) -> str:
    """
    Process webhook credit. Returns action: 'activated' or 'mismatch'.
    Caller must skip when record_funding_event returns None (duplicate).
    """
    giveaway.amount_received_kobo = amount_kobo
    expected = giveaway.amount_kobo or 0

    if amount_kobo == expected:
        pool = giveaway.prize_pool_kobo or max(0, amount_kobo - (giveaway.transaction_fee_kobo or 0))
        activate_giveaway(db, giveaway, pool)
        return "activated"

    if amount_kobo < expected:
        giveaway.funding_status = FundingStatus.UNDERPAID
    else:
        giveaway.funding_status = FundingStatus.OVERPAID
    db.commit()
    return "mismatch"


def funding_receipt_text(giveaway: Giveaway, payment_reference: str, amount_kobo: int) -> str:
    pool = giveaway.prize_pool_kobo or 0
    per = pool // max(1, giveaway.num_winners)
    closes = ""
    if giveaway.closes_at:
        closes = f"\nEntries close: {giveaway.closes_at.strftime('%d %b %Y %H:%M UTC')}"
    return (
        f"Payment received — {format_ngn(amount_kobo)}\n"
        f"Ref: {payment_reference}\n"
        f"Prize pool: {format_ngn(pool)} ({format_ngn(per)} × {giveaway.num_winners} winners)\n"
        f"Giveaway is now LIVE. Reply to this thread to enter!{closes}"
    )