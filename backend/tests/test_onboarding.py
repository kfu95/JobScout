from io import BytesIO

import pytest
from docx import Document
from reportlab.pdfgen import canvas
from pydantic import ValidationError

import app.onboarding as onboarding_store
from app.onboarding import extract_text, parse_resume, save_facts, load_facts, Preferences, storage_dir
from app.scoring import prepare_packet
from app.preferences import salary_filter as preference_filter


@pytest.fixture(autouse=True)
def local_store(tmp_path, monkeypatch):
    monkeypatch.setenv("ONBOARDING_DIR", str(tmp_path / "private"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def test_synthetic_documents():
    doc = Document()
    doc.add_paragraph("Experience")
    doc.add_paragraph("Engineer | Example Corp Jan 2020 - Mar 2024")
    doc.add_paragraph("Built reliable services", style="List Bullet")
    doc.add_paragraph("Skills")
    doc.add_paragraph("Python, SQL", style="List Bullet")
    buf = BytesIO()
    doc.save(buf)
    facts = parse_resume(extract_text("resume.docx", buf.getvalue()))
    assert facts[0]["company"] == "Example Corp"
    assert facts[0]["dates"] == "Jan 2020 - Mar 2024"
    assert facts[0]["bullets"] == ["Built reliable services"]
    assert facts[1]["skills"] == ["Python", "SQL"]
    assert all(not f["verified"] for f in facts)
    pdf = BytesIO()
    page = canvas.Canvas(pdf)
    page.drawString(30, 700, "Engineer | Example Corp 2020 - 2024")
    page.save()
    assert parse_resume(extract_text("resume.pdf", pdf.getvalue()))[0]["role"] == "Engineer"
    present = parse_resume("Staff Engineer | Current Corp February 2024 - Present")
    assert present[0]["company"] == "Current Corp"
    assert present[0]["dates"] == "February 2024 - Present"


def test_only_verified_facts_enter_packet():
    profile = {"name": "Test", "verified_experience": []}
    facts = [{"id": "1", "role": "Engineer", "company": "A", "dates": "2020 - 2024", "bullets": ["Built things"], "skills": ["Python"], "verified": False}]
    save_facts(facts)
    with pytest.raises(ValueError):
        prepare_packet({"title": "Engineer"}, profile)
    facts[0]["verified"] = True
    save_facts(facts)
    assert prepare_packet({"title": "Engineer"}, profile)["selected_facts"] == facts
    assert load_facts(profile) == facts


def test_preference_salary():
    prefs = Preferences(salary_floor=150000).model_dump()
    assert not preference_filter({"description": "Salary $100,000 - $140,000"}, prefs)[0]
    assert not preference_filter({"description": "Compensation range:\n$100,000-$140,000"}, prefs)[0]
    assert preference_filter({"description": "Salary: $25,000 signing bonus"}, prefs) == (True, "Salary unknown")
    assert not preference_filter({"description": "Salary NYC: $100,000-$140,000"}, prefs)[0]
    assert not preference_filter({"description": "Base salary $140,000 plus equity grant $200,000"}, prefs)[0]
    assert preference_filter({"description": "Salary $120k - $180k"}, prefs)[0]
    assert preference_filter({"description": "No compensation listed"}, prefs) == (True, "Salary unknown")
    assert preference_filter({"description": "Benefits include a $10,000 annual learning stipend"}, prefs) == (True, "Salary unknown")
    assert preference_filter({"description": "Signing bonus $50,000 and equity grant $100,000"}, prefs) == (True, "Salary unknown")
    usd_prefs = Preferences(salary_floor=200000).model_dump()
    assert preference_filter({"description": "Compensation range: $190,000-$220,000 CAD"}, usd_prefs) == (True, "Salary unknown")
    assert preference_filter({"description": "Salary: $20,000 per month"}, usd_prefs) == (True, "Salary unknown")
    assert preference_filter({"description": "Salary: $190,000-$220,000 USD"}, usd_prefs) == (True, "Salary stated")
    assert preference_filter({"description": "Salary: $190,000-$220,000 SGD"}, usd_prefs) == (True, "Salary unknown")
    assert preference_filter({"description": "Salary: $190,000-$220,000 cad"}, usd_prefs) == (True, "Salary unknown")
    with pytest.raises(ValidationError):
        Preferences(currency="EUR")


def test_preference_terms_are_normalized():
    preferences = Preferences(target_titles=["  ", " software engineer "], locations=["", " New York "])
    assert preferences.target_titles == ["software engineer"]
    assert preferences.locations == ["New York"]


def test_private_write_failure_preserves_existing_facts(monkeypatch):
    original = [{"id": "1", "role": "Engineer", "verified": True}]
    save_facts(original)

    def fail_replace(source, destination):
        raise OSError("disk unavailable")

    monkeypatch.setattr(onboarding_store.os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk unavailable"):
        save_facts([{"id": "2", "role": "Manager", "verified": False}])
    assert load_facts({}) == original
    assert not list(storage_dir().glob(".facts.json.*"))


def test_api_upload_review_and_revoke(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app, packet_is_current
    from app.db import Base, get_db
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)

    def override_db():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr("app.main.init_db", lambda: None)
    monkeypatch.setattr("app.main.load_profile", lambda: {"verified_experience": []})
    doc = Document()
    doc.add_paragraph("Engineer | Example Corp 2020 - 2024")
    doc.add_paragraph("• Built services")
    buf = BytesIO()
    doc.save(buf)
    try:
        with TestClient(app) as client:
            response = client.post("/api/onboarding/resume", files={"file": ("resume.docx", buf.getvalue())})
            assert response.status_code == 200
            fact = response.json()["facts"][0]
            assert not fact["verified"]
            fact["verified"] = True
            assert client.put(f"/api/onboarding/facts/{fact['id']}", json=fact).status_code == 200
            assert packet_is_current({"selected_facts": [fact]})

            second = Document()
            second.add_paragraph("Staff Engineer | Other Corp 2021 - 2025")
            second_buffer = BytesIO()
            second.save(second_buffer)
            assert client.post("/api/onboarding/resume", files={"file": ("new.docx", second_buffer.getvalue())}).status_code == 200

            third = Document()
            third.add_paragraph("Principal Engineer | Next Corp 2022 - 2026")
            third_buffer = BytesIO()
            third.save(third_buffer)
            assert client.post("/api/onboarding/resume", files={"file": ("latest.docx", third_buffer.getvalue())}).status_code == 200
            stored = client.get("/api/onboarding").json()["facts"]
            assert [(item["company"], item["verified"]) for item in stored] == [("Example Corp", True), ("Next Corp", False)]
            assert not list(storage_dir().glob("resume.*"))

            fact["verified"] = False
            assert client.put(f"/api/onboarding/facts/{fact['id']}", json=fact).status_code == 200
            assert not packet_is_current({"selected_facts": [{**fact, "verified": True}]})
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
