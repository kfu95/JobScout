import asyncio
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.db import Base
from app.models import Board, Job, JobVersion
import app.collectors as collectors
import app.scoring as scoring
from app.collectors import normalize, content_hash, persist_snapshot
from app.onboarding import Preferences, save_preferences
from app.scoring import baseline_score, prepare_packet, score_posting


def test_parse_providers():
    assert normalize("greenhouse", {"id": 7, "title": "Senior Engineer", "location": {"name": "NYC"}, "absolute_url": "https://example.com", "content": "<p>Python &amp; React</p>"})["description"] == "Python & React"
    assert normalize("lever", {"id": "a", "text": "Engineer", "categories": {"location": "NYC"}, "descriptionPlain": "Hi", "applyUrl": "https://example.com"})["external_id"] == "a"
    compensation = {
        "scrapeableCompensationSalarySummary": "$20K - $180K",
        "summaryComponents": [
            {"compensationType": "Salary", "interval": "1 MONTH", "currencyCode": "USD", "minValue": 20000, "maxValue": 30000},
            {"compensationType": "Salary", "interval": "1 YEAR", "currencyCode": "SGD", "minValue": 120000, "maxValue": 140000},
            {"compensationType": "Salary", "interval": "1 YEAR", "currencyCode": "USD", "minValue": 120000, "maxValue": 180000},
        ],
    }
    ashby = normalize("ashby", {"jobUrl": "https://x/a", "title": "Engineer", "location": "NYC", "workplaceType": "Hybrid", "isRemote": True, "descriptionPlain": "Hi", "applyUrl": "https://x/b", "compensation": compensation})
    assert ashby["external_id"] == "https://x/a"
    assert ashby["location"] == "NYC / Hybrid"
    assert ashby["description"] == "Hi\nAnnual USD salary: $120,000 - $180,000"
    assert content_hash(ashby) != content_hash({**ashby, "description": "Hi\nAnnual USD salary: $120,000 - $160,000"})
    assert normalize("ashby", {"jobUrl": "https://x/monthly", "title": "Engineer", "descriptionPlain": "Hi", "compensation": {"summaryComponents": compensation["summaryComponents"][:2]}})["description"] == "Hi"


def test_idempotency_versions_closure():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        board = Board(company="Test", provider="ashby", slug="test")
        db.add(board)
        db.commit()
        posting = {"external_id": "1", "title": "Senior Software Engineer", "location": "New York", "description": "Python", "apply_url": "https://example.com"}
        assert persist_snapshot(db, board, [posting])["new"] == 1
        assert persist_snapshot(db, board, [posting])["updated"] == 0
        posting["description"] = "Python and React"
        assert persist_snapshot(db, board, [posting])["updated"] == 1
        assert len(db.scalars(select(JobVersion)).all()) == 2
        assert persist_snapshot(db, board, [])["closed"] == 0
        assert persist_snapshot(db, board, [])["closed"] == 1
        assert db.scalar(select(Job)).is_open is False


def test_scoring_and_verified_packets():
    profile = {"name": "Test", "skills": ["Python"], "target_titles": ["senior backend engineer"],
               "preferred_locations": ["new york"], "verified_experience": [{"id": "fact1", "role": "Engineer", "company": "Test Co", "bullets": ["Built verified software"]}]}
    score, reason, method = baseline_score({"title": "Senior Backend Engineer", "location": "New York", "description": "Python"}, profile)
    assert score >= 80 and method == "heuristic"
    assert baseline_score({"title": "Senior Sales Manager", "location": "New York", "description": "Python"}, profile)[0] == 0
    assert baseline_score({"title": "Senior Backend Engineering Manager", "location": "New York", "description": "Python"}, profile)[0] == 0

    fallback = Preferences(target_titles=[" "], locations=["new york"]).model_dump()
    assert baseline_score({"title": "Software Engineer", "location": "New York", "description": ""}, profile, fallback)[0] > 0

    ranked = Preferences(target_titles=["software engineer"], seniority=["staff"]).model_dump()
    plain_score = baseline_score({"title": "Software Engineer", "location": "", "description": ""}, profile, ranked)[0]
    staff_score = baseline_score({"title": "Staff Software Engineer", "location": "", "description": ""}, profile, ranked)[0]
    assert staff_score == plain_score + 15

    location_preferences = Preferences(target_titles=["software engineer"], locations=["ny"]).model_dump()
    germany_score = baseline_score({"title": "Software Engineer", "location": "Berlin, Germany", "description": ""}, profile, location_preferences)[0]
    new_york_score = baseline_score({"title": "Software Engineer", "location": "New York, NY", "description": ""}, profile, location_preferences)[0]
    assert new_york_score == germany_score + 15

    hybrid_preferences = Preferences(target_titles=["software engineer"], work_modes=["hybrid"]).model_dump()
    assert baseline_score({"title": "Software Engineer", "location": "Berlin / Hybrid", "description": ""}, profile, hybrid_preferences)[0] == plain_score + 15
    packet = prepare_packet({"title": "Senior Backend Engineer", "company": "A", "location": "New York", "description": "Python"}, profile)
    assert packet["selected_facts"][0]["id"] == "fact1"


def test_ai_scoring_preserves_selected_seniority(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("ENABLE_AI_SCORING", "true")
    monkeypatch.setattr(
        scoring,
        "_ai_parse",
        lambda *args, **kwargs: scoring.FitResult(score=60, reasoning="Fit", strengths=[], gaps=[]),
    )
    preferences = Preferences(target_titles=["software engineer"], seniority=["staff"]).model_dump()
    profile = {"skills": []}
    plain = score_posting({"title": "Software Engineer", "location": "", "description": ""}, profile, preferences)
    staff = score_posting({"title": "Staff Software Engineer", "location": "", "description": ""}, profile, preferences)
    assert plain[0] == 60 and plain[2] == "AI"
    assert staff[0] == plain[0] + 15 and staff[2] == "AI"


def test_allowed_ai_score_reserves_zero_for_hard_filters(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("ENABLE_AI_SCORING", "true")
    monkeypatch.setattr(
        scoring,
        "_ai_parse",
        lambda *args, **kwargs: scoring.FitResult(score=0, reasoning="Low fit", strengths=[], gaps=[]),
    )
    preferences = Preferences(target_titles=["software engineer"]).model_dump()
    profile = {"skills": []}
    allowed = score_posting({"title": "Software Engineer", "location": "", "description": ""}, profile, preferences)
    rejected = score_posting({"title": "Sales Manager", "location": "", "description": ""}, profile, preferences)
    assert allowed[0] == 1 and allowed[2] == "AI"
    assert rejected[0] == 0 and rejected[2] == "heuristic"


def test_collection_uses_preferences_current_after_fetch(monkeypatch):
    async def scenario():
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        save_preferences(Preferences(target_titles=["software engineer"]))
        with Session(engine) as db:
            board = Board(company="Test", provider="ashby", slug="test")
            db.add(board)
            db.commit()

            async def fetch_with_preference_change(client, fetched_board):
                save_preferences(Preferences(target_titles=["sales engineer"]))
                return [{"external_id": "sales", "title": "Sales Engineer", "location": "", "description": "", "apply_url": "https://example.com/sales"}]

            monkeypatch.setattr(collectors, "fetch_board", fetch_with_preference_change)
            result = await collectors.collect_all(db)
            assert result[0]["ok"] is True
            assert db.scalar(select(Job).where(Job.external_id == "sales")).score > 0

    asyncio.run(scenario())
