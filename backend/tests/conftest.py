import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from gmail_fakes import Gmail


@pytest.fixture
def gmail_setup(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAIL_CREDENTIAL_DIR", str(tmp_path))
    monkeypatch.setenv("GMAIL_QUERY", "jobs")
    monkeypatch.setenv("GMAIL_LABELS", "job-alerts")
    (tmp_path / "mailbox").write_text("user@example.com\n")
    gmail = Gmail()
    monkeypatch.setattr("app.gmail_sync.load_credentials", lambda: object())
    monkeypatch.setattr("app.gmail_sync.build", lambda *args, **kwargs: gmail)
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield gmail, db
    engine.dispose()
