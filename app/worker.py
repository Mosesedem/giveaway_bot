"""
Standalone bot worker — runs the scheduler without the FastAPI web server.

Use on Render as a separate worker service so polling continues even when
the web dashboard is idle or cold-starting.

    python -m app.worker
"""

import logging
import os
import signal
import time

from app.db import init_db
from app.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> None:
    init_db()
    interval = int(os.getenv("BOT_CYCLE_SECONDS", "90"))
    start_scheduler(interval_seconds=interval)
    logger.info("Giveaway bot worker running (scheduler every %ss)", interval)

    def _shutdown(signum, frame):
        logger.info("Shutting down worker (signal %s)", signum)
        stop_scheduler()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()