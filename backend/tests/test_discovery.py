import asyncio
import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.aggregators import collect_aggregators, fetch_remotive, fetch_usajobs, store_aggregator
from app.collectors import persist_snapshot
from app.db import Base
from app.discovery import refresh_directory
from app.models import Board, Job
from app.preferences import matches

PREFS = {"titles": ["senior software engineer"], "locations": ["new york", "remote"], "salary_floor": 150000}


def run(coro):
    return asyncio.run(coro)


def test_aggregator_responses_and_key_headers(monkeypatch):
    async def no_wait(_):
        pass
    monkeypatch.setattr("app.discovery.asyncio.sleep", no_wait)
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.host == "remotive.com":
            return httpx.Response(200, json={"jobs": [{"id": 4, "company_name": "A", "title": "Senior Software Engineer", "candidate_required_location": "USA", "url": "https://remotive.com/job/4", "salary": "$160,000 - $200,000 per year"}]})
        return httpx.Response(200, json={"SearchResult": {"SearchResultCountAll": 1, "SearchResultItems": [{"MatchedObjectId": "2", "MatchedObjectDescriptor": {"PositionTitle": "Senior Software Engineer", "OrganizationName": "Agency", "PositionURI": "https://usajobs.gov/job/2", "PositionLocation": [{"LocationName": "New York"}], "PositionRemuneration": [{"RateIntervalCode": "Per Year", "MaximumRange": "180000"}]}}]}})
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_remotive(client), await fetch_usajobs(client, "secret", "person@example.com")
    remotive, usa = run(exercise())
    assert matches(remotive[0], PREFS) and matches(usa[0], PREFS)
    assert requests[1].headers["authorization-key"] == "secret"
    assert requests[1].headers["user-agent"] == "person@example.com"
    assert not matches({**remotive[0], "remote_region": "Germany"}, PREFS)
    assert not matches({**usa[0], "salary_max": 100000}, PREFS)


def test_directory_and_cross_source_dedup(monkeypatch):
    async def no_wait(_):
        pass
    monkeypatch.setattr("app.discovery.asyncio.sleep", no_wait)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    async def exercise(db):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, text="ats_vendor,company_name,board_slug,last_crawled\ngreenhouse,Example,example,2026-01-01\n"))) as client:
            assert await refresh_directory(db, client) == 1
            assert await refresh_directory(db, client) == 0
    with Session(engine) as db:
        run(exercise(db))
        board = db.scalar(select(Board))
        posting = {"external_id": "1", "company": "Example", "title": "Senior Software Engineer", "location": "New York", "description": "", "apply_url": "https://example.com/job/1?utm_source=test"}
        persist_snapshot(db, board, [posting])
        store_aggregator(db, "usajobs", [{**posting, "external_id": "99", "apply_url": "https://example.com/job/1"}], PREFS)
        assert len(db.scalars(select(Job)).all()) == 1


def test_missing_usajobs_credentials_are_logged(monkeypatch, caplog):
    monkeypatch.delenv("USAJOBS_API_KEY", raising=False)
    monkeypatch.delenv("USAJOBS_EMAIL", raising=False)
    async def no_wait(_):
        pass
    monkeypatch.setattr("app.discovery.asyncio.sleep", no_wait)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    async def exercise(db):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"jobs": []}))) as client:
            return await collect_aggregators(db, client, PREFS)
    with Session(engine) as db:
        result = run(exercise(db))
    assert any(row.get("skipped") for row in result)
    assert "Skipping USAJOBS" in caplog.text


def test_public_board_endpoints_and_retry_after(monkeypatch):
    from app.collectors import fetch_board
    waits = []
    async def record_wait(seconds):
        waits.append(seconds)
    monkeypatch.setattr("app.discovery.asyncio.sleep", record_wait)
    attempts = 0
    def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        if "greenhouse" in request.url.host:
            return httpx.Response(200, json={"jobs": [{"id": 1, "internal_job_id": 7, "title": "Senior Software Engineer", "location": {"name": "New York"}, "absolute_url": "https://example.com/1"}]})
        if "lever" in request.url.host:
            return httpx.Response(200, json=[{"id": "2", "text": "Senior Software Engineer", "applyUrl": "https://example.com/2"}])
        return httpx.Response(200, json={"jobs": [{"title": "Senior Software Engineer", "jobUrl": "https://example.com/3"}]})
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return [await fetch_board(client, Board(provider=p, slug="example", company="Example")) for p in ("greenhouse", "lever", "ashby")]
    posts = run(exercise())
    assert [len(page) for page in posts] == [1, 1, 1]
    assert 3 in waits
