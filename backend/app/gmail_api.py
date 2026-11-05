import time

from fastapi import APIRouter, Depends, HTTPException
from google.auth.exceptions import GoogleAuthError
from googleapiclient.errors import HttpError
from sqlalchemy.orm import Session

from .db import get_db
from .gmail_auth import GmailAuthError, load_credentials, new_flow, save_credentials, token_path
from .gmail_sync import GmailSyncError, sync_gmail

router = APIRouter(prefix="/api/gmail", tags=["gmail"])
_pending = {}

@router.post("/connect")
def connect():
    try:
        flow = new_flow()
        url, state = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="false")
        _pending.clear()
        _pending[state] = (flow, time.monotonic())
        return {"authorization_url": url}
    except GmailAuthError as exc:
        raise HTTPException(400, str(exc)) from exc

@router.get("/callback")
def callback(state: str, code: str = "", error: str = ""):
    pending = _pending.pop(state, None)
    if not pending or time.monotonic() - pending[1] > 600:
        raise HTTPException(400, "OAuth state is invalid or expired; start again")
    if error or not code:
        raise HTTPException(400, "Gmail authorization was denied")
    try:
        flow = pending[0]
        flow.fetch_token(code=code)
        if not flow.credentials.refresh_token:
            raise GmailAuthError("Google did not return a refresh token; reconnect Gmail")
        save_credentials(flow.credentials)
    except (GoogleAuthError, ValueError, GmailAuthError) as exc:
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

@router.post("/sync")
def sync(db: Session = Depends(get_db)):
    try:
        return sync_gmail(db)
    except GmailAuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except GmailSyncError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (HttpError, GoogleAuthError) as exc:
        raise HTTPException(502, "Gmail sync failed; check credentials and provider availability") from exc
