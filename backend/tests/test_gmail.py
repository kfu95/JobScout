from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo

import httplib2
import pytest
from fastapi.testclient import TestClient
from google.auth.exceptions import RefreshError, TransportError
from googleapiclient.errors import HttpError
from oauthlib.oauth2 import InvalidGrantError
from requests.exceptions import Timeout
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app import gmail_api
from app.gmail_auth import GmailAuthError, GmailTransportError, load_credentials
from app.gmail_sync import GmailSyncError, sync_gmail
from app.main import app
from app.models import GmailMessage, GmailSyncState
from app.worker import seconds_until_gmail_sync

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
        self.label_history_ids = []
        self.expired = False
        self.history_start = []
        self.full_list_calls = 0
        self.get_ids = []
        self.history_without_labels = set()
        self.last_get_id = None
        self.query_matches = {"a", "b"}
        self.message_labels = {}
        self.full_query = "jobs"
        self.expected_labels = ["job-alerts"]
        self.profile_result = {"emailAddress": "user@example.com", "historyId": "100"}

    def users(self):
        return self

    def messages(self):
        return self

    def history(self):
        return self

    def getProfile(self, **kwargs):
        return Request(self.profile_result)

    def history_message(self, message_id):
        message = {"id": message_id}
        if message_id not in self.history_without_labels:
            message["labelIds"] = self.message_labels.get(message_id, ["job-alerts"])
        return {"message": message}

    def list(self, **kwargs):
        if "startHistoryId" in kwargs:
            self.history_start.append(kwargs["startHistoryId"])
            if self.expired:
                return Request(HttpError(SimpleNamespace(status=404, reason="expired"), b"{}"))
            if not kwargs["pageToken"]:
                return Request({"history": [{
                    "messagesAdded": [self.history_message(i) for i in self.history_ids[:1]],
                }], "nextPageToken": "next"})
            return Request({"history": [{
                "messagesAdded": [self.history_message(i) for i in self.history_ids[1:]],
                "labelsAdded": [{"message": {"id": i}} for i in self.label_history_ids],
            }]})
        if kwargs["q"] != self.full_query:
            assert kwargs["q"] == f"({self.full_query}) after:1699999999 before:1700000001"
            assert kwargs["labelIds"] == self.expected_labels
            assert kwargs["includeSpamTrash"] is False
            matches = [{"id": self.last_get_id}] if self.last_get_id in self.query_matches else []
            return Request({"messages": matches})
        assert kwargs["labelIds"] == self.expected_labels
        assert kwargs["includeSpamTrash"] is False
        self.full_list_calls += 1
        if not kwargs["pageToken"]:
            return Request({"messages": [{"id": self.ids[0]}], "nextPageToken": "next"})
        return Request({"messages": [{"id": i} for i in self.ids[1:]]})

    def get(self, **kwargs):
        assert kwargs["format"] == "metadata"
        self.last_get_id = kwargs["id"]
        self.get_ids.append(kwargs["id"])
        return Request({"threadId": "thread", "internalDate": "1700000000000", "snippet": "s" * 600,
                        "labelIds": self.message_labels.get(kwargs["id"], ["job-alerts"]),
                        "payload": {"headers": [{"name": "Subject", "value": "Job"}, {"name": "Message-ID", "value": "<id>"}]}})

