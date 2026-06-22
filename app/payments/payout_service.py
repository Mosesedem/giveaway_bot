"""Winner payout: bank-app style collect → verify → transfer."""

import logging
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Giveaway, PayoutStatus, Winner, WinnerStatus, PaymentEvent
from app.payments.exceptions import AccountVerificationError, PaymentError, ProviderDisabledError
from app.payments.money import format_ngn
from app.payments.money import prize_per_winner
from app.payments.paystack import PaystackClient
from app.payments.safehaven import SafeHavenClient
from app.payments.settings import paystack_enabled, payout_provider

logger = logging.getLogger(__name__)

BANK_LINE_RE = re.compile(r"bank\s*[:=]\s*([a-zA-Z0-9\s]+?)(?:\s+account|\s+acct|\s*$)", re.I)
ACCOUNT_RE = re.compile(r"(?:acct|account|acc)\s*[:=]?\s*(\d{10})", re.I)
PLAIN_ACCOUNT_RE = re.compile(r"\b(\d{10})\b")

from app.payments.banks import BANK_ALIASES, resolve_bank_code as _resolve_bank_code


def _provider_order(preferred: str) -> list[str]:
    if preferred == "paystack":
        return ["paystack", "safehaven"]
    return ["safehaven", "paystack"]


def _verification_providers(preferred: str) -> list[str]:
    order = _provider_order(preferred)
    available = []
    for name in order:
        if name == "paystack" and PaystackClient().configured():
            available.append(name)
        elif name == "safehaven" and SafeHavenClient().configured():
            available.append(name)
    return available or ["safehaven", "paystack"]


def prize_per_winner_kobo(giveaway: Giveaway) -> int:
    pool = giveaway.prize_pool_kobo
    if not pool and giveaway.amount_kobo:
        pool = max(0, giveaway.amount_kobo - (giveaway.transaction_fee_kobo or 0))
    if not pool or giveaway.num_winners < 1:
        return 0
    return prize_per_winner(pool, giveaway.num_winners)


def parse_bank_from_text(text: str) -> tuple[str | None, str | None]:
    """Return (bank_code, account_number) from free-form DM text."""
    acct_match = ACCOUNT_RE.search(text) or PLAIN_ACCOUNT_RE.search(text)
    if not acct_match:
        return None, None
    account_number = acct_match.group(1)

    bank_name = None
    bank_match = BANK_LINE_RE.search(text)
    if bank_match:
        bank_name = bank_match.group(1).strip()

    lower = text.lower()
    bank_code = None
    if bank_name:
        bank_code = resolve_bank_code(bank_name)
    if not bank_code:
        for alias, code in BANK_ALIASES.items():
            if alias in lower:
                bank_code = code
                break
    return bank_code, account_number


def resolve_bank_code(bank_name: str) -> str | None:
    return _resolve_bank_code(bank_name)


def start_payout_collection(winner: Winner, giveaway: Giveaway) -> str:
    amount = format_ngn(prize_per_winner_kobo(giveaway))
    winner.payout_status = PayoutStatus.COLLECTING_DETAILS
    winner.payout_amount_kobo = prize_per_winner_kobo(giveaway)
    return (
        f"You won {giveaway.title}! Prize: {amount}.\n\n"
        "Reply with your bank details in this format:\n"
        "Bank: GTBank\n"
        "Account: 0123456789\n\n"
        "We'll verify your name before sending payment."
    )


