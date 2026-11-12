import asyncio
import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.aggregators import collect_aggregators, fetch_remotive, fetch_usajobs, remotive_posting, remotive_salary_max, store_aggregator, usajobs_posting
from app.collectors import collect_all, normalize, persist_snapshot
from app.db import Base
from app.discovery import refresh_directory
from app.models import Board, Job, JobVersion
from app.onboarding import Preferences, save_preferences
from app.preferences import matches

PREFS = {
    "target_titles": ["software engineer"],
    "seniority": [],
    "locations": ["new york"],
    "work_modes": ["remote"],
    "salary_floor": 150000,
    "currency": "USD",
}


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
            return httpx.Response(200, json={"jobs": [{"id": 4, "company_name": "A", "title": "Senior Software Engineer", "candidate_required_location": "USA", "url": "https://remotive.com/job/4", "salary": "40k - 50k USD"}]})
        return httpx.Response(200, json={"SearchResult": {"SearchResultCountAll": 1, "SearchResultItems": [{"MatchedObjectId": "2", "MatchedObjectDescriptor": {"PositionTitle": "Senior Software Engineer", "OrganizationName": "Agency", "PositionURI": "https://usajobs.gov/job/2", "PositionLocation": [{"LocationName": "New York"}], "PositionRemuneration": [{"MinimumRange": "92108", "MaximumRange": "119746", "RateIntervalCode": "PA", "Description": "Per Year"}]}}]}})
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_remotive(client), await fetch_usajobs(client, "secret", "person@example.com", "accountant")
    remotive, usa = run(exercise())
    assert remotive[0]["salary_max"] == 50000
    assert usa[0]["salary_max"] == 119746
    assert not matches(remotive[0], PREFS)
    assert not matches(usa[0], PREFS)
    assert requests[1].url.params["Keyword"] == "accountant"
    assert "category" not in requests[0].url.params
    assert requests[1].headers["authorization-key"] == "secret"
    assert requests[1].headers["user-agent"] == "person@example.com"
    assert not matches({**remotive[0], "remote_region": "Germany"}, PREFS)
    assert not matches({**remotive[0], "location": "Remote Germany", "remote_region": ""}, PREFS)
    assert matches({**usa[0], "title": "Staff Software Engineer", "salary_max": 180000}, PREFS)
    foreign_salary = remotive_posting({
        "id": 5, "company_name": "B", "title": "Senior Software Engineer", "candidate_required_location": "USA",
        "url": "https://remotive.com/job/5", "salary": "€140k - €160k",
    })
    assert foreign_salary["salary_max"] is None
    assert matches(foreign_salary, {**PREFS, "salary_floor": 170000})
    dollar_salary = remotive_posting({
        "id": 6, "company_name": "C", "title": "Senior Software Engineer", "candidate_required_location": "USA",
        "url": "https://remotive.com/job/6", "salary": "$40,000 - $50,000",
    })
    assert dollar_salary["salary_max"] == 50000
    assert remotive_salary_max("$40k - 50k") == 50000
    mixed_salary = remotive_posting({
        "id": 7, "company_name": "D", "title": "Senior Software Engineer", "candidate_required_location": "USA",
        "url": "https://remotive.com/job/7", "salary": "€200k - €220k (USD equivalent $140k - $145k)",
    })
    assert mixed_salary["salary_max"] == 145000
    assert not matches(mixed_salary, PREFS)