@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAIL_CREDENTIAL_DIR", str(tmp_path))
    monkeypatch.setenv("GMAIL_QUERY", "jobs")
    monkeypatch.setenv("GMAIL_LABELS", "job-alerts")
    (tmp_path / "mailbox").write_text("user@example.com\n")
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
    gmail.history_ids = [
        "spam", "trash", "missing-labels", "c", "d", "wrong-query", "wrong-label",
    ]
    gmail.label_history_ids = ["label-only"]
    gmail.query_matches.update({"c", "d", "wrong-label"})
    gmail.message_labels["spam"] = ["SPAM"]
    gmail.message_labels["trash"] = ["TRASH"]
    gmail.history_without_labels.add("missing-labels")
    gmail.message_labels["wrong-label"] = ["other"]
    assert sync_gmail(db) == {"added": 2, "mode": "incremental"}
    assert gmail.full_list_calls == 2
    assert "spam" not in gmail.get_ids
    assert "trash" not in gmail.get_ids
    assert "missing-labels" not in gmail.get_ids
    assert db.scalar(
        select(GmailMessage.id).where(GmailMessage.provider_message_id == "label-only")
    ) is None
    assert gmail.history_start == ["100", "100"]
    assert sync_gmail(db) == {"added": 0, "mode": "incremental"}
    assert gmail.full_list_calls == 2
    gmail.expired = True
    gmail.ids += ["e"]
    gmail.query_matches.add("e")
    assert sync_gmail(db) == {"added": 1, "mode": "full"}
    assert db.scalar(select(func.count(GmailMessage.id))) == 5
    assert db.get(GmailSyncState, 1).history_id == "100"

def test_sync_rejects_a_different_mailbox_without_mutating_state(setup, tmp_path):
    gmail, db = setup
    (tmp_path / "mailbox").write_text("other@example.com\n")
    with pytest.raises(GmailAuthError, match="account switching is not supported"):
        sync_gmail(db)
    assert db.get(GmailSyncState, 1) is None
    assert db.scalar(select(func.count(GmailMessage.id))) == 0
    assert gmail.full_list_calls == 0

def test_sync_requires_callback_mailbox_binding(setup, tmp_path):
    gmail, db = setup
    (tmp_path / "mailbox").unlink()
    with pytest.raises(GmailAuthError, match="identity is missing"):
        sync_gmail(db)
    assert not (tmp_path / "mailbox").exists()
    assert db.get(GmailSyncState, 1) is None
    assert gmail.full_list_calls == 0

def test_filter_change_stays_history_only(setup, monkeypatch):
    gmail, db = setup
    assert sync_gmail(db) == {"added": 2, "mode": "full"}
    monkeypatch.setenv("GMAIL_QUERY", "from:alerts@example.com")
    assert sync_gmail(db) == {"added": 0, "mode": "incremental"}
    assert gmail.full_list_calls == 2

@pytest.mark.parametrize(
    ("current_labels", "query_matches"),
    [
        (["SPAM"], True),
        (["TRASH"], True),
        (["other"], True),
        (["job-alerts"], False),
    ],
)
def test_full_sync_rechecks_current_filters_before_storing(
    setup, current_labels, query_matches
):
    gmail, db = setup
    gmail.message_labels["a"] = current_labels
    if not query_matches:
        gmail.query_matches.remove("a")
    assert sync_gmail(db) == {"added": 1, "mode": "full"}
    assert db.scalar(
        select(GmailMessage.id).where(GmailMessage.provider_message_id == "a")
    ) is None
    assert db.scalar(
        select(GmailMessage.id).where(GmailMessage.provider_message_id == "b")
    ) is not None

@pytest.mark.parametrize("query", ['subject:"after: interview"', "subject:after:interview"])
def test_operator_like_subject_queries_are_accepted(setup, monkeypatch, query):
    gmail, db = setup
    monkeypatch.setenv("GMAIL_QUERY", query)
    gmail.full_query = query
    assert sync_gmail(db) == {"added": 2, "mode": "full"}

@pytest.mark.parametrize(
    ("query", "labels", "message"),
    [
        ("older_than:1d", "", "Time-relative"),
        ("newer_than:2d", "", "Time-relative"),
        ("after:2026/01/01", "", "Time-relative"),
        ("before:2026/01/01", "", "Time-relative"),
        ("older:2026/01/01", "", "Time-relative"),
        ("newer:2026/01/01", "", "Time-relative"),
        ("-older_than:1d", "", "Time-relative"),
        ("in:spam", "", "Spam or Trash"),
        ("label:trash", "", "Spam or Trash"),
        ("in:anywhere", "", "Spam or Trash"),
        ("", "SPAM", "Spam or Trash"),
        ("", "TRASH", "Spam or Trash"),
    ],
)
def test_unsupported_gmail_filters_are_rejected_before_provider_access(
    setup, monkeypatch, query, labels, message
):
    gmail, db = setup
    monkeypatch.setenv("GMAIL_QUERY", query)
    monkeypatch.setenv("GMAIL_LABELS", labels)
    with pytest.raises(GmailSyncError, match=message):
        sync_gmail(db)
    assert gmail.full_list_calls == 0

