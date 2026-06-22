"""Webhook handlers for SafeHaven and Paystack."""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Giveaway
from app.payments.funding_service import apply_funding_credit, record_funding_event

logger = logging.getLogger(__name__)


def _amount_to_kobo(raw_amount) -> int:
    if raw_amount is None:
        return 0
    if isinstance(raw_amount, int) and raw_amount > 1_000_000:
        return raw_amount
    return int(float(raw_amount) * 100)


def _find_giveaway(db: Session, external_ref: str) -> Giveaway | None:
    giveaway = db.execute(
        select(Giveaway).where(Giveaway.va_external_reference == external_ref)
    ).scalar_one_or_none()
    if not giveaway and str(external_ref).startswith("gw-"):
        gid = str(external_ref).replace("gw-", "", 1)
        giveaway = db.get(Giveaway, gid)
    return giveaway


def _completed_status(data: dict) -> bool:
    status = str(data.get("status") or "").lower()
    return not status or status in {"completed", "success", "approved"}


def _apply_incoming_credit(
    db: Session,
    giveaway: Giveaway,
    amount_kobo: int,
    payment_ref: str,
    provider_event: str,
    payload: dict,
) -> tuple[Giveaway, str]:
    event = record_funding_event(
        db,
        giveaway,
        "safehaven",
        provider_event,
        {"amount_kobo": amount_kobo, "payment_reference": payment_ref, "raw": payload},
        payment_reference=payment_ref,
    )
    if event is None:
        db.commit()
        return giveaway, "duplicate"
    db.commit()
    action = apply_funding_credit(db, giveaway, amount_kobo, payment_ref)
    db.refresh(giveaway)
    if action == "mismatch":
        from app.conversation.host_funding import open_host_funding_session

        open_host_funding_session(db, giveaway)
    return giveaway, action


def handle_safehaven_account_credit(db: Session, payload: dict) -> tuple[Giveaway | None, str]:
    """Handle account.credit / inwards transfer to a giveaway VA."""
    data = payload.get("data") or payload
    external_ref = data.get("externalReference") or data.get("mandateReference")
    acct = str(data.get("creditAccountNumber") or "")
    giveaway = None
    if external_ref:
        giveaway = _find_giveaway(db, str(external_ref))
    if not giveaway and acct:
        giveaway = db.execute(
            select(Giveaway).where(Giveaway.va_account_number == acct)
        ).scalar_one_or_none()
    if not giveaway:
        logger.warning("No giveaway for account.credit acct=%s ref=%s", acct, external_ref)
        return None, "ignored"
    if not _completed_status(data):
        return giveaway, "ignored"

    amount_kobo = _amount_to_kobo(data.get("amount"))
    payment_ref = str(data.get("paymentReference") or data.get("sessionId") or acct)
    return _apply_incoming_credit(db, giveaway, amount_kobo, payment_ref, "account.credit", payload)


def handle_safehaven_webhook(db: Session, payload: dict) -> tuple[Giveaway | None, str]:
    from app.payments.payout_webhooks import handle_safehaven_account_debit

    event = str(payload.get("type") or payload.get("eventType") or "").lower()
    data = payload.get("data") or {}

    if "debit" in event or event == "account.debit" or data.get("type") == "Outwards":
        entity_id, action, kind = handle_safehaven_account_debit(db, payload)
        if kind:
            return None, f"payout_{action}"
        return None, action

    if "virtualaccount" in event.replace(".", "") or event == "virtualaccount.transfer":
        return handle_safehaven_virtual_account_transfer(db, payload)
    if "credit" in event or event == "account.credit":
        return handle_safehaven_account_credit(db, payload)
    if data.get("creditAccountNumber"):
        return handle_safehaven_account_credit(db, payload)
    return handle_safehaven_virtual_account_transfer(db, payload)


def handle_paystack_webhook(db: Session, payload: dict) -> dict:
    """Route Paystack events to funding or payout confirmation handlers."""
    from app.payments.payout_webhooks import handle_paystack_transfer_event

    event = str(payload.get("event") or "")
    data = payload.get("data") or {}
    reference = str(data.get("reference") or "")

    if event in {"transfer.success", "transfer.failed", "transfer.reversed"}:
        entity_id, action, kind = handle_paystack_transfer_event(db, payload)
        if kind:
            return {
                "kind": kind,
                "entity_id": entity_id,
                "action": action,
                "giveaway": None,
                "funding_action": None,
            }
        if event != "transfer.success":
            return {
                "kind": "ignored",
                "entity_id": None,
                "action": "ignored",
                "giveaway": None,
                "funding_action": None,
            }

    if event in {"charge.success", "transfer.success"}:
        giveaway, funding_action = handle_paystack_charge_success(db, payload)
        return {
            "kind": "funding",
            "entity_id": giveaway.id if giveaway else None,
            "action": funding_action,
            "giveaway": giveaway,
            "funding_action": funding_action,
        }

    return {"kind": "ignored", "entity_id": None, "action": "ignored", "giveaway": None, "funding_action": None}


def _is_paystack_payout_ref(reference: str) -> bool:
    ref = str(reference or "").lower()
    return ref.startswith("payout-") or ref.startswith("refund-")


def handle_safehaven_virtual_account_transfer(db: Session, payload: dict) -> tuple[Giveaway | None, str]:
    data = payload.get("data") or payload
    external_ref = data.get("externalReference") or data.get("paymentReference")
    if not external_ref:
        logger.warning("SafeHaven VA webhook missing reference: %s", payload)
        return None, "ignored"

    giveaway = _find_giveaway(db, str(external_ref))
    if not giveaway:
        logger.warning("No giveaway for VA ref %s", external_ref)
        return None, "ignored"

    if not _completed_status(data):
        logger.info("Ignoring non-completed VA transfer status=%s", data.get("status"))
        return giveaway, "ignored"

    amount_kobo = _amount_to_kobo(data.get("amount"))
    payment_ref = str(data.get("paymentReference") or data.get("sessionId") or external_ref)
    gw, action = _apply_incoming_credit(
        db, giveaway, amount_kobo, payment_ref, "virtualAccount.transfer", payload
    )
    return gw, action


def handle_paystack_charge_success(db: Session, payload: dict) -> tuple[Giveaway | None, str]:
    data = payload.get("data") or {}
    metadata = data.get("metadata") or {}
    giveaway_id = metadata.get("giveaway_id")
    external_ref = metadata.get("external_reference")

    giveaway = None
    if giveaway_id:
        giveaway = db.get(Giveaway, str(giveaway_id))
    if not giveaway and external_ref:
        giveaway = _find_giveaway(db, str(external_ref))
    if not giveaway:
        reference = data.get("reference", "")
        if str(reference).startswith("gw-"):
            giveaway = _find_giveaway(db, reference)
    if not giveaway:
        return None, "ignored"

    amount_kobo = int(data.get("amount") or 0)
    payment_ref = str(data.get("reference") or data.get("id") or giveaway.id)

    event = record_funding_event(
        db, giveaway, "paystack", payload.get("event", "charge.success"),
        {"amount_kobo": amount_kobo, "payment_reference": payment_ref, "raw": payload},
        payment_reference=payment_ref,
    )
    if event is None:
        db.commit()
        return giveaway, "duplicate"

    db.commit()
    action = apply_funding_credit(db, giveaway, amount_kobo, payment_ref)
    db.refresh(giveaway)
    if action == "mismatch":
        from app.conversation.host_funding import open_host_funding_session

        open_host_funding_session(db, giveaway)
    return giveaway, action