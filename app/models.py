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
    ACTIVE = "active"
    CLOSED = "closed"
    WINNERS_SELECTED = "winners_selected"
    COMPLETE = "complete"


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
    status: Mapped[GiveawayStatus] = mapped_column(
        Enum(GiveawayStatus), default=GiveawayStatus.DRAFT
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closes_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

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

    giveaway: Mapped["Giveaway"] = relationship(back_populates="winners")
