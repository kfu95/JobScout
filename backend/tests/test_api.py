import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from app.db import Base, get_db
from app.main import app
from app.models import Application, Board, Job, now
from app.collectors import content_hash
from app.onboarding import save_facts


@pytest.fixture
def api_engine(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)

    def override_db():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr("app.main.init_db", lambda: None)
    yield engine
    app.dependency_overrides.clear()
    engine.dispose()


def add_job(engine, external_id="1", title="Senior Software Engineer", score=90):
    with Session(engine) as db:
        board = Board(company="Example Co", provider="ashby", slug="example")
        db.add(board)
        db.flush()
        p = {"title": title, "location": "New York", "description": "Python and React", "apply_url": f"https://example.com/apply/{external_id}"}
        job = Job(board_id=board.id, external_id=external_id, **p, hash=content_hash(p), score=score)
        db.add(job)
        db.commit()
        return job.id


def test_review_before_submission(monkeypatch, api_engine):
    job_id = add_job(api_engine)
    monkeypatch.setattr("app.main.packet_is_current", lambda packet: True)
    monkeypatch.setattr("app.main.prepare_packet", lambda posting: {
        "candidate_name": "Test Candidate", "candidate_email": "", "summary": "Verified", "skills": ["Python"],
        "selected_facts": [{"id":"one", "company":"Test", "role":"Engineer", "bullets":["True statement"]}],
        "why_company_draft": "Draft", "verification_notes": ["Check details"]})
    with TestClient(app) as client:
        assert client.post(f"/api/jobs/{job_id}/reported").status_code == 409
        assert client.post(f"/api/jobs/{job_id}/decision", json={"decision":"approve"}).status_code == 409
        assert client.post(f"/api/jobs/{job_id}/prepare").status_code == 200
        pdf = client.get(f"/api/jobs/{job_id}/resume.pdf")
        assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
        assert client.post(f"/api/jobs/{job_id}/decision", json={"decision":"approve"}).json()["status"] == "approved"
        assert client.post(f"/api/jobs/{job_id}/reported").json()["status"] == "submission_reported"
        assert client.post(f"/api/jobs/{job_id}/status", json={"status":"confirmed"}).status_code == 400
        assert client.post(f"/api/jobs/{job_id}/status", json={"status":"confirmed", "evidence":"receipt email"}).json()["status"] == "confirmed"


def test_changed_fact_invalidates_approved_packet(api_engine):
    fact = {"id": "fact-1", "role": "Engineer", "company": "Example Co", "dates": "2020 - 2024", "bullets": ["Built services"], "skills": ["Python"], "verified": True}
    save_facts([fact])
    job_id = add_job(api_engine)
    with Session(api_engine) as db:
        db.add(Application(job_id=job_id, status="approved", packet={"selected_facts": [fact]}, approved_at=now()))
        db.commit()

    changed = {**fact, "bullets": ["Built reliable services"]}
    with TestClient(app) as client:
        assert client.put(f"/api/onboarding/facts/{fact['id']}", json=changed).status_code == 200
        detail = client.get(f"/api/jobs/{job_id}").json()
        assert detail["application_status"] == "prepared"
        assert detail["packet"] == {}
        assert client.post(f"/api/jobs/{job_id}/reported").status_code == 409

    with Session(api_engine) as db:
        application = db.query(Application).filter_by(job_id=job_id).one()
        assert application.approved_at is None


def test_preferences_rescore_stored_jobs_once(api_engine, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    engineer_id = add_job(api_engine, "engineer", "Senior Software Engineer", 5)
    with Session(api_engine) as db:
        board = db.query(Board).one()
        p = {"title": "Senior Sales Manager", "location": "Remote", "description": "Enterprise sales", "apply_url": "https://example.com/apply/sales"}
        sales = Job(board_id=board.id, external_id="sales", **p, hash=content_hash(p), score=95)
        db.add(sales)
        db.commit()
        sales_id = sales.id

    preferences = {"target_titles": ["senior software engineer"], "locations": ["new york"], "work_modes": [], "role_families": [], "seniority": [], "salary_floor": None, "currency": "USD", "excluded_companies": [], "excluded_keywords": []}
    with TestClient(app) as client:
        assert client.put("/api/onboarding/preferences", json=preferences).status_code == 200
        feed = client.get("/api/jobs", params={"min_score": 70}).json()
        assert [job["id"] for job in feed] == [engineer_id]
        assert client.get(f"/api/jobs/{engineer_id}").json()["score"] == 80
        assert client.get(f"/api/jobs/{sales_id}").json()["score"] == 0
        assert client.get("/api/stats").json()["strong_matches"] == 1


def test_board_listing_limit_is_optional(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([Board(company=f"Company {index}", provider="ashby", slug=f"company-{index}") for index in range(3)])
        db.commit()

    def override_db():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr("app.main.init_db", lambda: None)
    try:
        with TestClient(app) as client:
            assert len(client.get("/api/boards?limit=2").json()) == 2
            assert len(client.get("/api/boards").json()) == 3
            assert client.get("/api/boards?limit=501").status_code == 422
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
