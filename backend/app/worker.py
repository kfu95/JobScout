import asyncio
import logging
import os
from .db import SessionLocal, init_db
from .collectors import collect_all
from .gmail_sync import sync_gmail
from .gmail_auth import token_path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("job-agent-worker")

async def main():
    init_db()
    seconds = max(300, int(os.getenv("POLL_SECONDS", "7200")))
    while True:
        try:
            with SessionLocal() as db:
                results = await collect_all(db)
            log.info("Collection results: %s", results)
        except Exception:
            log.exception("Collection cycle failed; will retry")
        if token_path().exists():
            try:
                with SessionLocal() as db:
                    log.info("Gmail sync: %s", sync_gmail(db))
            except Exception:
                log.exception("Gmail sync failed; will retry")
        await asyncio.sleep(seconds)

if __name__ == "__main__":
    asyncio.run(main())
