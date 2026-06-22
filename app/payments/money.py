"""NGN amount parsing, fee calculation, and duration parsing."""

import os
import re
from datetime import datetime, timedelta, timezone

_AMOUNT_RE = re.compile(
    r"(?:₦|ngn|ngn\s*|n\s*)?"
    r"(\d+(?:,\d{3})*(?:\.\d{1,2})?)"
    r"\s*(k|thousand|m|million)?",
    re.IGNORECASE,
)


def parse_ngn_to_kobo(text: str) -> int | None:
    """Parse '50000', '₦50k', '50 thousand', '1.5m' into kobo."""
    if not text:
        return None
    match = _AMOUNT_RE.search(text.replace(" ", ""))
    if not match:
        return None
    raw = match.group(1).replace(",", "")
    amount = float(raw)
    suffix = (match.group(2) or "").lower()
    if suffix in {"k", "thousand"}:
        amount *= 1_000
    elif suffix in {"m", "million"}:
        amount *= 1_000_000
    if amount <= 0:
        return None
    return int(round(amount * 100))


def format_ngn(kobo: int) -> str:
    naira = kobo / 100
    return f"₦{naira:,.2f}"


def transaction_fee_from_config(prize_pool_kobo: int, mode: str, fixed_kobo: int, percent: float) -> int:
    """Compute fee from explicit config (used by settings module and tests)."""
    if mode == "fixed":
        return fixed_kobo
    if mode == "percent":
        return int(prize_pool_kobo * percent / 100)
    return fixed_kobo + int(prize_pool_kobo * percent / 100)


_DURATION_RE = re.compile(
    r"(?:duration|closes?|ends?|run)\s*[:=]?\s*"
    r"(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|h|weeks?|w|minutes?|mins?|m)\b",
    re.I,
)
_SHORT_DURATION_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|h|weeks?|w)\b",
    re.I,
)


def _duration_to_seconds(value: float, unit: str) -> int:
    u = unit.lower().rstrip("s")
    if u in {"day", "d"}:
        return int(value * 86400)
    if u in {"hour", "hr", "h"}:
        return int(value * 3600)
    if u in {"week", "w"}:
        return int(value * 7 * 86400)
    if u in {"minute", "min", "m"}:
        return int(value * 60)
    return int(value * 86400)


def parse_duration_seconds(text: str) -> int | None:
    """Parse 'duration: 7 days', '48h', 'closes: 3d' into seconds."""
    if not text:
        return None
    match = _DURATION_RE.search(text) or _SHORT_DURATION_RE.search(text)
    if not match:
        return None
    seconds = _duration_to_seconds(float(match.group(1)), match.group(2))
    return seconds if seconds >= 3600 else None


def closes_at_from_duration(duration_seconds: int, base: datetime | None = None) -> datetime:
    start = base or datetime.now(timezone.utc)
    return start + timedelta(seconds=duration_seconds)


def prize_per_winner(pool_kobo: int, num_winners: int) -> int:
    if num_winners < 1 or pool_kobo <= 0:
        return 0
    return pool_kobo // num_winners