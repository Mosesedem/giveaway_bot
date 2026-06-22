"""
SQLAlchemy ORM models, backed by Postgres in production (and SQLite for
local dev if you don't want to spin up Postgres immediately).

Tables:
  cursors           - since_id per stream (carried over from state_store.py)
  processed_tweets  - dedup for anything we've already acted on
  sent_dms          - dedup for DMs (winner notices, etc.)
  giveaways         - one row per giveaway campaign (new — dashboard needs this)
  entries           - one row per valid entrant for a giveaway (new)
  winners           - one row per selected winner + payout status (new)
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    String, Text, DateTime, ForeignKey, Enum, Integer, Boolean, func
)
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, mapped_column, relationship
)


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


# ============================================================
# Cursor / dedup tables (same job as the old state_store.py)
# ============================================================
class Cursor(Base):
    __tablename__ = "cursors"

    stream_key: Mapped[str] = mapped_column(String, primary_key=True)
    since_id: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ProcessedTweet(Base):
    __tablename__ = "processed_tweets"

    tweet_id: Mapped[str] = mapped_column(String, primary_key=True)
    context: Mapped[str] = mapped_column(String, nullable=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SentDM(Base):
    __tablename__ = "sent_dms"

    dedup_key: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ============================================================
# Giveaway domain tables (new — needed for the dashboard)
# ============================================================
class GiveawayStatus(str, enum.Enum):
    DRAFT = "draft"
    AWAITING_FUNDING = "awaiting_funding"
    ACTIVE = "active"
    CLOSED = "closed"
    WINNERS_SELECTED = "winners_selected"
    COMPLETE = "complete"


class FundingStatus(str, enum.Enum):
    NOT_REQUIRED = "not_required"
    AWAITING_PAYMENT = "awaiting_payment"
    FUNDED = "funded"
    EXPIRED = "expired"
    UNDERPAID = "underpaid"
    OVERPAID = "overpaid"
    PENDING_HOST_CONFIRMATION = "pending_host_confirmation"


class RefundStatus(str, enum.Enum):
    NOT_REQUIRED = "not_required"
    COLLECTING_BANK = "collecting_bank"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class PayoutStatus(str, enum.Enum):
    NOT_STARTED = "not_started"
    COLLECTING_DETAILS = "collecting_details"
    VERIFYING = "verifying"
    READY = "ready"
    PROCESSING = "processing"
    PAID = "paid"
    FAILED = "failed"


class Giveaway(Base):
    __tablename__ = "giveaways"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    host_tweet_id: Mapped[str] = mapped_column(String, nullable=True, index=True)
    host_user_id: Mapped[str] = mapped_column(String, nullable=True)
    host_username: Mapped[str] = mapped_column(String, nullable=True)
    conversation_id: Mapped[str] = mapped_column(String, nullable=True, index=True)
    title: Mapped[str] = mapped_column(String, default="Untitled giveaway")
    prize_description: Mapped[str] = mapped_column(Text, nullable=True)
    num_winners: Mapped[int] = mapped_column(Integer, default=1)
    pick_seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Prize pool (kobo) before platform fee. Host-facing "giveaway amount".
    prize_pool_kobo: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transaction_fee_kobo: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Total host must transfer (prize_pool + fee).
    amount_kobo: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount_received_kobo: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Per-giveaway VA account debited for winner payouts after funding.
    payout_source_account: Mapped[str] = mapped_column(String, nullable=True)
    funding_status: Mapped[FundingStatus] = mapped_column(
        Enum(FundingStatus), default=FundingStatus.NOT_REQUIRED
    )
    payment_provider: Mapped[str] = mapped_column(String, default="safehaven")
    va_provider_id: Mapped[str] = mapped_column(String, nullable=True)
    va_account_number: Mapped[str] = mapped_column(String, nullable=True)
    va_bank_name: Mapped[str] = mapped_column(String, nullable=True)
    va_account_name: Mapped[str] = mapped_column(String, nullable=True)
    va_external_reference: Mapped[str] = mapped_column(String, nullable=True, index=True)
    va_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    funded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[GiveawayStatus] = mapped_column(
        Enum(GiveawayStatus), default=GiveawayStatus.DRAFT
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closes_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    selection_notified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    refund_amount_kobo: Mapped[int | None] = mapped_column(Integer, nullable=True)
    refund_status: Mapped[RefundStatus] = mapped_column(
        Enum(RefundStatus), default=RefundStatus.NOT_REQUIRED
    )
    refund_reference: Mapped[str] = mapped_column(String, nullable=True)
    refund_bank_code: Mapped[str] = mapped_column(String, nullable=True)
    refund_account_number: Mapped[str] = mapped_column(String, nullable=True)
    refund_account_name: Mapped[str] = mapped_column(String, nullable=True)

    entries: Mapped[list["Entry"]] = relationship(back_populates="giveaway", cascade="all, delete-orphan")
    winners: Mapped[list["Winner"]] = relationship(back_populates="giveaway", cascade="all, delete-orphan")


class Entry(Base):
    __tablename__ = "entries"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    giveaway_id: Mapped[str] = mapped_column(String, ForeignKey("giveaways.id"), index=True)
    tweet_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    username: Mapped[str] = mapped_column(String, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=True)
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True)
    invalid_reason: Mapped[str] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    giveaway: Mapped["Giveaway"] = relationship(back_populates="entries")


class WinnerStatus(str, enum.Enum):
    SELECTED = "selected"
    NOTIFIED = "notified"
    DM_FAILED = "dm_failed"
    CONFIRMED = "confirmed"
    PAID = "paid"


class Winner(Base):
    __tablename__ = "winners"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    giveaway_id: Mapped[str] = mapped_column(String, ForeignKey("giveaways.id"), index=True)
    entry_id: Mapped[str] = mapped_column(String, ForeignKey("entries.id"))
    user_id: Mapped[str] = mapped_column(String, index=True)
    username: Mapped[str] = mapped_column(String, nullable=True)
    status: Mapped[WinnerStatus] = mapped_column(Enum(WinnerStatus), default=WinnerStatus.SELECTED)
    selected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    notified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=True)
    payout_status: Mapped[PayoutStatus] = mapped_column(
        Enum(PayoutStatus), default=PayoutStatus.NOT_STARTED
    )
    payout_amount_kobo: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bank_code: Mapped[str] = mapped_column(String, nullable=True)
    bank_name: Mapped[str] = mapped_column(String, nullable=True)
    account_number: Mapped[str] = mapped_column(String, nullable=True)
    account_name: Mapped[str] = mapped_column(String, nullable=True)
    name_enquiry_ref: Mapped[str] = mapped_column(String, nullable=True)
    payout_reference: Mapped[str] = mapped_column(String, nullable=True, index=True)
    payout_provider: Mapped[str] = mapped_column(String, nullable=True)
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    giveaway: Mapped["Giveaway"] = relationship(back_populates="winners")


class ConversationKind(str, enum.Enum):
    GIVEAWAY_SETUP = "giveaway_setup"
    WINNER_PAYOUT = "winner_payout"
    HOST_FUNDING = "host_funding"


class ConversationSession(Base):
    __tablename__ = "conversation_sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    kind: Mapped[ConversationKind] = mapped_column(Enum(ConversationKind))
    user_id: Mapped[str] = mapped_column(String, index=True)
    thread_tweet_id: Mapped[str] = mapped_column(String, index=True)
    state: Mapped[str] = mapped_column(String, default="start")
    draft_json: Mapped[str] = mapped_column(Text, default="{}")
    related_giveaway_id: Mapped[str] = mapped_column(String, ForeignKey("giveaways.id"), nullable=True)
    related_winner_id: Mapped[str] = mapped_column(String, ForeignKey("winners.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class PaymentEvent(Base):
    __tablename__ = "payment_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    provider: Mapped[str] = mapped_column(String, index=True)
    event_type: Mapped[str] = mapped_column(String)
    external_reference: Mapped[str] = mapped_column(String, nullable=True, index=True)
    payment_reference: Mapped[str] = mapped_column(String, nullable=True, index=True)
    giveaway_id: Mapped[str] = mapped_column(String, ForeignKey("giveaways.id"), nullable=True, index=True)
    winner_id: Mapped[str] = mapped_column(String, ForeignKey("winners.id"), nullable=True)
    amount_kobo: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DMQueueStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SENT = "sent"
    FAILED = "failed"


class DMQueueItem(Base):
    __tablename__ = "dm_queue"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    winner_id: Mapped[str] = mapped_column(String, ForeignKey("winners.id"), index=True)
    giveaway_id: Mapped[str] = mapped_column(String, ForeignKey("giveaways.id"), index=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[DMQueueStatus] = mapped_column(Enum(DMQueueStatus), default=DMQueueStatus.PENDING)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    last_error: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