@pytest.mark.parametrize(
    ("now", "expected_seconds"),
    [
        (datetime(2026, 1, 15, 5, 30, tzinfo=ZoneInfo("America/New_York")), 1800),
        (datetime(2026, 3, 7, 7, 0, tzinfo=ZoneInfo("America/New_York")), 22 * 3600),
        (datetime(2026, 10, 31, 7, 0, tzinfo=ZoneInfo("America/New_York")), 24 * 3600),
    ],
)
def test_daily_gmail_schedule_tracks_eastern_dst(now, expected_seconds):
    assert seconds_until_gmail_sync(now) == expected_seconds

def test_revoked_credentials_are_explicit(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAIL_CREDENTIAL_DIR", str(tmp_path))
    (tmp_path / "token.json").write_text("{}")
    class Credentials:
        valid = False
        refresh_token = "revoked"
        def refresh(self, request):
            raise RefreshError("invalid_grant: revoked", {"error": "invalid_grant"})
    monkeypatch.setattr("app.gmail_auth.Credentials.from_authorized_user_file", lambda *args: Credentials())
    with pytest.raises(GmailAuthError, match="revoked"):
        load_credentials()
    monkeypatch.setattr("app.main.init_db", lambda: None)
    with TestClient(app) as client:
        response = client.get("/api/gmail/status")
        assert response.status_code == 401
        assert "revoked" in response.json()["detail"]

def test_oauth_flow_checks_state_and_saves_credentials(monkeypatch, tmp_path):
    class Flow:
        def __init__(self, redirect_uri):
            self.redirect_uri = redirect_uri

        credentials = SimpleNamespace(refresh_token="refresh")

        def authorization_url(self, **kwargs):
            assert kwargs["access_type"] == "offline"
            query = urlencode({"redirect_uri": self.redirect_uri})
            return f"https://accounts.google.com/test?{query}", "random-state"

        def fetch_token(self, **kwargs):
            assert kwargs == {"code": "code", "timeout": 10}

    def flow_from_file(*args, **kwargs):
        return Flow(kwargs["redirect_uri"])

    saved = []
    monkeypatch.setenv("GMAIL_CREDENTIAL_DIR", str(tmp_path))
    monkeypatch.setattr("app.gmail_auth.Flow.from_client_secrets_file", flow_from_file)
    monkeypatch.setattr("app.gmail_api.mailbox_email", lambda credentials: "user@example.com")
    monkeypatch.setattr("app.gmail_api.save_credentials", saved.append)
    monkeypatch.setattr("app.main.init_db", lambda: None)
    with TestClient(app) as client:
        authorization_url = client.post("/api/gmail/connect").json()["authorization_url"]
        assert parse_qs(urlparse(authorization_url).query)["redirect_uri"] == ["http://127.0.0.1:8000/"]
        assert client.get("/", params={"state": "wrong", "code": "code"}).status_code == 400
        assert client.get("/", params={"state": "random-state", "code": "code"}).json() == {"connected": True}
        assert client.get("/", params={"state": "random-state", "code": "code"}).status_code == 400
        assert client.post("/api/gmail/connect").status_code == 200
        assert client.get("/", params={"state": "random-state", "code": "code"}).json() == {"connected": True}
    assert len(saved) == 2
    assert (tmp_path / "mailbox").read_text() == "user@example.com\n"

def test_oauth_rejects_a_different_mailbox_before_saving(monkeypatch, tmp_path):
    class Flow:
        credentials = SimpleNamespace(refresh_token="refresh")

        def authorization_url(self, **kwargs):
            return "https://accounts.google.com/test", "switch-state"

        def fetch_token(self, **kwargs):
            assert kwargs == {"code": "code", "timeout": 10}

    saved = []
    monkeypatch.setenv("GMAIL_CREDENTIAL_DIR", str(tmp_path))
    (tmp_path / "mailbox").write_text("first@example.com\n")
    monkeypatch.setattr("app.gmail_api.new_flow", Flow)
    monkeypatch.setattr("app.gmail_api.mailbox_email", lambda credentials: "second@example.com")
    monkeypatch.setattr("app.gmail_api.save_credentials", saved.append)
    monkeypatch.setattr("app.main.init_db", lambda: None)
    with TestClient(app) as client:
        assert client.post("/api/gmail/connect").status_code == 200
        response = client.get("/", params={"state": "switch-state", "code": "code"})
    assert response.status_code == 400
    assert "account switching is not supported" in response.json()["detail"]
    assert saved == []
    assert (tmp_path / "mailbox").read_text() == "first@example.com\n"

def test_concurrent_oauth_callbacks_cannot_switch_mailboxes(monkeypatch, tmp_path):
    class Flow:
        def __init__(self, email):
            self.credentials = SimpleNamespace(refresh_token="refresh", email=email)

        def fetch_token(self, **kwargs):
            assert kwargs == {"code": "code", "timeout": 10}

    barrier = threading.Barrier(2)
    saved = []
    real_mailbox_needs_binding = gmail_api.mailbox_needs_binding

    def mailbox_email(credentials):
        barrier.wait()
        return credentials.email

    def slow_mailbox_needs_binding(email):
        needs_binding = real_mailbox_needs_binding(email)
        if needs_binding:
            time.sleep(0.05)
        return needs_binding

    monkeypatch.setenv("GMAIL_CREDENTIAL_DIR", str(tmp_path))
    monkeypatch.setattr("app.gmail_api.mailbox_email", mailbox_email)
    monkeypatch.setattr("app.gmail_api.mailbox_needs_binding", slow_mailbox_needs_binding)
    monkeypatch.setattr("app.gmail_api.save_credentials", saved.append)
    monkeypatch.setattr("app.main.init_db", lambda: None)
    with gmail_api._oauth_lock:
        gmail_api._pending.clear()
        gmail_api._pending.update({
            "first-state": (Flow("first@example.com"), time.monotonic()),
            "second-state": (Flow("second@example.com"), time.monotonic()),
        })
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(
            lambda state: client.get("/", params={"state": state, "code": "code"}),
            ["first-state", "second-state"],
        ))
    assert sorted(response.status_code for response in responses) == [200, 400]
    assert len(saved) == 1
    assert (tmp_path / "mailbox").read_text().strip() == saved[0].email

