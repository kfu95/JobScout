from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.digest import send_digest
from app.models import Application, Board, DigestJob, Job
from app.worker import seconds_until_digest


class Mail:
    messages = []

    def __init__(self, host, port, timeout):
        assert (host, port, timeout) == ("smtp.example.com", 587, 30)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def starttls(self):
        pass

    def login(self, username, password):
        assert (username, password) == ("user", "secret")

    def send_message(self, message):
        self.messages.append(message)


@pytest.fixture
def setup(monkeypatch):
    for key, value in {"SMTP_HOST": "smtp.example.com", "SMTP_USERNAME": "user", "SMTP_PASSWORD": "secret", "DIGEST_TO": "to@example.com"}.items():
        monkeypatch.setenv(key, value)
    Mail.messages = []
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        board = Board(company="Example", provider="ashby", slug="example")
        db.add(board)
        db.flush()
        db.add_all([Job(board_id=board.id, external_id=str(i), title=f"Job {i}", location="Remote", description="Annual USD salary: $100,000 - $150,000", apply_url=f"https://example.com/{i}", hash=str(i), score=i) for i in (20, 90, 0)])
        db.commit()
        yield db
    engine.dispose()


def test_digest_sends_ranked_jobs_once_and_test_does_not_mark(setup):
    db = setup
    assert send_digest(db, test=True, transport=Mail)["count"] == 2
    assert not db.scalars(select(DigestJob)).all()
    assert send_digest(db, transport=Mail) == {"status": "sent", "count": 2}
    content = Mail.messages[-1].get_content()
    assert content.index("Job 90") < content.index("Job 20")
    assert "Remote" in content and "$150,000" in content and "ashby" in content
    assert "http://localhost:3000/?job=" in content
    assert send_digest(db, transport=Mail)["status"] == "empty"
    assert len(Mail.messages) == 2
    assert not db.scalars(select(Application)).all()


def test_failed_delivery_does_not_mark_jobs(setup):
    class Broken(Mail):
        def send_message(self, message):
            raise OSError("offline")
    with pytest.raises(OSError):
        send_digest(setup, transport=Broken)
    assert not setup.scalars(select(DigestJob)).all()


def test_schedule_and_unconfigured(setup, monkeypatch):
    monkeypatch.setenv("DIGEST_TIMEZONE", "America/New_York")
    monkeypatch.setenv("DIGEST_TIME", "08:30")
    assert seconds_until_digest(datetime(2026, 1, 1, 12, tzinfo=timezone.utc)) == 5400
    monkeypatch.delenv("SMTP_HOST")
    assert send_digest(setup, transport=Mail)["status"] == "unconfigured"
