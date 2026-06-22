"""Refund excess overpayment to host when restructuring."""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Giveaway, PaymentEvent, RefundStatus
from app.payments.exceptions import AccountVerificationError, PaymentError
from app.payments.money import format_ngn
from app.payments.paystack import PaystackClient
from app.payments.payout_service import _verification_providers, parse_bank_from_text
from app.payments.safehaven import SafeHavenClient

logger = logging.getLogger(__name__)


def overpaid_excess_kobo(giveaway: Giveaway) -> int:
    received = giveaway.amount_received_kobo or 0
    expected = giveaway.amount_kobo or 0
    return max(0, received - expected)


def refund_collect_prompt(amount_kobo: int, *, full: bool = False) -> str:
    label = (
        f"your payment of {format_ngn(amount_kobo)}"
        if full
        else f"the excess {format_ngn(amount_kobo)}"
    )
    return (
        f"Before we restructure, we'll refund {label}.\n\n"
        "Reply with your bank details:\n"
        "Bank: GTBank\n"
        "Account: 0123456789"
    )


def refund_amount_for_restructure(giveaway: Giveaway) -> int:
    """Amount to return when host chooses RESTRUCTURE."""
    excess = overpaid_excess_kobo(giveaway)
    if excess > 0:
        return excess
    return giveaway.amount_received_kobo or 0


def process_host_refund_bank(
    db: Session,
    giveaway: Giveaway,
    bank_code: str,
    account_number: str,
) -> str:
    giveaway.refund_bank_code = bank_code.strip()
    giveaway.refund_account_number = account_number.strip()
    giveaway.refund_status = RefundStatus.PROCESSING
    db.commit()

    account_name = None
    name_ref = None
    provider_used = None
    errors: list[str] = []
    for provider_name in _verification_providers("safehaven"):
        try:
            if provider_name == "paystack":
                ps = PaystackClient()
                resolved = ps.resolve_account(account_number, bank_code)
                account_name = resolved.account_name
                name_ref = f"paystack-refund-{account_number}"
                provider_used = "paystack"
            else:
                sh = SafeHavenClient()
                enquiry = sh.name_enquiry(bank_code, account_number)
                account_name = enquiry.account_name
                name_ref = enquiry.session_id
                provider_used = "safehaven"
            break
        except (AccountVerificationError, PaymentError) as exc:
            errors.append(str(exc))

    if not account_name or not name_ref:
        giveaway.refund_status = RefundStatus.COLLECTING_BANK
        db.commit()
        raise PaymentError(errors[-1] if errors else "Account verification failed")

    giveaway.refund_account_name = account_name
    db.commit()
    return execute_host_refund(db, giveaway, name_ref, provider_used)


def collect_refund_bank_details(
    db: Session,
    giveaway: Giveaway,
    text: str,
) -> str:
    bank_code, account_number = parse_bank_from_text(text)
    if not bank_code or not account_number:
        return refund_collect_prompt(giveaway.refund_amount_kobo or overpaid_excess_kobo(giveaway))

    giveaway.refund_bank_code = bank_code
    giveaway.refund_account_number = account_number
    giveaway.refund_status = RefundStatus.PROCESSING
    db.commit()

    account_name = None
    name_ref = None
    provider_used = None
    errors: list[str] = []
    for provider_name in _verification_providers("safehaven"):
        try:
            if provider_name == "paystack":
                ps = PaystackClient()
                resolved = ps.resolve_account(account_number, bank_code)
                account_name = resolved.account_name
                name_ref = f"paystack-refund-{account_number}"
                provider_used = "paystack"
            else:
                sh = SafeHavenClient()
                enquiry = sh.name_enquiry(bank_code, account_number)
                account_name = enquiry.account_name
                name_ref = enquiry.session_id
                provider_used = "safehaven"
            break
        except (AccountVerificationError, PaymentError) as exc:
            errors.append(str(exc))

    if not account_name or not name_ref:
        giveaway.refund_status = RefundStatus.COLLECTING_BANK
        db.commit()
        detail = errors[-1] if errors else "verification failed"
        return f"Could not verify refund account: {detail}. Please resend bank details."

    giveaway.refund_account_name = account_name
    db.commit()
    return execute_host_refund(db, giveaway, name_ref, provider_used)


def execute_host_refund(
    db: Session,
    giveaway: Giveaway,
    name_enquiry_ref: str,
    provider: str,
) -> str:
    amount = giveaway.refund_amount_kobo or overpaid_excess_kobo(giveaway)
    if amount <= 0:
        giveaway.refund_status = RefundStatus.NOT_REQUIRED
        db.commit()
        return "No excess to refund."

    reference = f"refund-{giveaway.id}-{uuid.uuid4().hex[:8]}"
    giveaway.refund_reference = reference
    debit_account = giveaway.payout_source_account or giveaway.va_account_number
    narration = f"Giveaway overpay refund {giveaway.title[:30]}"

    try:
        if provider == "paystack":
            ps = PaystackClient()
            result = ps.transfer(
                amount_kobo=amount,
                recipient_code=None,
                account_number=giveaway.refund_account_number or "",
                bank_code=giveaway.refund_bank_code or "",
                account_name=giveaway.refund_account_name or "Host",
                narration=narration,
                reference=reference,
            )
            provider_name = "paystack"
        else:
            sh = SafeHavenClient()
            result = sh.transfer(
                name_enquiry_ref=name_enquiry_ref,
                beneficiary_bank_code=giveaway.refund_bank_code or "",
                beneficiary_account_number=giveaway.refund_account_number or "",
                amount_kobo=amount,
                narration=narration,
                payment_reference=reference,
                debit_account_number=debit_account,
            )
            provider_name = "safehaven"

        received = giveaway.amount_received_kobo or 0
        giveaway.amount_received_kobo = max(0, received - amount)
        from app.models import FundingStatus

        giveaway.funding_status = FundingStatus.AWAITING_PAYMENT
        giveaway.refund_status = RefundStatus.COMPLETED
        db.add(
            PaymentEvent(
                provider=provider_name,
                event_type="refund.completed",
                external_reference=reference,
                payment_reference=reference,
                giveaway_id=giveaway.id,
                amount_kobo=amount,
                raw_json=str({"status": getattr(result, "status", "ok")}),
            )
        )
        db.commit()
        return (
            f"Refunded {format_ngn(amount)} to {giveaway.refund_account_name}. "
            f"Ref: {reference}\n\n"
            "Now reply with your new prize amount, winners: N, and duration (e.g. 7 days)."
        )
    except Exception as exc:
        giveaway.refund_status = RefundStatus.FAILED
        db.commit()
        logger.error("Host refund failed for %s: %s", giveaway.id, exc)
        raise PaymentError(f"Refund failed: {exc}") from exc