@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [(InvalidGrantError(), 400), (Timeout(), 502)],
)
def test_oauth_exchange_failures_are_bounded_and_mapped(
    monkeypatch, failure, expected_status
):
    class Flow:
        credentials = SimpleNamespace(refresh_token="refresh")

        def authorization_url(self, **kwargs):
            return "https://accounts.google.com/test", "failure-state"

        def fetch_token(self, **kwargs):
            assert kwargs == {"code": "code", "timeout": 10}
            raise failure

    saved = []
    monkeypatch.setattr("app.gmail_api.new_flow", Flow)
    monkeypatch.setattr("app.gmail_api.mailbox_email", lambda credentials: pytest.fail("profile called"))
    monkeypatch.setattr("app.gmail_api.save_credentials", saved.append)
    monkeypatch.setattr("app.main.init_db", lambda: None)
    with TestClient(app) as client:
        assert client.post("/api/gmail/connect").status_code == 200
        response = client.get("/", params={"state": "failure-state", "code": "code"})
    assert response.status_code == expected_status
    assert saved == []

def test_oauth_profile_transport_failure_returns_502(monkeypatch):
    class Flow:
        credentials = SimpleNamespace(refresh_token="refresh")

        def authorization_url(self, **kwargs):
            return "https://accounts.google.com/test", "transport-state"

        def fetch_token(self, **kwargs):
            assert kwargs == {"code": "code", "timeout": 10}

    class GmailProfile:
        def users(self):
            return self

        def getProfile(self, **kwargs):
            return Request(httplib2.ServerNotFoundError("dns failed"))

    saved = []
    monkeypatch.setattr("app.gmail_api.new_flow", Flow)
    monkeypatch.setattr("app.gmail_api.build", lambda *args, **kwargs: GmailProfile())
    monkeypatch.setattr("app.gmail_api.save_credentials", saved.append)
    monkeypatch.setattr("app.main.init_db", lambda: None)
    with TestClient(app) as client:
        assert client.post("/api/gmail/connect").status_code == 200
        response = client.get("/", params={"state": "transport-state", "code": "code"})
    assert response.status_code == 502
    assert saved == []

