import time
from threading import Lock

from fastapi import APIRouter, Depends, HTTPException
from google.auth.exceptions import GoogleAuthError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from oauthlib.oauth2 import OAuth2Error
from requests.exceptions import RequestException
from sqlalchemy.orm import Session

from .db import get_db
from .gmail_auth import (
    GmailAuthError,
    GmailTransportError,
    gmail_transport_call,
    load_credentials,
    mailbox_needs_binding,
    new_flow,
    save_credentials,
    save_mailbox,
    token_path,
)
from .gmail_sync import GmailSyncError, sync_gmail

router = APIRouter(prefix="/api/gmail", tags=["gmail"])
callback_router = APIRouter(tags=["gmail"])
_pending = {}
_oauth_lock = Lock()
TOKEN_EXCHANGE_TIMEOUT = 10

def mailbox_email(credentials):
    service = gmail_transport_call(
        lambda: build("gmail", "v1", credentials=credentials, cache_discovery=False)
    )
    profile = gmail_transport_call(
        lambda: service.users().getProfile(userId="me").execute()
    )
    email = profile.get("emailAddress", "").strip()
    if not email:
        raise GmailAuthError("Google did not identify the Gmail mailbox")
    return email

@router.post("/connect")
def connect():
    try:
        flow = new_flow()
        url, state = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="false")
        with _oauth_lock:
            _pending.clear()
            _pending[state] = (flow, time.monotonic())
        return {"authorization_url": url}
    except GmailAuthError as exc:
        raise HTTPException(400, str(exc)) from exc

@callback_router.get("/")
def callback(state: str, code: str = "", error: str = ""):
    with _oauth_lock:
        pending = _pending.pop(state, None)
    if not pending or time.monotonic() - pending[1] > 600:
        raise HTTPException(400, "OAuth state is invalid or expired; start again")
    if error or not code:
        raise HTTPException(400, "Gmail authorization was denied")
    try:
        flow = pending[0]
        flow.fetch_token(code=code, timeout=TOKEN_EXCHANGE_TIMEOUT)
        if not flow.credentials.refresh_token:
            raise GmailAuthError("Google did not return a refresh token; reconnect Gmail")
        email = mailbox_email(flow.credentials)
        with _oauth_lock:
            needs_binding = mailbox_needs_binding(email)
            save_credentials(flow.credentials)
            if needs_binding:
                save_mailbox(email)
    except OAuth2Error as exc:
        raise HTTPException(400, "Gmail authorization code is invalid or expired; start again") from exc
    except (RequestException, GmailTransportError, HttpError) as exc:
        raise HTTPException(502, "Gmail authorization service is unavailable; try again") from exc
    except GmailAuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (GoogleAuthError, ValueError) as exc:
        raise HTTPException(400, "Gmail authorization failed; reconnect Gmail") from exc
    return {"connected": True}

@router.get("/status")
def status():
    if not token_path().exists():
        return {"connected": False}
    try:
        load_credentials()
        return {"connected": True}
    except GmailAuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except GoogleAuthError as exc:
        raise HTTPException(502, "Gmail credential refresh failed; try again") from exc

@router.post("/sync")
def sync(db: Session = Depends(get_db)):
    try:
        return sync_gmail(db)
    except GmailAuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except GmailSyncError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (GmailTransportError, HttpError, GoogleAuthError) as exc:
        raise HTTPException(502, "Gmail sync failed; check credentials and provider availability") from exc