def test_ats_provider_contract_normalization():
    lever = normalize("lever", {
        "id": "lever-1", "text": "Staff Software Engineer", "applyUrl": "https://example.com/lever-1",
        "categories": {"location": "San Francisco", "allLocations": ["San Francisco", "New York"]},
        "workplaceType": "hybrid",
        "salaryRange": {"currency": "USD", "interval": "per-year-salary", "min": 100000, "max": 120000},
    })
    assert lever["location"] == "San Francisco, New York, Hybrid"
    assert lever["salary_max"] == 120000
    assert matches({**lever, "salary_max": 180000}, PREFS)
    assert not matches(lever, PREFS)

    lever_remote = normalize("lever", {
        "id": "lever-2", "text": "Staff Software Engineer", "applyUrl": "https://example.com/lever-2",
        "categories": {"location": "United States", "allLocations": ["United States"]},
        "workplaceType": "remote",
    })
    assert lever_remote["location"] == "United States, Remote"
    assert matches(lever_remote, PREFS)

    lever_onsite = normalize("lever", {
        "id": "lever-3", "text": "Staff Software Engineer", "applyUrl": "https://example.com/lever-3",
        "categories": {"location": "Boston"}, "workplaceType": "on-site",
    })
    assert lever_onsite["location"] == "Boston, Onsite"

    ashby_hybrid = normalize("ashby", {
        "jobUrl": "https://example.com/ashby-1", "title": "Staff Software Engineer", "location": "Boston",
        "isRemote": True, "workplaceType": "Hybrid", "descriptionPlain": "",
    })
    assert ashby_hybrid["location"] == "Boston / Hybrid"
    assert not matches(ashby_hybrid, PREFS)

    ashby_remote = normalize("ashby", {
        "jobUrl": "https://example.com/ashby-remote", "title": "Staff Software Engineer", "location": "Boston",
        "isRemote": True, "workplaceType": "Remote", "descriptionPlain": "",
    })
    assert ashby_remote["location"] == "Boston / Remote"
    assert matches(ashby_remote, PREFS)

    ashby = normalize("ashby", {
        "jobUrl": "https://example.com/ashby-2", "title": "Staff Software Engineer", "location": "San Francisco",
        "secondaryLocations": [{"location": "New York"}], "workplaceType": "OnSite", "descriptionPlain": "",
        "compensation": {"summaryComponents": [{
            "compensationType": "Salary", "interval": "1 YEAR", "currencyCode": "USD",
            "minValue": 81000, "maxValue": 87000,
        }]},
    })
    assert ashby["location"] == "San Francisco, New York / OnSite"
    assert ashby["salary_max"] == 87000
    assert matches({**ashby, "salary_max": 180000}, PREFS)
    assert not matches(ashby, PREFS)

    assert normalize("lever", {
        "id": "lever-4", "text": "Engineer", "applyUrl": "https://example.com/lever-4",
        "categories": {}, "salaryRange": {"currency": "EUR", "interval": "per-year-salary", "max": 180000},
    })["salary_max"] is None


def test_full_locations_match_before_bounded_persistence():
    location_names = [f"Region {index} {'x' * 20}" for index in range(30)] + ["New York"]
    postings = [
        normalize("lever", {
            "id": "lever-long", "text": "Senior Software Engineer", "applyUrl": "https://example.com/lever-long",
            "categories": {"allLocations": location_names}, "workplaceType": "hybrid",
        }),
        normalize("ashby", {
            "jobUrl": "https://example.com/ashby-long", "title": "Senior Software Engineer",
            "location": location_names[0],
            "secondaryLocations": [{"location": location} for location in location_names[1:]],
            "workplaceType": "OnSite", "descriptionPlain": "",
        }),
        usajobs_posting({
            "MatchedObjectId": "usa-long",
            "MatchedObjectDescriptor": {
                "PositionTitle": "Senior Software Engineer", "OrganizationName": "Agency",
                "PositionURI": "https://usajobs.gov/job/long",
                "PositionLocation": [{"LocationName": location} for location in location_names],
            },
        }),
    ]
    assert all(len(posting["location"]) > 500 and matches(posting, PREFS) for posting in postings)

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        for index, posting in enumerate(postings):
            board = Board(company=f"Company {index}", provider=f"provider-{index}", slug=f"provider-{index}")
            db.add(board)
            db.commit()
            persist_snapshot(db, board, [posting], preferences=PREFS)
            persist_snapshot(db, board, [{**posting, "location": posting["location"] + ", Updated Place"}], preferences=PREFS)
            job = db.scalar(select(Job).where(Job.board_id == board.id))
            versions = db.scalars(select(JobVersion).where(JobVersion.job_id == job.id)).all()
            assert len(job.location) == 500 and job.location.endswith("...")
            assert "Preferred location" in job.score_reason
            assert len(versions) == 2
            assert all(version.location == job.location for version in versions)


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
        persist_snapshot(db, board, [])
        store_aggregator(db, "usajobs", [{**posting, "external_id": "99", "apply_url": "https://example.com/job/1"}], PREFS)
        persist_snapshot(db, board, [])
        job = db.scalar(select(Job))
        assert job.is_open and job.missing_count == 1
        other = Board(company="Other", provider="ashby", slug="other")
        db.add(other)
        db.commit()
        other_posting = {**posting, "external_id": "2", "company": "Other", "apply_url": "https://example.com/job/2"}
        persist_snapshot(db, other, [other_posting])
        persist_snapshot(db, other, [{**other_posting, "apply_url": "https://example.com/job/1?ref=duplicate"}])
        assert len(db.scalars(select(Job).where(Job.is_open.is_(True))).all()) == 1


