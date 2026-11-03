"""The ingest worker: drains the bus, nothing else.

    python -m app.worker [--workers 8]

Run as its own deployment when the API replicas should only accept (set
INGEST_WORKERS=0 on them and REDIS_URL on both). Scales independently: queue
depth up, workers up. Shuts down cleanly on SIGTERM, finishing what it took.
"""
from __future__ import annotations

import argparse
import asyncio
import signal

from app import taxonomy
from app.core import ingest
from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db import session as db

configure_logging(settings.log_level, json_logs=settings.is_production)
log = get_logger("worker")


async def main(workers: int) -> None:
    if not settings.redis_url:
        log.warning("worker_without_redis",
                    note="memory backend: this process only drains what it accepted itself")
    await db.connect()
    await taxonomy.load()
    await ingest.start(workers)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass
    log.info("worker_ready", workers=workers)
    await stop.wait()
    await ingest.stop(drain_s=20)
    await db.disconnect()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=max(1, settings.ingest_workers))
    asyncio.run(main(ap.parse_args().workers))
