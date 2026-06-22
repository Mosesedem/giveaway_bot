"""Runtime payment settings (DB-backed + env defaults)."""

import os
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import SystemSetting

FEE_MODES = ("fixed", "percent", "percent_plus_fixed")


@dataclass
class FeeConfig:
    mode: str
    fixed_kobo: int
    percent: float

    def describe(self) -> str:
        if self.mode == "fixed":
            return f"₦{self.fixed_kobo / 100:,.2f} fixed"
        if self.mode == "percent":
            return f"{self.percent:g}%"
        return f"{self.percent:g}% + ₦{self.fixed_kobo / 100:,.2f}"


def get_setting(db: Session, key: str, default: str = "") -> str:
    row = db.get(SystemSetting, key)
    return row.value if row else default


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(SystemSetting, key)
    if row is None:
        row = SystemSetting(key=key, value=value)
        db.add(row)
    else:
        row.value = value
    db.commit()


def seed_default_settings(db: Session) -> None:
    defaults = {
        "paystack_enabled": (
            "true" if os.getenv("PAYSTACK_ENABLED", "false").lower() == "true" else "false"
        ),
        "transaction_fee_mode": os.getenv("TRANSACTION_FEE_MODE", "percent_plus_fixed"),
        "transaction_fee_fixed_kobo": os.getenv("TRANSACTION_FEE_KOBO", "20000"),
        "transaction_fee_percent": os.getenv("TRANSACTION_FEE_PERCENT", "2"),
    }
    for key, value in defaults.items():
        if db.get(SystemSetting, key) is None:
            db.add(SystemSetting(key=key, value=value))
    db.commit()


def fee_config(db: Session) -> FeeConfig:
    mode = get_setting(db, "transaction_fee_mode", "percent_plus_fixed")
    if mode not in FEE_MODES:
        mode = "percent_plus_fixed"
    return FeeConfig(
        mode=mode,
        fixed_kobo=max(0, int(get_setting(db, "transaction_fee_fixed_kobo", "20000") or "0")),
        percent=max(0.0, float(get_setting(db, "transaction_fee_percent", "2") or "0")),
    )


def compute_transaction_fee_kobo(prize_pool_kobo: int, db: Session) -> int:
    cfg = fee_config(db)
    if cfg.mode == "fixed":
        return cfg.fixed_kobo
    if cfg.mode == "percent":
        return int(prize_pool_kobo * cfg.percent / 100)
    return cfg.fixed_kobo + int(prize_pool_kobo * cfg.percent / 100)


def paystack_enabled(db: Session) -> bool:
    env_default = os.getenv("PAYSTACK_ENABLED", "false").lower() == "true"
    db_val = get_setting(db, "paystack_enabled", str(env_default).lower())
    return db_val.lower() == "true"


def payout_provider(db: Session) -> str:
    if paystack_enabled(db):
        return "paystack"
    return os.getenv("PAYOUT_PROVIDER", "safehaven")


def funding_provider() -> str:
    return os.getenv("FUNDING_PROVIDER", "safehaven")