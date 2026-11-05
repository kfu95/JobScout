import asyncio
import csv
import io
import logging
import random
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from sqlalchemy import select
from .models import Board, DiscoveryState, now

log = logging.getLogger(__name__)
DIRECTORY_URL = "https://raw.githubusercontent.com/fyrosofttech/lastroundai-hiring-data/main/ats-directory/lastroundai-ats-company-directory-2026-08.csv"
PROVIDERS = {"greenhouse", "lever", "ashby"}


def elapsed(state, interval):
    if state is None:
        return True
    stamp = state.updated_at
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return now() - stamp >= interval


def mark(db, key, value=""):
    state = db.get(DiscoveryState, key) or DiscoveryState(key=key)
    state.value = value
    state.updated_at = now()
    db.add(state)
    db.commit()


async def polite_get(client, url, **kwargs):
    await asyncio.sleep(random.uniform(0.8, 1.2))
    response = await client.get(url, **kwargs)
    if response.status_code in (429, 503):
        retry = response.headers.get("Retry-After")
        if retry:
            try:
                delay = float(retry)
            except ValueError:
                delay = (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds()
            if delay > 0:
                await asyncio.sleep(delay)
            response = await client.get(url, **kwargs)
    response.raise_for_status()
    return response


async def refresh_directory(db, client):
    if not elapsed(db.get(DiscoveryState, "directory"), timedelta(days=7)):
        return 0
    response = await polite_get(client, DIRECTORY_URL)
    rows = list(csv.DictReader(io.StringIO(response.text)))
    if not rows or not {"ats_vendor", "company_name", "board_slug"}.issubset(rows[0]):
        raise ValueError("Invalid company directory")
    from .collectors import SLUG_PATTERN
    existing = set(db.execute(select(Board.provider, Board.slug)).all())
    count = 0
    for row in rows:
        provider, slug, company = row["ats_vendor"], row["board_slug"], row["company_name"]
        if provider in PROVIDERS and SLUG_PATTERN.fullmatch(slug) and company and (provider, slug) not in existing:
            db.add(Board(provider=provider, slug=slug, company=company[:200]))
            existing.add((provider, slug))
            count += 1
    db.commit()
    mark(db, "directory")
    return count
