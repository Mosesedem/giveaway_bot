"""SafeHaven OAuth2 client_assertion JWT builder (RS256)."""

import os
import time
import uuid

import jwt

from app.payments.exceptions import ProviderConfigError


def _load_private_key() -> str:
    inline = os.getenv("SAFEHAVEN_PRIVATE_KEY", "").strip()
    if inline:
        return inline.replace("\\n", "\n")
    path = os.getenv("SAFEHAVEN_PRIVATE_KEY_PATH", "").strip()
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    return ""


def has_signing_material() -> bool:
    return bool(_load_private_key())


def build_client_assertion(
    client_id: str,
    token_url: str,
    private_key: str | None = None,
    ttl_seconds: int = 300,
) -> str:
    key = private_key or _load_private_key()
    if not key:
        raise ProviderConfigError(
            "Set SAFEHAVEN_PRIVATE_KEY or SAFEHAVEN_PRIVATE_KEY_PATH for JWT auth"
        )
    if not client_id:
        raise ProviderConfigError("SAFEHAVEN_CLIENT_ID is required for JWT auth")

    now = int(time.time())
    payload = {
        "iss": client_id,
        "sub": client_id,
        "aud": token_url,
        "iat": now,
        "exp": now + ttl_seconds,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, key, algorithm="RS256")