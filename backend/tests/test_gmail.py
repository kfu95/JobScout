from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.gmail_auth import GmailAuthError, load_credentials
from app.gmail_sync import sync_gmail
from app.main import app
from app.models import GmailMessage, GmailSyncState

class Request:
    def __init__(self, result):
        self.result = result

    def execute(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

class Gmail:
    def __init__(self):
        self.ids = ["a", "b"]
        self.history_ids = []
        self.expired = False
        self.history_start = []

    def users(self):
        return self

    def messages(self):
        return self

    def history(self):
        return self

    def getProfile(self, **kwargs):
        return Request({"historyId": "100"})

    def list(self, **kwargs):
        if "startHistoryId" in kwargs:
            self.history_start.append(kwargs["startHistoryId"])
            if self.expired:
                return Request(HttpError(SimpleNamespace(status=404, reason="expired"), b"{}"))
            if not kwargs["pageToken"]:
                return Request({"history": [{"messagesAdded": [{"message": {"id": i}} for i in self.history_ids[:1]]}], "nextPageToken": "next"})
            return Request({"history": [{"labelsAdded": [{"message": {"id": i}} for i in self.history_ids[1:]]}]})
        assert kwargs["q"] == "jobs"
        if not kwargs["pageToken"]:
            return Request({"messages": [{"id": self.ids[0]}], "nextPageToken": "next"})
        return Request({"messages": [{"id": i} for i in self.ids[1:]]})

    def get(self, **kwargs):
        assert kwargs["format"] == "metadata"
        return Request({"threadId": "thread", "internalDate": "1700000000000", "snippet": "s" * 600,
                        "payload": {"headers": [{"name": "Subject", "value": "Job"}, {"name": "Message-ID", "value": "<id>"}]}})

@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAIL_CREDENTIAL_DIR", str(tmp_path))
    monkeypatch.setenv("GMAIL_QUERY", "jobs")
    monkeypatch.delenv("GMAIL_LABELS", raising=False)
    gmail = Gmail()
    monkeypatch.setattr("app.gmail_sync.load_credentials", lambda: object())
    monkeypatch.setattr("app.gmail_sync.build", lambda *args, **kwargs: gmail)
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield gmail, db
    engine.dispose()

def test_full_incremental_expiry_and_idempotence(setup):
    gmail, db = setup
    assert sync_gmail(db) == {"added": 2, "mode": "full"}
    assert len(db.scalar(select(GmailMessage).where(GmailMessage.provider_message_id == "a")).snippet) == 500
    gmail.ids += ["c", "d"]
    gmail.history_ids = ["c", "d"]
    assert sync_gmail(db) == {"added": 2, "mode": "incremental"}
    assert gmail.history_start == ["100", "100"]
    assert sync_gmail(db) == {"added": 0, "mode": "incremental"}
    gmail.expired = True
    gmail.ids += ["e"]
    assert sync_gmail(db) == {"added": 1, "mode": "full"}
    assert db.scalar(select(func.count(GmailMessage.id))) == 5
    assert db.get(GmailSyncState, 1).history_id == "100"

def test_revoked_credentials_are_explicit(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAIL_CREDENTIAL_DIR", str(tmp_path))
    (tmp_path / "token.json").write_text("{}")
    class Credentials:
        valid = False
        refresh_token = "revoked"
        def refresh(self, request):
            raise RefreshError("revoked")
    monkeypatch.setattr("app.gmail_auth.Credentials.from_authorized_user_file", lambda *args: Credentials())
    with pytest.raises(GmailAuthError, match="revoked"):
        load_credentials()
    monkeypatch.setattr("app.main.init_db", lambda: None)
    with TestClient(app) as client:
        response = client.get("/api/gmail/status")
        assert response.status_code == 401
        assert "revoked" in response.json()["detail"]

def test_oauth_flow_checks_state_and_saves_credentials(monkeypatch):
    class Flow:
        credentials = SimpleNamespace(refresh_token="refresh")
        def authorization_url(self, **kwargs):
            assert kwargs["access_type"] == "offline"
            return "https://accounts.google.com/test", "random-state"
        def fetch_token(self, **kwargs):
            assert kwargs == {"code": "code"}
    saved = []
    monkeypatch.setattr("app.gmail_api.new_flow", lambda: Flow())
    monkeypatch.setattr("app.gmail_api.save_credentials", saved.append)
    monkeypatch.setattr("app.main.init_db", lambda: None)
    with TestClient(app) as client:
        assert client.post("/api/gmail/connect").json()["authorization_url"].startswith("https://")
        assert client.get("/api/gmail/callback", params={"state": "wrong", "code": "code"}).status_code == 400
        assert client.get("/api/gmail/callback", params={"state": "random-state", "code": "code"}).json() == {"connected": True}
        assert client.get("/api/gmail/callback", params={"state": "random-state", "code": "code"}).status_code == 400
    assert len(saved) == 1