def test_filtered_observation_does_not_close_live_job():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        board = Board(company="Example", provider="greenhouse", slug="example")
        db.add(board)
        db.commit()
        posting = {"external_id": "1", "company": "Example", "title": "Senior Software Engineer", "location": "New York", "description": "", "apply_url": "https://example.com/job/1"}
        persist_snapshot(db, board, [posting])
        moved = {**posting, "location": "Boston", "salary_max": 120000}
        unseen = {**moved, "external_id": "2", "apply_url": "https://example.com/job/2"}
        assert not matches(moved, PREFS)
        for _ in range(2):
            persist_snapshot(db, board, [moved, unseen], preferences=PREFS)
        job = db.scalar(select(Job))
        assert len(db.scalars(select(Job)).all()) == 1
        assert job.is_open and job.missing_count == 0 and job.score == 0
        assert job.location == "Boston"
        assert "Annual USD salary maximum: $120,000" in job.description


def test_filtered_canonical_duplicate_is_reconciled():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        board = Board(company="Example", provider="greenhouse", slug="example")
        db.add(board)
        db.commit()
        posting = {"external_id": "1", "title": "Senior Software Engineer", "location": "New York", "description": "", "apply_url": "https://example.com/job/1"}
        persist_snapshot(db, board, [posting], preferences=PREFS)
        store_aggregator(db, "remotive", [{
            **posting,
            "external_id": "remote-1",
            "company": "Example",
            "location": "Remote Germany",
            "remote_region": "Germany",
            "salary_max": 120000,
        }], PREFS)
        job = db.scalar(select(Job))
        assert len(db.scalars(select(Job)).all()) == 1
        assert job.is_open and job.score > 0
        assert job.location == "New York"
        assert "Annual USD salary maximum" not in job.description


def test_location_eligibility_matches_stored_and_new_jobs():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        board = Board(company="Example", provider="greenhouse", slug="example")
        db.add(board)
        db.commit()
        existing = {
            "external_id": "existing", "title": "Software Engineer", "location": "Boston",
            "description": "", "apply_url": "https://example.com/existing",
        }
        persist_snapshot(db, board, [existing], preferences={**PREFS, "locations": [], "salary_floor": None})
        persist_snapshot(db, board, [
            existing,
            {**existing, "external_id": "new-onsite", "apply_url": "https://example.com/new-onsite"},
            {**existing, "external_id": "new-remote", "location": "Boston, Remote", "apply_url": "https://example.com/new-remote"},
        ], preferences={**PREFS, "salary_floor": None})

        jobs = {job.external_id: job for job in db.scalars(select(Job)).all()}
        assert jobs["existing"].score == 0
        assert "new-onsite" not in jobs
        assert jobs["new-remote"].score > 0


