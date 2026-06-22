"""
Configurable entry validation rules for giveaway replies.

Rules are driven by environment variables so you can tighten requirements
without code changes. Validation runs during entry collection.
"""

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from app.models import Giveaway
from app.x_client import XClient
from app.x_exceptions import XClientError


@dataclass
class ValidationResult:
    is_valid: bool
    reason: str | None = None
    username: str | None = None


def validation_rules_enabled() -> bool:
    cfg = validation_config()
    return any(
        [
            cfg["require_follow_host"],
            cfg["require_follow_bot"],
            cfg["min_account_age_days"] > 0,
            cfg["min_followers"] > 0,
            bool(cfg["entry_keyword"]),
        ]
    )


def validation_config() -> dict:
    return {
        "require_follow_host": os.getenv("REQUIRE_FOLLOW_HOST", "false").lower() == "true",
        "require_follow_bot": os.getenv("REQUIRE_FOLLOW_BOT", "false").lower() == "true",
        "min_account_age_days": int(os.getenv("MIN_ACCOUNT_AGE_DAYS", "0") or "0"),
        "min_followers": int(os.getenv("MIN_FOLLOWERS", "0") or "0"),
        "entry_keyword": os.getenv("REQUIRE_ENTRY_KEYWORD", "").strip(),
    }


def validate_entry(
    client: XClient,
    giveaway: Giveaway,
    author_id: str,
    text: str,
) -> ValidationResult:
    """Apply all enabled validation rules to a candidate reply."""
    cfg = validation_config()
    username: str | None = None
    user_info: dict | None = None

    needs_user_lookup = (
        cfg["min_account_age_days"] > 0
        or cfg["min_followers"] > 0
        or cfg["require_follow_host"]
        or cfg["require_follow_bot"]
    )

    if needs_user_lookup:
        try:
            user_info = client.get_user_by_id(author_id)
            username = user_info.get("username")
        except XClientError as exc:
            return ValidationResult(False, f"could not verify account: {exc}")

    if cfg["entry_keyword"]:
        if cfg["entry_keyword"].lower() not in (text or "").lower():
            return ValidationResult(False, f"missing keyword '{cfg['entry_keyword']}'", username)

    if user_info and cfg["min_followers"] > 0:
        followers = user_info.get("followers_count", 0)
        if followers < cfg["min_followers"]:
            return ValidationResult(
                False,
                f"needs {cfg['min_followers']}+ followers (has {followers})",
                username,
            )

    if user_info and cfg["min_account_age_days"] > 0:
        created_at = user_info.get("created_at")
        if created_at is None:
            return ValidationResult(False, "account age could not be verified", username)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - created_at).days
        if age_days < cfg["min_account_age_days"]:
            return ValidationResult(
                False,
                f"account too new ({age_days}d, need {cfg['min_account_age_days']}d)",
                username,
            )

    if cfg["require_follow_host"]:
        if not giveaway.host_user_id:
            return ValidationResult(False, "giveaway has no host_user_id for follow check", username)
        try:
            if not client.user_follows(author_id, giveaway.host_user_id):
                return ValidationResult(False, "must follow the host", username)
        except XClientError as exc:
            return ValidationResult(False, f"follow check failed: {exc}", username)

    if cfg["require_follow_bot"]:
        bot_id = str(client.get_bot_identity()["user_id"])
        try:
            if not client.user_follows(author_id, bot_id):
                return ValidationResult(False, "must follow the bot", username)
        except XClientError as exc:
            return ValidationResult(False, f"bot follow check failed: {exc}", username)

    return ValidationResult(True, None, username)