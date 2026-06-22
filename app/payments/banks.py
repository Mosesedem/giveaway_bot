"""Nigerian bank code resolution (aliases + SafeHaven list cache)."""

import logging
import threading
import time

from app.payments.safehaven import SafeHavenClient

logger = logging.getLogger(__name__)

# Common NUBAN bank codes (extend as needed; SafeHaven list is authoritative when live).
BANK_ALIASES: dict[str, str] = {
    "gtb": "058",
    "gtbank": "058",
    "guaranty": "058",
    "guaranty trust": "058",
    "zenith": "057",
    "access": "044",
    "uba": "033",
    "united bank": "033",
    "first": "011",
    "firstbank": "011",
    "first bank": "011",
    "fidelity": "070",
    "stanbic": "221",
    "stanbic ibtc": "221",
    "union": "032",
    "union bank": "032",
    "sterling": "232",
    "fcmb": "214",
    "wema": "035",
    "polaris": "076",
    "keystone": "082",
    "ecobank": "050",
    "heritage": "030",
    "unity": "215",
    "jaiz": "301",
    "providus": "101",
    "kuda": "50211",
    "opay": "999992",
    "palmpay": "999991",
    "moniepoint": "50515",
    "lotus": "303",
    "suntrust": "100",
    "titan": "102",
    "safe haven": "999240",
    "safehaven": "999240",
}

_cache_lock = threading.Lock()
_cache_banks: list[dict[str, str]] = []
_cache_at: float = 0.0
_CACHE_TTL = 3600.0


def _refresh_cache() -> list[dict[str, str]]:
    global _cache_banks, _cache_at
    client = SafeHavenClient()
    if not client.configured() or client.mock:
        return [{"code": code, "name": alias} for alias, code in BANK_ALIASES.items()]
    try:
        banks = client.list_banks()
        with _cache_lock:
            _cache_banks = banks
            _cache_at = time.time()
        return banks
    except Exception as exc:
        logger.warning("Could not refresh bank list: %s", exc)
        return _cache_banks


def list_banks() -> list[dict[str, str]]:
    if time.time() - _cache_at < _CACHE_TTL and _cache_banks:
        return _cache_banks
    return _refresh_cache()


def resolve_bank_code(bank_name: str) -> str | None:
    name = bank_name.strip().lower()
    for alias, code in sorted(BANK_ALIASES.items(), key=lambda x: -len(x[0])):
        if alias in name:
            return code
    for bank in list_banks():
        bank_label = bank.get("name", "").lower()
        if name in bank_label or bank_label in name:
            return bank.get("code")
    return None