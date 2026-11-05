"""Read-only, documented public ATS integrations. No job applications are submitted."""
import hashlib
import html
import json
import re
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
        return {
            "external_id": str(item["id"]),
            "title": item.get("text", ""),
            "location": categories.get("location") or ", ".join(categories.get("allLocations") or []) or "",
            "description": plain(item.get("descriptionPlain") or item.get("description") or item.get("lists") or ""),
            "apply_url": item.get("applyUrl") or item.get("hostedUrl") or "",
        }
    if provider == "ashby":
        return {
            "external_id": str(item.get("id") or item.get("jobUrl") or item["applyUrl"]),
            "title": item.get("title", ""),
            "location": item.get("location", "") + (" / Remote" if item.get("isRemote") else ""),
            "description": plain(item.get("descriptionPlain") or item.get("descriptionHtml") or ""),
            "apply_url": item.get("applyUrl") or item.get("jobUrl") or "",
        }
    raise ValueError(f"Unsupported provider: {provider}")


def content_hash(posting):
    keys = {k: posting.get(k, "") for k in ("title", "location", "description", "apply_url")}
    return hashlib.sha256(json.dumps(keys, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def fetch_board(client: httpx.AsyncClient, board: Board):
    if board.provider not in ALLOWED_PROVIDERS or not SLUG_PATTERN.fullmatch(board.slug):
        raise ValueError("Invalid provider or board slug")
    slug = board.slug
    if board.provider == "greenhouse":
        resp = await client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"})
        resp.raise_for_status()
        return [normalize("greenhouse", x) for x in resp.json()["jobs"] if x.get("internal_job_id") is not None]
    if board.provider == "ashby":
        resp = await client.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", params={"includeCompensation": "true"})
        resp.raise_for_status()
        return [normalize("ashby", x) for x in resp.json()["jobs"] if x.get("isListed", True)]
    all_items = []
    for offset in range(0, 5000, 100):
        resp = await client.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json", "skip": offset, "limit": 100})
        resp.raise_for_status()
        page = resp.json()
        if not isinstance(page, list):
            raise ValueError("Unexpected Lever response")
        all_items.extend(page)
        if len(page) < 100:
            return [normalize("lever", x) for x in all_items]
    raise ValueError("Lever board exceeds 5,000 postings; refusing partial snapshot")


def persist_snapshot(db, board: Board, postings: list[dict]):
    """Apply a COMPLETE successful snapshot. Failed fetches must never close jobs."""
    unique_postings = {p["external_id"]: p for p in postings if p.get("title") and p.get("apply_url")}
    old_jobs = {j.external_id: j for j in db.scalars(select(Job).where(Job.board_id == board.id)).all()}
    stats = {"new": 0, "updated": 0, "closed": 0, "seen": len(unique_postings)}
    for external_id, p in unique_postings.items():
        digest = content_hash(p)
        job = old_jobs.pop(external_id, None)
        if job is None:
            score, reason, method = score_posting(p)
            job = Job(board_id=board.id, external_id=external_id, title=p["title"], location=p["location"],
                      description=p["description"], apply_url=p["apply_url"], hash=digest,
                      score=score, score_reason=reason, score_method=method)
            db.add(job)
            db.flush()
            db.add(JobVersion(job_id=job.id, hash=digest, title=job.title, location=job.location, description=job.description))
            stats["new"] += 1
        else:
            job.last_seen = now()
            job.is_open = True
            job.missing_count = 0
            if job.hash != digest:
                job.title, job.location, job.description = p["title"], p["location"], p["description"]
                job.apply_url, job.hash = p["apply_url"], digest
                job.score, job.score_reason, job.score_method = score_posting(p)
                db.add(JobVersion(job_id=job.id, hash=digest, title=job.title, location=job.location, description=job.description))
                stats["updated"] += 1
    for job in old_jobs.values():
        if job.is_open:
            job.missing_count += 1
            if job.missing_count >= 2:
                job.is_open = False
                stats["closed"] += 1
    board.last_success_at = now()
    board.last_error = None
    db.commit()
    return stats


async def collect_all(db):
    boards = db.scalars(select(Board).where(Board.enabled.is_(True)).order_by(Board.id)).all()
    results = []
    async with httpx.AsyncClient(timeout=25, follow_redirects=False, headers={"Accept": "application/json", "User-Agent": "PersonalJobTracker/0.1"}) as client:
        for board in boards:
            try:
                postings = await fetch_board(client, board)
                stats = persist_snapshot(db, board, postings)
                results.append({"company": board.company, "provider": board.provider, "ok": True, **stats})
            except Exception as e:
                db.rollback()
                board.last_error = str(e)[:700]
                db.add(board)
                db.commit()
                results.append({"company": board.company, "provider": board.provider, "ok": False, "error": str(e)[:250]})
    return results
