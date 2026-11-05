import os
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlparse

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
        self.full_list_calls = 0
        self.last_get_id = None
        self.query_matches = {"a", "b"}
        self.message_labels = {}
        self.full_query = "jobs"
        self.expected_labels = ["job-alerts"]
        self.include_spam_trash = False

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
        if kwargs["q"] != self.full_query:
            assert kwargs["q"] == f"({self.full_query}) after:1699999999 before:1700000001"
            assert kwargs["labelIds"] == self.expected_labels
            assert kwargs["includeSpamTrash"] is self.include_spam_trash
            matches = [{"id": self.last_get_id}] if self.last_get_id in self.query_matches else []
            return Request({"messages": matches})
        assert kwargs["labelIds"] == self.expected_labels
        assert kwargs["includeSpamTrash"] is self.include_spam_trash
        self.full_list_calls += 1
        if not kwargs["pageToken"]:
            return Request({"messages": [{"id": self.ids[0]}], "nextPageToken": "next"})
        return Request({"messages": [{"id": i} for i in self.ids[1:]]})

    def get(self, **kwargs):
        assert kwargs["format"] == "metadata"
        self.last_get_id = kwargs["id"]
        return Request({"threadId": "thread", "internalDate": "1700000000000", "snippet": "s" * 600,
                        "labelIds": self.message_labels.get(kwargs["id"], ["job-alerts"]),
                        "payload": {"headers": [{"name": "Subject", "value": "Job"}, {"name": "Message-ID", "value": "<id>"}]}})

@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAIL_CREDENTIAL_DIR", str(tmp_path))
    monkeypatch.setenv("GMAIL_QUERY", "jobs")
    monkeypatch.setenv("GMAIL_LABELS", "job-alerts")
    monkeypatch.setenv("GMAIL_RECONCILE_SECONDS", "86400")
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
    assert gmail.full_list_calls == 2
    gmail.history_ids = ["c", "d", "wrong-query", "wrong-label"]
    gmail.query_matches.update({"c", "d", "wrong-label"})
    gmail.message_labels["wrong-label"] = ["other"]
    assert sync_gmail(db) == {"added": 2, "mode": "incremental"}
    assert gmail.full_list_calls == 2
    assert gmail.history_start == ["100", "100"]
    assert sync_gmail(db) == {"added": 0, "mode": "incremental"}
    assert gmail.full_list_calls == 2
    gmail.expired = True
    gmail.ids += ["e"]
    assert sync_gmail(db) == {"added": 1, "mode": "full"}
    assert db.scalar(select(func.count(GmailMessage.id))) == 5
    assert db.get(GmailSyncState, 1).history_id == "100"

def test_periodic_reconciliation_catches_relative_query_matches(setup, monkeypatch, tmp_path):
    gmail, db = setup
    monkeypatch.setenv("GMAIL_QUERY", "older_than:1d")
    gmail.full_query = "older_than:1d"
    assert sync_gmail(db) == {"added": 2, "mode": "full"}
    gmail.ids.append("relative")
    assert sync_gmail(db) == {"added": 0, "mode": "incremental"}
    assert db.scalar(select(GmailMessage.id).where(GmailMessage.provider_message_id == "relative")) is None
    os.utime(tmp_path / ".last-full-sync", (0, 0))
    assert sync_gmail(db) == {"added": 1, "mode": "full"}
    assert db.scalar(select(GmailMessage.id).where(GmailMessage.provider_message_id == "relative"))

@pytest.mark.parametrize(
    ("query", "labels", "system_label"),
    [("in:spam", "", "SPAM"), ("", "TRASH", "TRASH")],
)
def test_explicit_spam_and_trash_filters_are_honored(
    setup, monkeypatch, query, labels, system_label
):
    gmail, db = setup
    monkeypatch.setenv("GMAIL_QUERY", query)
    monkeypatch.setenv("GMAIL_LABELS", labels)
    gmail.full_query = query or None
    gmail.expected_labels = [labels] if labels else None
    gmail.include_spam_trash = True
    gmail.message_labels.update({"a": [system_label], "b": [system_label]})
    assert sync_gmail(db) == {"added": 2, "mode": "full"}
    gmail.history_ids = ["system-message"]
    gmail.query_matches.add("system-message")
    gmail.message_labels["system-message"] = [system_label]
    assert sync_gmail(db) == {"added": 1, "mode": "incremental"}
    assert db.scalar(
        select(GmailMessage.id).where(GmailMessage.provider_message_id == "system-message")
    )

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
        def __init__(self, redirect_uri):
            self.redirect_uri = redirect_uri

        credentials = SimpleNamespace(refresh_token="refresh")

        def authorization_url(self, **kwargs):
            assert kwargs["access_type"] == "offline"
            query = urlencode({"redirect_uri": self.redirect_uri})
            return f"https://accounts.google.com/test?{query}", "random-state"

        def fetch_token(self, **kwargs):
            assert kwargs == {"code": "code"}

    def flow_from_file(*args, **kwargs):
        return Flow(kwargs["redirect_uri"])

    saved = []
    monkeypatch.setattr("app.gmail_auth.Flow.from_client_secrets_file", flow_from_file)
    monkeypatch.setattr("app.gmail_api.save_credentials", saved.append)
    monkeypatch.setattr("app.main.init_db", lambda: None)
    with TestClient(app) as client:
        authorization_url = client.post("/api/gmail/connect").json()["authorization_url"]
        assert parse_qs(urlparse(authorization_url).query)["redirect_uri"] == ["http://127.0.0.1:8000/"]
        assert client.get("/", params={"state": "wrong", "code": "code"}).status_code == 400
        assert client.get("/", params={"state": "random-state", "code": "code"}).json() == {"connected": True}
        assert client.get("/", params={"state": "random-state", "code": "code"}).status_code == 400
    assert len(saved) == 1
