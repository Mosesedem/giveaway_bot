"""Process-wide runtime flags updated by the scheduler and startup."""

from datetime import datetime, timezone

last_cycle_at: datetime | None = None
last_cycle_summary: dict | None = None
last_cycle_error: str | None = None


def mark_cycle_success(summary: dict) -> None:
    global last_cycle_at, last_cycle_summary, last_cycle_error
    last_cycle_at = datetime.now(timezone.utc)
    last_cycle_summary = summary
    last_cycle_error = None


def mark_cycle_failure(error: str) -> None:
    global last_cycle_at, last_cycle_error
    last_cycle_at = datetime.now(timezone.utc)
    last_cycle_error = error