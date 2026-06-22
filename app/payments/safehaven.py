"""SafeHaven MFB API client (virtual accounts, name enquiry, transfers)."""

import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

import requests

from app.payments.exceptions import (
    AccountVerificationError,
    ProviderConfigError,
    TransferError,
    VirtualAccountError,
)
from app.payments import safehaven_auth

logger = logging.getLogger(__name__)


@dataclass
class VirtualAccountResult:
    provider_id: str
    account_number: str
    bank_name: str
    account_name: str
    external_reference: str
    expires_in_seconds: int


@dataclass
class NameEnquiryResult:
    session_id: str
    account_name: str
    bank_code: str
    account_number: str


@dataclass
class TransferResult:
    reference: str
    session_id: str
    status: str


class SafeHavenClient:
    def __init__(self):
        self.base_url = os.getenv("SAFEHAVEN_BASE_URL", "https://api.sandbox.safehavenmfb.com").rstrip("/")
        self.client_id = os.getenv("SAFEHAVEN_CLIENT_ID", "")
        self.client_assertion = os.getenv("SAFEHAVEN_CLIENT_ASSERTION", "")
        self.ibs_client_id = os.getenv("SAFEHAVEN_IBS_CLIENT_ID", "")
        self.debit_account = os.getenv("SAFEHAVEN_DEBIT_ACCOUNT", "")
        self.settlement_account_number = os.getenv("SAFEHAVEN_SETTLEMENT_ACCOUNT_NUMBER", "")
        self.settlement_bank_code = os.getenv("SAFEHAVEN_SETTLEMENT_BANK_CODE", "999240")
        self.mock = os.getenv("SAFEHAVEN_MOCK", "false").lower() == "true"
        self._access_token = os.getenv("SAFEHAVEN_ACCESS_TOKEN", "")
        self._token_expires_at = 0.0

    def configured(self) -> bool:
        return self.mock or bool(
            self._access_token
            or (self.client_id and self.client_assertion)
            or (self.client_id and safehaven_auth.has_signing_material())
        )

    def _client_assertion(self) -> str:
        if self.client_assertion:
            return self.client_assertion
        token_url = f"{self.base_url}/oauth2/token"
        return safehaven_auth.build_client_assertion(self.client_id, token_url)

    def _ensure_token(self) -> str:
        if self.mock:
            return "mock-token"
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token
        if not self.client_id:
            if self._access_token:
                return self._access_token
            raise ProviderConfigError("SafeHaven credentials not configured")
        if not self.client_assertion and not safehaven_auth.has_signing_material():
            if self._access_token:
                return self._access_token
            raise ProviderConfigError(
                "Set SAFEHAVEN_CLIENT_ASSERTION or SAFEHAVEN_PRIVATE_KEY for OAuth"
            )

        resp = requests.post(
            f"{self.base_url}/oauth2/token",
            json={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_assertion": self._client_assertion(),
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["access_token"]
        self._token_expires_at = time.time() + int(data.get("expires_in", 2400))
        if not self.ibs_client_id:
            self.ibs_client_id = data.get("ibs_client_id", "")
        return self._access_token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._ensure_token()}",
            "ClientID": self.ibs_client_id,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        if self.mock:
            return {}
        url = f"{self.base_url}{path}"
        resp = requests.request(method, url, headers=self._headers(), timeout=45, **kwargs)
        try:
            payload = resp.json()
        except Exception:
            payload = {"raw": resp.text}
        if resp.status_code >= 400:
            logger.error("SafeHaven %s %s failed: %s", method, path, payload)
            resp.raise_for_status()
        return payload

    def create_virtual_account(
        self,
        amount_kobo: int,
        external_reference: str,
        callback_url: str,
        valid_for_seconds: int = 86400,
    ) -> VirtualAccountResult:
        if self.mock:
            acct = f"9{uuid.uuid4().int % 10**9:09d}"
            return VirtualAccountResult(
                provider_id=f"mock-va-{external_reference}",
                account_number=acct,
                bank_name="Safe Haven MFB (Mock)",
                account_name="Giveaway Escrow",
                external_reference=external_reference,
                expires_in_seconds=valid_for_seconds,
            )

        amount_naira = amount_kobo / 100
        body = {
            "validFor": valid_for_seconds,
            "callbackUrl": callback_url,
            "amountControl": "Fixed",
            "amount": int(amount_naira) if amount_kobo % 100 == 0 else amount_naira,
            "externalReference": external_reference,
            "settlementAccount": {
                "accountNumber": self.settlement_account_number,
                "bankCode": self.settlement_bank_code,
            },
        }
        data = self._request("POST", "/virtual-accounts", json=body)
        va = data.get("data") or data
        if not va.get("accountNumber"):
            raise VirtualAccountError(f"Unexpected VA response: {data}")
        return VirtualAccountResult(
            provider_id=str(va.get("_id") or va.get("id") or external_reference),
            account_number=str(va["accountNumber"]),
            bank_name=str(va.get("bankName") or "Safe Haven MFB"),
            account_name=str(va.get("accountName") or "Giveaway Funding"),
            external_reference=external_reference,
            expires_in_seconds=valid_for_seconds,
        )

    def name_enquiry(self, bank_code: str, account_number: str) -> NameEnquiryResult:
        if self.mock:
            return NameEnquiryResult(
                session_id=f"mock-session-{account_number}",
                account_name="MOCK ACCOUNT HOLDER",
                bank_code=bank_code,
                account_number=account_number,
            )

        data = self._request(
            "POST",
            "/transfers/name-enquiry",
            json={"bankCode": bank_code, "accountNumber": account_number},
        )
        inner = data.get("data") or {}
        if inner.get("responseCode") not in ("00", None) and data.get("responseCode") != "00":
            raise AccountVerificationError(data.get("message") or "Name enquiry failed")
        return NameEnquiryResult(
            session_id=str(inner["sessionId"]),
            account_name=str(inner["accountName"]),
            bank_code=str(inner.get("bankCode") or bank_code),
            account_number=str(inner.get("accountNumber") or account_number),
        )

    def transfer(
        self,
        name_enquiry_ref: str,
        beneficiary_bank_code: str,
        beneficiary_account_number: str,
        amount_kobo: int,
        narration: str,
        payment_reference: str,
        debit_account_number: str | None = None,
    ) -> TransferResult:
        if self.mock:
            return TransferResult(
                reference=payment_reference,
                session_id=f"mock-out-{payment_reference}",
                status="Completed",
            )

        debit_account = debit_account_number or self.debit_account
        if not debit_account:
            raise ProviderConfigError("Payout debit account is required (giveaway VA or SAFEHAVEN_DEBIT_ACCOUNT)")

        amount_naira = amount_kobo / 100
        body = {
            "nameEnquiryReference": name_enquiry_ref,
            "debitAccountNumber": debit_account,
            "beneficiaryBankCode": beneficiary_bank_code,
            "beneficiaryAccountNumber": beneficiary_account_number,
            "amount": int(amount_naira) if amount_kobo % 100 == 0 else amount_naira,
            "saveBeneficiary": False,
            "narration": narration[:100],
            "paymentReference": payment_reference,
        }
        data = self._request("POST", "/transfers", json=body)
        inner = data.get("data") or {}
        return TransferResult(
            reference=payment_reference,
            session_id=str(inner.get("sessionId") or payment_reference),
            status=str(inner.get("status") or "Completed"),
        )

    def list_banks(self) -> list[dict[str, str]]:
        if self.mock:
            return [
                {"code": "058", "name": "GTBank"},
                {"code": "011", "name": "First Bank"},
                {"code": "999240", "name": "Safe Haven MFB"},
            ]
        data = self._request("GET", "/transfers/banks")
        banks = data.get("data") or data.get("banks") or []
        return [{"code": str(b.get("code") or b.get("bankCode")), "name": str(b.get("name"))} for b in banks]