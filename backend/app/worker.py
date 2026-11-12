import asyncio
import logging
import os
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import SessionLocal, init_db
from .collectors import collect_all
from .gmail_sync import sync_gmail
from .gmail_auth import token_path
from .digest import send_digest
from .onboarding import storage_dir
from .scoring import load_profile

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("job-agent-worker")
SCHEDULE_ZONE = ZoneInfo(os.getenv("TZ", "UTC"))

def seconds_until_gmail_sync(now=None):
    zone = now.tzinfo if now is not None else SCHEDULE_ZONE
    now = now or datetime.now(zone)
    if now.tzinfo is None:
        raise ValueError("Gmail schedule requires a timezone-aware datetime")
    local_now = now.astimezone(zone)
    next_run = datetime.combine(local_now.date(), time(hour=6), tzinfo=zone)
    if local_now > next_run:
        next_run += timedelta(days=1)
    return max(
        0,
        (next_run.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds(),
    )

async def collection_loop(seconds):
    while True:
        if not (storage_dir() / "preferences.json").exists() and not load_profile().get("target_titles"):
            log.info("Waiting for job preferences before collecting")
            await asyncio.sleep(60)
            continue
        try:
            with SessionLocal() as db:
                results = await collect_all(db)
            log.info("Collection results: %s", results)
        except Exception:
            log.exception("Collection cycle failed; will retry")
        await asyncio.sleep(seconds)

def run_gmail_sync():
    with SessionLocal() as db:
        return sync_gmail(db)

async def gmail_loop():
    while True:
        await asyncio.sleep(seconds_until_gmail_sync())
        if token_path().exists():
            try:
                result = await asyncio.to_thread(run_gmail_sync)
                log.info("Gmail sync: %s", result)
            except Exception:
                log.exception("Gmail sync failed; will retry at the next daily run")

def seconds_until_digest(now=None):
    zone = ZoneInfo(os.getenv("DIGEST_TIMEZONE", "UTC"))
    hour, minute = map(int, os.getenv("DIGEST_TIME", "08:00").split(":"))
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Digest schedule requires a timezone-aware datetime")
    local = now.astimezone(zone)
    target = datetime.combine(local.date(), time(hour, minute), tzinfo=zone)
    if local >= target:
        target += timedelta(days=1)
    return (target.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()


def run_digest():
    with SessionLocal() as db:
        return send_digest(db)


async def digest_loop():
    while True:
        await asyncio.sleep(seconds_until_digest())
        try:
            log.info("Digest: %s", await asyncio.to_thread(run_digest))
        except Exception:
            log.exception("Digest failed; will retry at the next daily run")


async def main():
    init_db()
    seconds = max(300, int(os.getenv("POLL_SECONDS", "7200")))
    await asyncio.gather(collection_loop(seconds), gmail_loop(), digest_loop())

if __name__ == "__main__":
    asyncio.run(main())
