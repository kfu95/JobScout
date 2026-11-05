"""Read-only, documented public ATS integrations. No job applications are submitted."""
import hashlib
import html
import json
import logging
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from sqlalchemy import select

from .models import Board, Job, JobVersion, now
from .scoring import score_posting

ALLOWED_PROVIDERS = {"greenhouse", "lever", "ashby"}
SLUG_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{1,150}$")


def plain(value):
    value = html.unescape(html.unescape(str(value or "")))
    value = re.sub(r"(?i)<(br|/p|/li|/div)\b[^>]*>", "\n", value)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"[ \t]+", " ", value).strip()


def normalized_location(locations, workplace_type=""):
    values = []
    seen = set()
    for location in locations:
        value = str(location or "").strip()
        key = value.casefold()
        if value and key not in seen:
            values.append(value)
            seen.add(key)
    if str(workplace_type or "").casefold() == "remote" and "remote" not in seen:
        values.append("Remote")
    return ", ".join(values)


def positive_max(values):
    amounts = [float(value) for value in values if value is not None and float(value) > 0]
    return max(amounts) if amounts else None


def normalize(provider, item):
    if provider == "greenhouse":
        return {
            "external_id": str(item["id"]),
            "title": item.get("title", ""),
            "location": (item.get("location") or {}).get("name", ""),
            "description": plain(item.get("content", "")),
            "apply_url": item.get("absolute_url", ""),
        }
    if provider == "lever":
        categories = item.get("categories") or {}
        salary = item.get("salaryRange") or {}
        salary_max = None
        if str(salary.get("currency") or "").upper() == "USD" and salary.get("interval") == "per-year-salary":
            salary_max = positive_max([salary.get("max")])
        return {
            "external_id": str(item["id"]),
            "title": item.get("text", ""),
            "location": normalized_location(
                categories.get("allLocations") or [categories.get("location")],
                item.get("workplaceType"),
            ),
            "description": plain(item.get("descriptionPlain") or item.get("description") or item.get("lists") or ""),
            "apply_url": item.get("applyUrl") or item.get("hostedUrl") or "",
            "salary_max": salary_max,
        }
    if provider == "ashby":
        components = ((item.get("compensation") or {}).get("summaryComponents") or [])
        annual_usd_salary = [
            component.get("maxValue")
            for component in components
            if str(component.get("compensationType") or "").casefold() == "salary"
            and str(component.get("interval") or "").upper() == "1 YEAR"
            and str(component.get("currencyCode") or "").upper() == "USD"
        ]
        return {
            "external_id": str(item.get("id") or item.get("jobUrl") or item["applyUrl"]),
            "title": item.get("title", ""),
            "location": normalized_location(
                [item.get("location"), *[location.get("location") for location in item.get("secondaryLocations") or []]],
                item.get("workplaceType"),
            ),
            "description": plain(item.get("descriptionPlain") or item.get("descriptionHtml") or ""),
            "apply_url": item.get("applyUrl") or item.get("jobUrl") or "",
            "salary_max": positive_max(annual_usd_salary),
        }
    raise ValueError(f"Unsupported provider: {provider}")


def content_hash(posting):
    keys = {k: posting.get(k, "") for k in ("title", "location", "description", "apply_url")}
    return hashlib.sha256(json.dumps(keys, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def canonical_url(url):
    parsed = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
             if not (k.lower().startswith("utm_") or k.lower() in {"gh_src", "source", "ref", "referrer"})]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), urlencode(sorted(query)), ""))


