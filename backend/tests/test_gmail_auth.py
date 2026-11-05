import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlparse

import httplib2
import pytest
from fastapi.testclient import TestClient
from google.auth.exceptions import RefreshError, TransportError
from oauthlib.oauth2 import InvalidGrantError
from requests.exceptions import Timeout
from sqlalchemy import func, select

from app import gmail_api
from app.db import get_db
from app.gmail_auth import GmailAuthError, GmailTransportError, load_credentials
from app.main import app
from app.models import GmailMessage, GmailSyncState
from gmail_fakes import Request


def test_revoked_credentials_are_explicit(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAIL_CREDENTIAL_DIR", str(tmp_path))
    (tmp_path / "token.json").write_text("{}")

    class Credentials:
        valid = False
        refresh_token = "revoked"

        def refresh(self, request):
            raise RefreshError("invalid_grant: revoked", {"error": "invalid_grant"})

    monkeypatch.setattr(
        "app.gmail_auth.Credentials.from_authorized_user_file",
        lambda *args: Credentials(),
    )
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
    monkeypatch.setattr(
        "app.gmail_auth.Flow.from_client_secrets_file", flow_from_file
    )
    monkeypatch.setattr(
        "app.gmail_api.mailbox_email", lambda credentials: "user@example.com"
    )
    monkeypatch.setattr("app.gmail_api.save_credentials", saved.append)
    monkeypatch.setattr("app.main.init_db", lambda: None)
    with TestClient(app) as client:
        authorization_url = client.post("/api/gmail/connect").json()[
            "authorization_url"
        ]
        assert parse_qs(urlparse(authorization_url).query)["redirect_uri"] == [
            "http://127.0.0.1:8000/"
        ]
        assert (
            client.get("/", params={"state": "wrong", "code": "code"}).status_code
            == 400
        )
        assert client.get(
            "/", params={"state": "random-state", "code": "code"}
        ).json() == {"connected": True}
        assert (
            client.get(
                "/", params={"state": "random-state", "code": "code"}
            ).status_code
            == 400
        )
        assert client.post("/api/gmail/connect").status_code == 200
        assert client.get(
            "/", params={"state": "random-state", "code": "code"}
        ).json() == {"connected": True}
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
    monkeypatch.setattr(
        "app.gmail_api.mailbox_email", lambda credentials: "second@example.com"
    )
    monkeypatch.setattr("app.gmail_api.save_credentials", saved.append)
    monkeypatch.setattr("app.main.init_db", lambda: None)
    with TestClient(app) as client:
        assert client.post("/api/gmail/connect").status_code == 200
        response = client.get(
            "/", params={"state": "switch-state", "code": "code"}
        )
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
    monkeypatch.setattr(
        "app.gmail_api.mailbox_needs_binding", slow_mailbox_needs_binding
    )
    monkeypatch.setattr("app.gmail_api.save_credentials", saved.append)
    monkeypatch.setattr("app.main.init_db", lambda: None)
    with gmail_api._oauth_lock:
        gmail_api._pending.clear()
        gmail_api._pending.update(
            {
                "first-state": (Flow("first@example.com"), time.monotonic()),
                "second-state": (Flow("second@example.com"), time.monotonic()),
            }
        )
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda state: client.get(
                    "/", params={"state": state, "code": "code"}
                ),
                ["first-state", "second-state"],
            )
        )
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
    monkeypatch.setattr(
        "app.gmail_api.mailbox_email", lambda credentials: pytest.fail("profile called")
    )
    monkeypatch.setattr("app.gmail_api.save_credentials", saved.append)
    monkeypatch.setattr("app.main.init_db", lambda: None)
    with TestClient(app) as client:
        assert client.post("/api/gmail/connect").status_code == 200
        response = client.get(
            "/", params={"state": "failure-state", "code": "code"}
        )
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
        response = client.get(
            "/", params={"state": "transport-state", "code": "code"}
        )
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


def test_sync_invalid_grant_refresh_requires_reconnect(
    gmail_setup, monkeypatch, tmp_path
):
    gmail, db = gmail_setup
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


def test_sync_transient_refresh_failure_is_retryable(
    gmail_setup, monkeypatch, tmp_path
):
    gmail, db = gmail_setup
    token = tmp_path / "token.json"
    token.write_text("stored credentials")
    db.add(GmailSyncState(id=1, history_id="saved-cursor"))
    db.commit()
    gmail.profile_result = RefreshError(
        "temporarily_unavailable",
        {"error": "temporarily_unavailable"},
        retryable=True,
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
        lambda db: (_ for _ in ()).throw(
            GmailTransportError("network unavailable")
        ),
    )
    monkeypatch.setattr("app.main.init_db", lambda: None)
    app.dependency_overrides[get_db] = lambda: None
    try:
        with TestClient(app) as client:
            response = client.post("/api/gmail/sync")
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert response.status_code == 502
