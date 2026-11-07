"""Dedicated worker process: `python -m app.worker`.

Runs the collection/decision loop, liquidation websockets, candle store and outcome
evaluation. Use with BTCS_RUN_WORKER_IN_API=false on the API containers so exactly one
process writes signals.
"""
from __future__ import annotations

import asyncio
import logging
import signal

from app.config import get_settings
from app.db.session import Database
from app.service import SignalService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def main() -> None:
    settings = get_settings()
    db = Database(settings.database_url)
    db.create_all()
    svc = SignalService(settings, db)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass
    svc.start()
    try:
        await stop.wait()
    finally:
        await svc.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
