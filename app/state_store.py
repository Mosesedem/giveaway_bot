"""
Postgres-backed replacement for the original SQLite state_store.py.

Same public interface (get_since_id, set_since_id, is_processed,
mark_processed, filter_unprocessed, was_dm_sent, mark_dm_sent) so
x_client.py needs zero changes — just import this instead.
"""

from typing import Optional
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.models import Cursor, ProcessedTweet, SentDM
from app.db import SessionLocal, engine


def _upsert_stmt(table, values, conflict_col, update_cols):
    """Build an ON CONFLICT upsert that works for both Postgres and SQLite."""
    if engine.dialect.name == "postgresql":
        stmt = pg_insert(table).values(**values)
        return stmt.on_conflict_do_update(
            index_elements=[conflict_col], set_={c: getattr(stmt.excluded, c) for c in update_cols}
        )
    else:
        stmt = sqlite_insert(table).values(**values)
        return stmt.on_conflict_do_update(
            index_elements=[conflict_col], set_={c: getattr(stmt.excluded, c) for c in update_cols}
        )


class StateStore:
    """Drop-in replacement for the SQLite StateStore, backed by Postgres."""

    def __init__(self, session_factory=SessionLocal):
        self._session_factory = session_factory

    def _session(self) -> Session:
        return self._session_factory()

    # ------------------------------------------------------------------
    # Cursors
    # ------------------------------------------------------------------
    def get_since_id(self, stream_key: str) -> Optional[str]:
        with self._session() as db:
            row = db.execute(select(Cursor).where(Cursor.stream_key == stream_key)).scalar_one_or_none()
            return row.since_id if row else None

    def set_since_id(self, stream_key: str, since_id: str) -> None:
        with self._session() as db:
            stmt = _upsert_stmt(
                Cursor.__table__,
                {"stream_key": stream_key, "since_id": since_id},
                conflict_col="stream_key",
                update_cols=["since_id"],
            )
            db.execute(stmt)
            db.commit()

    # ------------------------------------------------------------------
    # Processed tweet dedup
    # ------------------------------------------------------------------
    def is_processed(self, tweet_id: str) -> bool:
        with self._session() as db:
            row = db.execute(
                select(ProcessedTweet).where(ProcessedTweet.tweet_id == tweet_id)
            ).scalar_one_or_none()
            return row is not None

    def mark_processed(self, tweet_id: str, context: str = "") -> None:
        with self._session() as db:
            existing = db.get(ProcessedTweet, tweet_id)
            if existing is None:
                db.add(ProcessedTweet(tweet_id=tweet_id, context=context))
                db.commit()

    def filter_unprocessed(self, tweet_ids: list[str]) -> list[str]:
        if not tweet_ids:
            return []
        with self._session() as db:
            rows = db.execute(
                select(ProcessedTweet.tweet_id).where(ProcessedTweet.tweet_id.in_(tweet_ids))
            ).scalars().all()
            already_done = set(rows)
        return [t for t in tweet_ids if t not in already_done]

    # ------------------------------------------------------------------
    # DM dedup
    # ------------------------------------------------------------------
    def was_dm_sent(self, dedup_key: str) -> bool:
        with self._session() as db:
            row = db.execute(select(SentDM).where(SentDM.dedup_key == dedup_key)).scalar_one_or_none()
            return row is not None

    def mark_dm_sent(self, dedup_key: str, user_id: str) -> None:
        with self._session() as db:
            existing = db.get(SentDM, dedup_key)
            if existing is None:
                db.add(SentDM(dedup_key=dedup_key, user_id=user_id))
                db.commit()
