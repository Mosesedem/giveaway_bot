"""
Background job scheduler for the bot loop.

Runs inside the same process as the FastAPI app (single Render web
service for now). When you outgrow that, split this into its own
worker process/service — the code doesn't need to change, just how
it's started.
"""

import logging
from apscheduler.schedulers.background import BackgroundScheduler

from app.db import SessionLocal
from app.x_client import XClient
from app.bot_logic import run_cycle

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
_client: XClient | None = None


def _job():
    db = SessionLocal()
    try:
        client = get_client()
        summary = run_cycle(db, client)
        logger.info(f"Bot cycle complete: {summary}")
    except Exception:
        logger.exception("Bot cycle failed")
    finally:
        db.close()


def get_client() -> XClient:
    global _client
    if _client is None:
        from app.state_store import StateStore
        _client = XClient(state_store=StateStore())
    return _client


def start_scheduler(interval_seconds: int = 90):
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(_job, "interval", seconds=interval_seconds, id="bot_cycle", max_instances=1)
    _scheduler.start()
    logger.info(f"Scheduler started, bot cycle every {interval_seconds}s")
    return _scheduler


def stop_scheduler():
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
