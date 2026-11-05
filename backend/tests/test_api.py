from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from app.db import Base, get_db
from app.main import app
from app.models import Board, Job
from app.collectors import content_hash


def test_review_before_submission(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        board = Board(company="Example Co", provider="ashby", slug="example")
        db.add(board)
        db.flush()
        p = {"title": "Senior Software Engineer", "location": "New York", "description": "Python and React", "apply_url": "https://example.com/apply"}
        job = Job(board_id=board.id, external_id="1", **p, hash=content_hash(p), score=90)
        db.add(job)
        db.commit()
        job_id = job.id

    def override_db():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr("app.main.init_db", lambda: None)
    monkeypatch.setattr("app.main.prepare_packet", lambda posting: {
        "candidate_name": "Test Candidate", "candidate_email": "", "summary": "Verified", "skills": ["Python"],
        "selected_facts": [{"id":"one", "company":"Test", "role":"Engineer", "bullets":["True statement"]}],
        "why_company_draft": "Draft", "verification_notes": ["Check details"]})
    try:
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
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
