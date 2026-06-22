"""Webhook authentication helpers (Paystack HMAC + shared-secret guards)."""

import hashlib
import hmac
import logging
import os

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)


def verify_paystack_signature(body: bytes, signature: str | None) -> None:
    secret = os.getenv("PAYSTACK_SECRET_KEY", "")
    if os.getenv("PAYSTACK_MOCK", "false").lower() == "true":
        return
    if not secret:
        logger.warning("Paystack webhook received but PAYSTACK_SECRET_KEY unset — rejecting")
        raise HTTPException(401, "Paystack webhook secret not configured")
    if not signature:
        raise HTTPException(401, "Missing x-paystack-signature header")
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha512).hexdigest()
    if not hmac.compare_digest(digest, signature):
        raise HTTPException(401, "Invalid Paystack webhook signature")


def verify_safehaven_webhook(request: Request) -> None:
    """
    SafeHaven docs do not publish HMAC signing. We enforce:
    - optional shared secret header (industry best practice)
    - skip verification in mock/dev when secret unset
    """
    secret = os.getenv("SAFEHAVEN_WEBHOOK_SECRET", "")
    if not secret:
        if os.getenv("SAFEHAVEN_MOCK", "false").lower() == "true":
            return
        if os.getenv("REQUIRE_WEBHOOK_SECRET", "false").lower() != "true":
            return
        raise HTTPException(401, "SAFEHAVEN_WEBHOOK_SECRET required in production")

    auth = request.headers.get("Authorization", "")
    header_secret = request.headers.get("X-Webhook-Secret", "")
    if auth == f"Bearer {secret}" or hmac.compare_digest(header_secret, secret):
        return
    raise HTTPException(401, "Invalid SafeHaven webhook credentials")