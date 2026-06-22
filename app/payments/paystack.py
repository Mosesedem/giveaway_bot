"""Paystack backup provider (disabled unless admin enables)."""

import logging
import os
import uuid
from dataclasses import dataclass

import requests

from app.payments.exceptions import (
    AccountVerificationError,
    ProviderConfigError,
    ProviderDisabledError,
    TransferError,
    VirtualAccountError,
)
from app.payments.safehaven import VirtualAccountResult

logger = logging.getLogger(__name__)


@dataclass
class PaystackResolveResult:
    account_name: str
    account_number: str
    bank_code: str


@dataclass
class PaystackTransferResult:
    reference: str
    status: str


class PaystackClient:
    def __init__(self):
        self.secret_key = os.getenv("PAYSTACK_SECRET_KEY", "")
        self.base_url = "https://api.paystack.co"
        self.mock = os.getenv("PAYSTACK_MOCK", "false").lower() == "true"

    def configured(self) -> bool:
        return self.mock or bool(self.secret_key)

    @property
    def name(self) -> str:
        return "paystack"

    def _headers(self) -> dict[str, str]:
        if not self.secret_key and not self.mock:
            raise ProviderConfigError("PAYSTACK_SECRET_KEY not set")
        return {"Authorization": f"Bearer {self.secret_key}", "Content-Type": "application/json"}

    def create_virtual_account(
        self,
        amount_kobo: int,
        external_reference: str,
        giveaway_id: str,
        valid_for_seconds: int = 86400,
    ) -> VirtualAccountResult:
        if self.mock:
            acct = f"8{uuid.uuid4().int % 10**9:09d}"
            return VirtualAccountResult(
                provider_id=f"mock-paystack-va-{giveaway_id}",
                account_number=acct,
                bank_name="Wema Bank (Paystack Mock)",
                account_name="Giveaway Escrow",
                external_reference=external_reference,
                expires_in_seconds=valid_for_seconds,
            )

        email = f"gw-{giveaway_id}@giveaways.bot"
        cust_resp = requests.post(
            f"{self.base_url}/customer",
            json={
                "email": email,
                "first_name": "Giveaway",
                "last_name": giveaway_id[:8],
                "metadata": {
                    "giveaway_id": giveaway_id,
                    "external_reference": external_reference,
                    "expected_amount_kobo": amount_kobo,
                },
            },
            headers=self._headers(),
            timeout=30,
        )
        cust_data = cust_resp.json()
        if not cust_data.get("status"):
            raise VirtualAccountError(cust_data.get("message") or "Paystack customer creation failed")
        customer_code = cust_data["data"]["customer_code"]

        preferred_bank = os.getenv("PAYSTACK_PREFERRED_BANK", "wema-bank")
        dva_resp = requests.post(
            f"{self.base_url}/dedicated_account",
            json={"customer": customer_code, "preferred_bank": preferred_bank},
            headers=self._headers(),
            timeout=45,
        )
        dva_data = dva_resp.json()
        if not dva_data.get("status"):
            raise VirtualAccountError(dva_data.get("message") or "Paystack DVA creation failed")
        inner = dva_data["data"]
        bank = inner.get("bank") or {}
        return VirtualAccountResult(
            provider_id=str(inner.get("id") or customer_code),
            account_number=str(inner["account_number"]),
            bank_name=str(bank.get("name") or "Paystack"),
            account_name=str(inner.get("account_name") or "Giveaway Funding"),
            external_reference=external_reference,
            expires_in_seconds=valid_for_seconds,
        )

    def resolve_account(self, account_number: str, bank_code: str) -> PaystackResolveResult:
        if self.mock:
            return PaystackResolveResult(
                account_name="MOCK PAYSTACK HOLDER",
                account_number=account_number,
                bank_code=bank_code,
            )
        resp = requests.get(
            f"{self.base_url}/bank/resolve",
            params={"account_number": account_number, "bank_code": bank_code},
            headers=self._headers(),
            timeout=30,
        )
        data = resp.json()
        if not data.get("status"):
            raise AccountVerificationError(data.get("message") or "Paystack resolve failed")
        return PaystackResolveResult(
            account_name=str(data["data"]["account_name"]),
            account_number=account_number,
            bank_code=bank_code,
        )

    def transfer(
        self,
        amount_kobo: int,
        recipient_code: str | None,
        account_number: str,
        bank_code: str,
        account_name: str,
        narration: str,
        reference: str,
    ) -> PaystackTransferResult:
        if self.mock:
            return PaystackTransferResult(reference=reference, status="success")

        # Create transfer recipient if needed
        if not recipient_code:
            rc_resp = requests.post(
                f"{self.base_url}/transferrecipient",
                json={
                    "type": "nuban",
                    "name": account_name,
                    "account_number": account_number,
                    "bank_code": bank_code,
                    "currency": "NGN",
                },
                headers=self._headers(),
                timeout=30,
            )
            rc_data = rc_resp.json()
            if not rc_data.get("status"):
                raise TransferError(rc_data.get("message") or "Failed to create recipient")
            recipient_code = rc_data["data"]["recipient_code"]

        tr_resp = requests.post(
            f"{self.base_url}/transfer",
            json={
                "source": "balance",
                "amount": amount_kobo,
                "recipient": recipient_code,
                "reason": narration[:100],
                "reference": reference,
            },
            headers=self._headers(),
            timeout=45,
        )
        tr_data = tr_resp.json()
        if not tr_data.get("status"):
            raise TransferError(tr_data.get("message") or "Paystack transfer failed")
        return PaystackTransferResult(
            reference=reference,
            status=str(tr_data["data"].get("status") or "success"),
        )


def require_enabled(enabled: bool) -> None:
    if not enabled:
        raise ProviderDisabledError("Paystack is disabled — enable in admin settings")