async def fetch_board(client: httpx.AsyncClient, board: Board):
    from .discovery import polite_get
    if board.provider not in ALLOWED_PROVIDERS or not SLUG_PATTERN.fullmatch(board.slug):
        raise ValueError("Invalid provider or board slug")
    slug = board.slug
    if board.provider == "greenhouse":
        resp = await polite_get(client, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"})
        resp.raise_for_status()
        return [normalize("greenhouse", x) for x in resp.json()["jobs"] if x.get("internal_job_id") is not None]
    if board.provider == "ashby":
        resp = await polite_get(client, f"https://api.ashbyhq.com/posting-api/job-board/{slug}", params={"includeCompensation": "true"})
        resp.raise_for_status()
        return [normalize("ashby", x) for x in resp.json()["jobs"] if x.get("isListed", True)]
    all_items = []
    for offset in range(0, 5000, 100):
        resp = await polite_get(client, f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json", "skip": offset, "limit": 100})
        resp.raise_for_status()
        page = resp.json()
        if not isinstance(page, list):
            raise ValueError("Unexpected Lever response")
        all_items.extend(page)
        if len(page) < 100:
            return [normalize("lever", x) for x in all_items]
    raise ValueError("Lever board exceeds 5,000 postings; refusing partial snapshot")


def persist_snapshot(
    db,
    board: Board,
    postings: list[dict],
    close_missing=True,
    existing_urls=None,
    observed_external_ids=None,
):
    """Apply a COMPLETE successful snapshot. Failed fetches must never close jobs."""
    unique_postings = {p["external_id"]: p for p in postings if p.get("title") and p.get("apply_url")}
    if observed_external_ids is None:
        observed_external_ids = set(unique_postings)
    old_jobs = {j.external_id: j for j in db.scalars(select(Job).where(Job.board_id == board.id)).all()}
    if existing_urls is None:
        existing_urls = {canonical_url(j.apply_url): j.id for j in db.scalars(select(Job)).all()}
    stats = {"new": 0, "updated": 0, "closed": 0, "seen": len(unique_postings)}
    for external_id, p in unique_postings.items():
        digest = content_hash(p)
        job = old_jobs.pop(external_id, None)
        url = canonical_url(p["apply_url"])
        duplicate_id = existing_urls.get(url)
        if duplicate_id is not None and (job is None or duplicate_id != job.id):
            duplicate = db.get(Job, duplicate_id)
            if duplicate is None:
                raise ValueError("Canonical URL index references a missing job")
            duplicate.last_seen = now()
            duplicate.is_open = True
            duplicate.missing_count = 0
            if duplicate.board_id == board.id:
                old_jobs.pop(duplicate.external_id, None)
            if job is not None:
                old_url = canonical_url(job.apply_url)
                if existing_urls.get(old_url) == job.id:
                    del existing_urls[old_url]
                job.is_open = False
                job.missing_count = 0
            continue
        if job is None:
            score, reason, method = score_posting(p)
            job = Job(board_id=board.id, external_id=external_id, title=p["title"], location=p["location"],
                      description=p["description"], apply_url=p["apply_url"], hash=digest,
                      score=score, score_reason=reason, score_method=method)
            db.add(job)
            db.flush()
            existing_urls[url] = job.id
            db.add(JobVersion(job_id=job.id, hash=digest, title=job.title, location=job.location, description=job.description))
            stats["new"] += 1
        else:
            job.last_seen = now()
            job.is_open = True
            job.missing_count = 0
            if job.hash != digest:
                old_url = canonical_url(job.apply_url)
                job.title, job.location, job.description = p["title"], p["location"], p["description"]
                job.apply_url, job.hash = p["apply_url"], digest
                job.score, job.score_reason, job.score_method = score_posting(p)
                if existing_urls.get(old_url) == job.id:
                    del existing_urls[old_url]
                db.add(JobVersion(job_id=job.id, hash=digest, title=job.title, location=job.location, description=job.description))
                stats["updated"] += 1
            existing_urls[url] = job.id
    for external_id in observed_external_ids:
        old_jobs.pop(external_id, None)
    for job in old_jobs.values():
        if close_missing and job.is_open:
            job.missing_count += 1
            if job.missing_count >= 2:
                job.is_open = False
                stats["closed"] += 1
    board.last_success_at = now()
    board.last_error = None
    db.commit()
    return stats


async def collect_all(db):
    from .aggregators import collect_aggregators
    from .discovery import refresh_directory, mark
    from .models import DiscoveryState
    from .preferences import discovery_preferences, matches
    preferences = discovery_preferences()
    results = []
    async with httpx.AsyncClient(timeout=25, follow_redirects=False, headers={"Accept": "application/json", "User-Agent": "PersonalJobTracker/0.1"}) as client:
        try:
            added = await refresh_directory(db, client)
            results.append({"provider": "directory", "ok": True, "added": added})
        except Exception as exc:
            db.rollback()
            logging.getLogger(__name__).exception("Directory refresh failed")
            results.append({"provider": "directory", "ok": False, "error": str(exc)[:250]})
        existing_urls = {canonical_url(j.apply_url): j.id for j in db.scalars(select(Job)).all()}
        results.extend(await collect_aggregators(db, client, preferences, existing_urls))
        state = db.get(DiscoveryState, "board_cursor")
        cursor = int(state.value) if state and state.value else 0
        query = select(Board).where(Board.enabled.is_(True), Board.provider.in_(ALLOWED_PROVIDERS))
        boards = db.scalars(query.where(Board.id > cursor).order_by(Board.id).limit(300)).all()
        if not boards and cursor:
            boards = db.scalars(query.order_by(Board.id).limit(300)).all()
        for board in boards:
            try:
                postings = await fetch_board(client, board)
                eligible = [posting for posting in postings if matches(posting, preferences)]
                observed_ids = {posting["external_id"] for posting in postings}
                stats = persist_snapshot(
                    db,
                    board,
                    eligible,
                    existing_urls=existing_urls,
                    observed_external_ids=observed_ids,
                )
                results.append({"company": board.company, "provider": board.provider, "ok": True, **stats})
            except Exception as e:
                db.rollback()
                board.last_error = str(e)[:700]
                db.add(board)
                db.commit()
                results.append({"company": board.company, "provider": board.provider, "ok": False, "error": str(e)[:250]})
            mark(db, "board_cursor", str(board.id))
    return results
