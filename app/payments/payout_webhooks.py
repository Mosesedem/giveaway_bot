"""Webhook handlers for outbound payout and refund transfer confirmation."""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Giveaway, PayoutStatus, RefundStatus, Winner, WinnerStatus, PaymentEvent
from app.payments.funding_service import payment_already_processed

logger = logging.getLogger(__name__)

_PAYOUT_REF_PREFIXES = ("payout-", "refund-")
_SUCCESS_STATUSES = {"completed", "success", "approved"}
_FAILURE_STATUSES = {"failed", "reversed", "declined", "rejected", "cancelled", "canceled"}


def _is_payout_reference(reference: str) -> bool:
    ref = str(reference or "").lower()
    return any(ref.startswith(p) for p in _PAYOUT_REF_PREFIXES)


def _terminal_action(status: str) -> str | None:
    s = str(status or "").lower()
    if s in _SUCCESS_STATUSES:
        return "paid"
    if s in _FAILURE_STATUSES:
        return "failed"
    return None


def _find_winner_by_reference(db: Session, reference: str) -> Winner | None:
    return db.execute(
        select(Winner).where(Winner.payout_reference == reference)
    ).scalar_one_or_none()


def _find_giveaway_refund(db: Session, reference: str) -> Giveaway | None:
    return db.execute(
        select(Giveaway).where(Giveaway.refund_reference == reference)
    ).scalar_one_or_none()


def _record_payout_event(
    db: Session,
    provider: str,
    event_type: str,
    reference: str,
    amount_kobo: int | None,
    giveaway_id: str | None,
    winner_id: str | None,
    payload: dict,
) -> bool:
    if payment_already_processed(db, reference):
        logger.info("Duplicate payout webhook ref %s", reference)
        return False
    event = PaymentEvent(
        provider=provider,
        event_type=event_type,
        external_reference=reference,
        payment_reference=reference,
        giveaway_id=giveaway_id,
        winner_id=winner_id,
        amount_kobo=amount_kobo,
        raw_json=str(payload),
    )
    db.add(event)
    return True


def _apply_winner_terminal(winner: Winner, action: str, status: str) -> None:
    if action == "paid":
        winner.payout_status = PayoutStatus.PAID
        winner.status = WinnerStatus.PAID
        winner.paid_at = datetime.now(timezone.utc)
        winner.notes = None
    else:
        winner.payout_status = PayoutStatus.FAILED
        winner.notes = f"Payout {status} via webhook"


def _apply_refund_terminal(giveaway: Giveaway, action: str) -> None:
    if action == "paid":
        giveaway.refund_status = RefundStatus.COMPLETED
    else:
        giveaway.refund_status = RefundStatus.FAILED


def confirm_winner_payout(
    db: Session,
    winner: Winner,
    reference: str,
    status: str,
    provider: str,
    event_type: str,
    payload: dict,
    amount_kobo: int | None = None,
) -> str:
    action = _terminal_action(status)
    if action is None:
        return "processing"

    recorded = _record_payout_event(
        db,
        provider,
        event_type,
        reference,
        amount_kobo,
        winner.giveaway_id,
        winner.id,
        payload,
    )
    if not recorded:
        if winner.payout_status == PayoutStatus.PROCESSING:
            _apply_winner_terminal(winner, action, status)
            db.commit()
            return action
        db.commit()
        return "duplicate"

    _apply_winner_terminal(winner, action, status)
    db.commit()
    return action


def confirm_host_refund(
    db: Session,
    giveaway: Giveaway,
    reference: str,
    status: str,
    provider: str,
    event_type: str,
    payload: dict,
    amount_kobo: int | None = None,
) -> str:
    action = _terminal_action(status)
    if action is None:
        return "processing"

    recorded = _record_payout_event(
        db,
        provider,
        event_type,
        reference,
        amount_kobo,
        giveaway.id,
        None,
        payload,
    )
    if not recorded:
        if giveaway.refund_status == RefundStatus.PROCESSING:
            _apply_refund_terminal(giveaway, action)
            db.commit()
            return action
        db.commit()
        return "duplicate"

    _apply_refund_terminal(giveaway, action)
    db.commit()
    return action


def handle_safehaven_account_debit(db: Session, payload: dict) -> tuple[str | None, str, str | None]:
    """
    Outbound transfer confirmation (winner payout or host refund).
    Returns (entity_id, action, kind) where kind is winner|refund|ignored.
    """
    data = payload.get("data") or payload
    reference = str(data.get("paymentReference") or data.get("sessionId") or "")
    if not reference or not _is_payout_reference(reference):
        return None, "ignored", None

    status = str(data.get("status") or "")
    amount_kobo = int(float(data.get("amount", 0)) * 100) if data.get("amount") else None

    winner = _find_winner_by_reference(db, reference)
    if winner:
        action = confirm_winner_payout(
            db, winner, reference, status, "safehaven", "account.debit", payload, amount_kobo
        )
        return winner.id, action, "winner"

    giveaway = _find_giveaway_refund(db, reference)
    if giveaway:
        action = confirm_host_refund(
            db, giveaway, reference, status, "safehaven", "account.debit", payload, amount_kobo
        )
        return giveaway.id, action, "refund"

    logger.warning("No payout/refund for SafeHaven debit ref %s", reference)
    return None, "ignored", None


def handle_paystack_transfer_event(db: Session, payload: dict) -> tuple[str | None, str, str | None]:
    """Paystack transfer.success / transfer.failed / transfer.reversed."""
    event = str(payload.get("event") or "")
    data = payload.get("data") or {}
    reference = str(data.get("reference") or data.get("transfer_code") or "")
    if not reference or not _is_payout_reference(reference):
        return None, "ignored", None

    status = str(data.get("status") or event.replace("transfer.", ""))
    amount_kobo = int(data.get("amount") or 0) or None

    winner = _find_winner_by_reference(db, reference)
    if winner:
        action = confirm_winner_payout(
            db, winner, reference, status, "paystack", event, payload, amount_kobo
        )
        return winner.id, action, "winner"

    giveaway = _find_giveaway_refund(db, reference)
    if giveaway:
        action = confirm_host_refund(
            db, giveaway, reference, status, "paystack", event, payload, amount_kobo
        )
        return giveaway.id, action, "refund"

    logger.warning("No payout/refund for Paystack transfer ref %s", reference)
    return None, "ignored", None