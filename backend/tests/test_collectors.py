from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.db import Base
from app.models import Board, Job, JobVersion
from app.collectors import normalize, content_hash, persist_snapshot
from app.scoring import baseline_score, prepare_packet


def test_parse_providers():
    assert normalize("greenhouse", {"id": 7, "title": "Senior Engineer", "location": {"name": "NYC"}, "absolute_url": "https://example.com", "content": "<p>Python &amp; React</p>"})["description"] == "Python & React"
    assert normalize("lever", {"id": "a", "text": "Engineer", "categories": {"location": "NYC"}, "descriptionPlain": "Hi", "applyUrl": "https://example.com"})["external_id"] == "a"
    assert normalize("ashby", {"jobUrl": "https://x/a", "title": "Engineer", "location": "NYC", "descriptionPlain": "Hi", "applyUrl": "https://x/b"})["external_id"] == "https://x/a"


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
    packet = prepare_packet({"title": "Senior Backend Engineer", "company": "A", "location": "New York", "description": "Python"}, profile)
    assert packet["selected_facts"][0]["id"] == "fact1"
