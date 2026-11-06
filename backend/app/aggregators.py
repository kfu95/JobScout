import hashlib
import logging
import os
import re
from datetime import timedelta

from sqlalchemy import select
from .discovery import elapsed, mark, polite_get
from .models import Board, DiscoveryState
from .collectors import canonical_url, existing_url_index, persist_snapshot, plain
from .preferences import matches

log = logging.getLogger(__name__)


def remotive_salary_max(text):
    text = str(text or "")
    if re.search(r"(?i)\b(?:hourly?|hrs?|daily|days?|weekly|weeks?|monthly|months?)\b|/(?:hr|day|week|month)\b", text):
        return None
    amount_pattern = r"(\d[\d,]*(?:\.\d+)?)\s*(k?)"
    separator = r"(?:-|–|—|to)"
    amounts = []
    usd_patterns = (
        rf"\$\s*{amount_pattern}(?:\s*{separator}\s*\$?\s*{amount_pattern})?",
        rf"\bUSD\b(?:\s+equivalent)?\s*:?\s*\$?\s*{amount_pattern}(?:\s*{separator}\s*\$?\s*{amount_pattern})?",
        rf"{amount_pattern}(?:\s*{separator}\s*{amount_pattern})?\s*\bUSD\b",
    )
    for pattern in usd_patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            groups = match.groups()
            for index in range(0, len(groups), 2):
                if groups[index]:
                    amounts.append(float(groups[index].replace(",", "")) * (1000 if groups[index + 1] else 1))
    amounts = [amount for amount in amounts if amount >= 1000]
    return max(amounts) if amounts else None


def remotive_posting(item):
    return {"external_id": str(item["id"]), "company": item.get("company_name") or "Unknown",
            "title": item.get("title", ""), "location": "Remote " + item.get("candidate_required_location", ""),
            "description": plain(item.get("description", "")), "apply_url": item["url"],
            "salary_max": remotive_salary_max(item.get("salary")), "remote_region": item.get("candidate_required_location", "")}


def usajobs_posting(item):
    data = item["MatchedObjectDescriptor"]
    pay = data.get("PositionRemuneration") or []
    annual = [float(p["MaximumRange"]) for p in pay if str(p.get("RateIntervalCode") or "").upper() == "PA"
              and p.get("MaximumRange") and float(p["MaximumRange"]) > 0]
    locations = [x.get("LocationName", "") for x in data.get("PositionLocation", [])]
    if data.get("UserArea", {}).get("Details", {}).get("RemoteIndicator"):
        locations.append("Remote")
    return {"external_id": str(item["MatchedObjectId"]), "company": data.get("OrganizationName") or "USAJOBS",
            "title": data.get("PositionTitle", ""), "location": ", ".join(locations),
            "description": plain(data.get("QualificationSummary", "")),
            "apply_url": data["PositionURI"], "salary_max": max(annual) if annual else None}


async def fetch_remotive(client):
    response = await polite_get(client, "https://remotive.com/api/remote-jobs", params={"category": "software-dev"})
    return [remotive_posting(item) for item in response.json()["jobs"]]


async def fetch_usajobs(client, key, email):
    result = []
    for page in range(1, 21):
        response = await polite_get(client, "https://data.usajobs.gov/api/search",
                                    params={"Keyword": "software", "ResultsPerPage": 500, "Page": page},
                                    headers={"Host": "data.usajobs.gov", "User-Agent": email, "Authorization-Key": key})
        search = response.json()["SearchResult"]
        result.extend(usajobs_posting(item) for item in search["SearchResultItems"])
        if len(result) >= int(search["SearchResultCountAll"]):
            return result
    raise ValueError("USAJOBS results exceed the 10,000 result API limit")


def store_aggregator(db, provider, postings, preferences, existing_urls=None):
    if existing_urls is None:
        existing_urls = existing_url_index(db)
    grouped = {}
    for posting in postings:
        grouped.setdefault(posting["company"], []).append(posting)
    stats = {"new": 0, "updated": 0, "seen": 0}
    for company, items in grouped.items():
        slug = hashlib.sha256(company.encode()).hexdigest()
        board = db.scalar(select(Board).where(Board.provider == provider, Board.slug == slug))
        if board is None:
            known_url = any(canonical_url(item["apply_url"]) in existing_urls for item in items)
            if not known_url and not any(matches(item, preferences) for item in items):
                continue
            board = Board(company=company[:200], provider=provider, slug=slug)
            db.add(board)
            db.commit()
        result = persist_snapshot(
            db,
            board,
            items,
            close_missing=False,
            existing_urls=existing_urls,
            preferences=preferences,
        )
        for key in stats:
            stats[key] += result[key]
    return stats


async def collect_aggregators(db, client, preferences, existing_urls=None):
    results = []
    for provider, interval in (("remotive", timedelta(hours=6)), ("usajobs", timedelta(hours=6))):
        if not elapsed(db.get(DiscoveryState, provider), interval):
            continue
        key, email = os.getenv("USAJOBS_API_KEY", ""), os.getenv("USAJOBS_EMAIL", "")
        if provider == "usajobs" and not (key and email):
            log.warning("Skipping USAJOBS: USAJOBS_API_KEY and USAJOBS_EMAIL are required")
            results.append({"provider": provider, "skipped": "missing API key or email"})
            continue
        try:
            mark(db, provider)
            postings = await (fetch_remotive(client) if provider == "remotive" else fetch_usajobs(client, key, email))
            stats = store_aggregator(db, provider, postings, preferences, existing_urls)
            results.append({"provider": provider, "ok": True, **stats})
        except Exception as exc:
            db.rollback()
            log.exception("%s collection failed", provider)
            results.append({"provider": provider, "ok": False, "error": str(exc)[:250]})
    return results