def apply_payout_details(
    db: Session,
    winner: Winner,
    bank_code: str,
    account_number: str,
    bank_name: str | None = None,
) -> str:
    winner.bank_code = bank_code
    winner.account_number = account_number
    winner.bank_name = bank_name
    winner.payout_status = PayoutStatus.VERIFYING
    db.commit()

    preferred = payout_provider(db) if paystack_enabled(db) else "safehaven"
    errors: list[str] = []
    for provider_name in _verification_providers(preferred):
        try:
            if provider_name == "paystack":
                ps = PaystackClient()
                resolved = ps.resolve_account(account_number, bank_code)
                winner.account_name = resolved.account_name
                winner.name_enquiry_ref = f"paystack-{account_number}"
                winner.payout_provider = "paystack"
            else:
                sh = SafeHavenClient()
                enquiry = sh.name_enquiry(bank_code, account_number)
                winner.account_name = enquiry.account_name
                winner.name_enquiry_ref = enquiry.session_id
                winner.payout_provider = "safehaven"
            winner.payout_status = PayoutStatus.READY
            db.commit()
            return (
                f"Account verified: {winner.account_name}\n"
                f"{account_number} · {bank_name or bank_code}\n\n"
                "Reply YES to confirm payout, or send corrected details."
            )
        except (AccountVerificationError, ProviderDisabledError, PaymentError) as exc:
            errors.append(f"{provider_name}: {exc}")

    winner.payout_status = PayoutStatus.COLLECTING_DETAILS
    db.commit()
    detail = errors[-1] if errors else "verification failed"
    return f"Could not verify account: {detail}. Please check bank name and 10-digit account number."


def execute_winner_payout(db: Session, winner: Winner, giveaway: Giveaway) -> str:
    if winner.payout_status != PayoutStatus.READY:
        raise PaymentError("Winner payout is not ready")
    if not winner.name_enquiry_ref or not winner.account_number or not winner.bank_code:
        raise PaymentError("Missing verified account details")

    amount = winner.payout_amount_kobo or prize_per_winner_kobo(giveaway)
    if amount <= 0:
        raise PaymentError("Invalid payout amount")

    reference = f"payout-{winner.id}-{uuid.uuid4().hex[:8]}"
    winner.payout_status = PayoutStatus.PROCESSING
    winner.payout_reference = reference
    db.commit()

    preferred = winner.payout_provider or (payout_provider(db) if paystack_enabled(db) else "safehaven")
    narration = f"Giveaway prize {giveaway.title[:40]}"
    debit_account = giveaway.payout_source_account or giveaway.va_account_number
    transfer_errors: list[str] = []

    try:
        result = None
        for provider_name in _verification_providers(preferred):
            try:
                if provider_name == "paystack":
                    ps = PaystackClient()
                    result = ps.transfer(
                        amount_kobo=amount,
                        recipient_code=None,
                        account_number=winner.account_number,
                        bank_code=winner.bank_code,
                        account_name=winner.account_name or "Winner",
                        narration=narration,
                        reference=reference,
                    )
                    winner.payout_provider = "paystack"
                else:
                    sh = SafeHavenClient()
                    result = sh.transfer(
                        name_enquiry_ref=winner.name_enquiry_ref,
                        beneficiary_bank_code=winner.bank_code,
                        beneficiary_account_number=winner.account_number,
                        amount_kobo=amount,
                        narration=narration,
                        payment_reference=reference,
                        debit_account_number=debit_account,
                    )
                    winner.payout_provider = "safehaven"
                break
            except Exception as exc:
                transfer_errors.append(f"{provider_name}: {exc}")
        if result is None:
            raise PaymentError("; ".join(transfer_errors) or "transfer failed")

        winner.payout_status = PayoutStatus.PAID
        winner.status = WinnerStatus.PAID
        winner.paid_at = datetime.now(timezone.utc)
        db.add(
            PaymentEvent(
                provider=winner.payout_provider,
                event_type="payout.completed",
                external_reference=reference,
                giveaway_id=giveaway.id,
                winner_id=winner.id,
                amount_kobo=amount,
                raw_json=str({"status": getattr(result, "status", "ok")}),
            )
        )
        db.commit()
        return f"Paid {format_ngn(amount)} to {winner.account_name}. Ref: {reference}"
    except Exception as exc:
        winner.payout_status = PayoutStatus.FAILED
        winner.notes = str(exc)
        db.commit()
        raise PaymentError(str(exc)) from exc