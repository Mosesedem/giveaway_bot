"""Funding provider abstraction with SafeHaven primary + Paystack failover."""

import logging
from typing import Protocol

from app.payments.exceptions import PaymentError, ProviderConfigError
from app.payments.paystack import PaystackClient
from app.payments.safehaven import SafeHavenClient, VirtualAccountResult

logger = logging.getLogger(__name__)


class FundingProvider(Protocol):
    name: str

    def configured(self) -> bool: ...

    def create_virtual_account(
        self,
        amount_kobo: int,
        external_reference: str,
        callback_url: str,
        valid_for_seconds: int,
        giveaway_id: str,
    ) -> VirtualAccountResult: ...


class SafeHavenFundingProvider:
    name = "safehaven"

    def __init__(self):
        self._client = SafeHavenClient()

    def configured(self) -> bool:
        return self._client.configured()

    def create_virtual_account(
        self,
        amount_kobo: int,
        external_reference: str,
        callback_url: str,
        valid_for_seconds: int,
        giveaway_id: str,
    ) -> VirtualAccountResult:
        return self._client.create_virtual_account(
            amount_kobo=amount_kobo,
            external_reference=external_reference,
            callback_url=callback_url,
            valid_for_seconds=valid_for_seconds,
        )


class PaystackFundingProvider:
    name = "paystack"

    def __init__(self):
        self._client = PaystackClient()

    def configured(self) -> bool:
        return self._client.configured()

    def create_virtual_account(
        self,
        amount_kobo: int,
        external_reference: str,
        callback_url: str,
        valid_for_seconds: int,
        giveaway_id: str,
    ) -> VirtualAccountResult:
        return self._client.create_virtual_account(
            amount_kobo=amount_kobo,
            external_reference=external_reference,
            giveaway_id=giveaway_id,
            valid_for_seconds=valid_for_seconds,
        )


def create_funding_va(
    amount_kobo: int,
    external_reference: str,
    callback_url: str,
    valid_for_seconds: int,
    giveaway_id: str,
) -> tuple[VirtualAccountResult, str]:
    """Try SafeHaven first, then Paystack. Returns (va, provider_name)."""
    providers: list[FundingProvider] = [
        SafeHavenFundingProvider(),
        PaystackFundingProvider(),
    ]
    errors: list[str] = []
    for provider in providers:
        if not provider.configured():
            continue
        try:
            va = provider.create_virtual_account(
                amount_kobo=amount_kobo,
                external_reference=external_reference,
                callback_url=callback_url,
                valid_for_seconds=valid_for_seconds,
                giveaway_id=giveaway_id,
            )
            logger.info("Funding VA created via %s for %s", provider.name, external_reference)
            return va, provider.name
        except Exception as exc:
            logger.warning("Funding provider %s failed: %s", provider.name, exc)
            errors.append(f"{provider.name}: {exc}")

    if not errors:
        raise ProviderConfigError("No funding provider configured (SafeHaven or Paystack)")
    raise PaymentError("All funding providers failed — " + "; ".join(errors))


def get_payout_client(preferred: str | None = None):
    """Return SafeHaven or Paystack client for verify/transfer with failover."""
    if preferred == "paystack":
        ps = PaystackClient()
        if ps.configured():
            return ps
    sh = SafeHavenClient()
    if sh.configured():
        return sh
    ps = PaystackClient()
    if ps.configured():
        return ps
    raise ProviderConfigError("No payout provider configured")