import asyncio
import logging
import os
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import SessionLocal, init_db
from .collectors import collect_all
from .gmail_sync import sync_gmail
from .gmail_auth import token_path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("job-agent-worker")
EASTERN = ZoneInfo("America/New_York")

def seconds_until_gmail_sync(now=None):
    now = now or datetime.now(EASTERN)
    if now.tzinfo is None:
        raise ValueError("Gmail schedule requires a timezone-aware datetime")
    local_now = now.astimezone(EASTERN)
    next_run = datetime.combine(local_now.date(), time(hour=6), tzinfo=EASTERN)
    if local_now > next_run:
        next_run += timedelta(days=1)
    return max(
        0,
        (next_run.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds(),
    )

async def collection_loop(seconds):
    while True:
        try:
            with SessionLocal() as db:
                results = await collect_all(db)
            log.info("Collection results: %s", results)
        except Exception:
            log.exception("Collection cycle failed; will retry")
        await asyncio.sleep(seconds)

async def gmail_loop():
    while True:
        await asyncio.sleep(seconds_until_gmail_sync())
        if token_path().exists():
            try:
                with SessionLocal() as db:
                    log.info("Gmail sync: %s", sync_gmail(db))
            except Exception:
                log.exception("Gmail sync failed; will retry at the next daily run")

async def main():
    init_db()
    seconds = max(300, int(os.getenv("POLL_SECONDS", "7200")))
    await asyncio.gather(collection_loop(seconds), gmail_loop())

if __name__ == "__main__":
    asyncio.run(main())