@pytest.mark.parametrize(
    "failure",
    [TransportError("network unavailable"), GmailTransportError("refresh unavailable")],
)
def test_status_refresh_transport_failure_returns_502(monkeypatch, tmp_path, failure):
    monkeypatch.setenv("GMAIL_CREDENTIAL_DIR", str(tmp_path))
    (tmp_path / "token.json").write_text("{}")
    monkeypatch.setattr(
        "app.gmail_api.load_credentials",
        lambda: (_ for _ in ()).throw(failure),
    )
    monkeypatch.setattr("app.main.init_db", lambda: None)
    with TestClient(app) as client:
        response = client.get("/api/gmail/status")
    assert response.status_code == 502

def test_sync_invalid_grant_refresh_requires_reconnect(setup, monkeypatch, tmp_path):
    gmail, db = setup
    token = tmp_path / "token.json"
    token.write_text("stored credentials")
    db.add(GmailSyncState(id=1, history_id="saved-cursor"))
    db.commit()
    gmail.profile_result = RefreshError(
        "invalid_grant: revoked", {"error": "invalid_grant"}
    )
    monkeypatch.setattr("app.main.init_db", lambda: None)
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as client:
            response = client.post("/api/gmail/sync")
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert response.status_code == 401
    assert "reconnect Gmail" in response.json()["detail"]
    assert token.read_text() == "stored credentials"
    assert db.get(GmailSyncState, 1).history_id == "saved-cursor"
    assert db.scalar(select(func.count(GmailMessage.id))) == 0

def test_sync_transient_refresh_failure_is_retryable(setup, monkeypatch, tmp_path):
    gmail, db = setup
    token = tmp_path / "token.json"
    token.write_text("stored credentials")
    db.add(GmailSyncState(id=1, history_id="saved-cursor"))
    db.commit()
    gmail.profile_result = RefreshError(
        "temporarily_unavailable", {"error": "temporarily_unavailable"}, retryable=True
    )
    monkeypatch.setattr("app.main.init_db", lambda: None)
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as client:
            response = client.post("/api/gmail/sync")
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert response.status_code == 502
    assert "try again" in response.json()["detail"]
    assert token.read_text() == "stored credentials"
    assert db.get(GmailSyncState, 1).history_id == "saved-cursor"
    assert db.scalar(select(func.count(GmailMessage.id))) == 0

def test_sync_transport_failure_returns_502(monkeypatch):
    monkeypatch.setattr(
        "app.gmail_api.sync_gmail",
        lambda db: (_ for _ in ()).throw(GmailTransportError("network unavailable")),
    )
    monkeypatch.setattr("app.main.init_db", lambda: None)
    app.dependency_overrides[get_db] = lambda: None
    try:
        with TestClient(app) as client:
            response = client.post("/api/gmail/sync")
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert response.status_code == 502