def test_ats_description_amount_is_not_treated_as_salary(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        board = Board(company="Example", provider="greenhouse", slug="example")
        db.add(board)
        db.commit()
        save_preferences(Preferences(
            target_titles=["staff software engineer"],
            locations=["new york"],
            salary_floor=150000,
        ))
        posting = {"external_id": "1", "company": "Example", "title": "Staff Software Engineer", "location": "New York", "description": "$100,000 annual benefit coverage", "apply_url": "https://example.com/job/1"}

        async def no_directory(_db, _client):
            return 0

        async def no_aggregators(_db, _client, _preferences, _existing_urls):
            return []

        async def one_posting(_client, _board):
            return [posting]

        monkeypatch.setattr("app.discovery.refresh_directory", no_directory)
        monkeypatch.setattr("app.aggregators.collect_aggregators", no_aggregators)
        monkeypatch.setattr("app.collectors.fetch_board", one_posting)
        run(collect_all(db))
        assert db.scalar(select(Job)) is not None


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


def test_new_aggregator_responses(monkeypatch):
    from app.aggregators import fetch_adzuna, fetch_remoteok

    async def no_wait(_):
        pass

    monkeypatch.setattr("app.discovery.asyncio.sleep", no_wait)
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == "remoteok.com":
            return httpx.Response(200, json=[{"legal": "terms"}, {"id": 7, "company": "Example", "position": "Software Engineer", "location": "USA", "description": "<p>Python</p>", "url": "https://remoteok.com/remote-jobs/7", "apply_url": "https://example.com/apply"}])
        return httpx.Response(200, json={"results": [{"id": "8", "company": {"display_name": "Example"}, "title": "Software Engineer", "location": {"display_name": "New York"}, "description": "Snippet", "redirect_url": "https://www.adzuna.com/land/8"}]})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_remoteok(client), await fetch_adzuna(client, "id", "key")

    remote, adzuna = run(exercise())
    assert remote[0]["apply_url"] == "https://remoteok.com/remote-jobs/7"
    assert remote[0]["description"] == "Python"
    assert adzuna[0]["location"] == "New York"
    assert adzuna[0]["apply_url"] == "https://www.adzuna.com/land/8"
    assert requests[1].url.params["app_id"] == "id"
    assert requests[1].url.params["app_key"] == "key"


def test_hackernews_only_collects_top_level_hiring_comments(monkeypatch):
    from app.aggregators import fetch_hackernews

    async def no_wait(_):
        pass

    monkeypatch.setattr("app.discovery.asyncio.sleep", no_wait)

    def handler(request):
        if request.url.path.endswith("search_by_date"):
            return httpx.Response(200, json={"hits": [{"objectID": "10", "title": "Ask HN: Who is hiring? (November 2026)"}]})
        return httpx.Response(200, json={"children": [{"id": 11, "text": "Acme | Software Engineer | Remote<p>Python</p>", "children": [{"id": 12, "text": "reply"}]}]})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_hackernews(client)

    postings = run(exercise())
    assert len(postings) == 1
    assert postings[0]["company"] == "Acme"
    assert postings[0]["title"] == "Acme | Software Engineer | Remote"
    assert postings[0]["apply_url"] == "https://news.ycombinator.com/item?id=11"


def test_adzuna_log_redacts_credentials(caplog):
    import logging

    caplog.set_level(logging.INFO, logger="httpx")
    logging.getLogger("httpx").info("HTTP Request: %s", "https://api.adzuna.com/?app_id=private-id&app_key=private-key")
    assert "private-id" not in caplog.text
    assert "private-key" not in caplog.text
    assert "Adzuna HTTP request completed" in caplog.text


def test_adzuna_missing_credentials_skip_and_do_not_request(monkeypatch, caplog):
    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)

    async def no_wait(_):
        pass

    monkeypatch.setattr("app.discovery.asyncio.sleep", no_wait)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == "remotive.com":
            return httpx.Response(200, json={"jobs": []})
        if request.url.host == "remoteok.com":
            return httpx.Response(200, json=[{"legal": "terms"}])
        return httpx.Response(200, json={"hits": [{"objectID": "10", "title": "Ask HN: Who is hiring?"}]} if request.url.path.endswith("search_by_date") else {"children": []})

    async def exercise(db):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await collect_aggregators(db, client, PREFS)

    with Session(engine) as db:
        result = run(exercise(db))
    assert any(row.get("provider") == "adzuna" and row.get("skipped") for row in result)
    assert "Skipping Adzuna" in caplog.text
    assert all(request.url.host != "api.adzuna.com" for request in requests)


def test_adzuna_uses_dashboard_credentials_and_target(monkeypatch):
    from app.settings import IntegrationSettings, save_settings

    save_settings(IntegrationSettings(adzuna_app_id="dashboard-id", adzuna_api_key="dashboard-key"))

    async def no_wait(_):
        pass

    monkeypatch.setattr("app.discovery.asyncio.sleep", no_wait)
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == "api.adzuna.com":
            return httpx.Response(200, json={"results": []})
        if request.url.host == "remotive.com":
            return httpx.Response(200, json={"jobs": []})
        if request.url.host == "remoteok.com":
            return httpx.Response(200, json=[{"legal": "terms"}])
        return httpx.Response(200, json={"hits": [{"objectID": "10", "title": "Ask HN: Who is hiring?"}]} if request.url.path.endswith("search_by_date") else {"children": []})

    async def exercise(db):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await collect_aggregators(db, client, PREFS)

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        result = run(exercise(db))
    request = next(req for req in requests if req.url.host == "api.adzuna.com")
    assert request.url.params["app_id"] == "dashboard-id"
    assert request.url.params["app_key"] == "dashboard-key"
    assert request.url.params["what"] == "software engineer"
    assert any(row.get("provider") == "adzuna" and row.get("ok") for row in result)
