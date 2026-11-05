import json
import os
import tempfile
from pathlib import Path

import httplib2
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
REDIRECT_URI = "http://127.0.0.1:8000/"

class GmailAuthError(Exception):
    pass

class GmailTransportError(Exception):
    pass

def gmail_transport_call(call):
    try:
        return call()
    except RefreshError as exc:
        response = next((arg for arg in exc.args if isinstance(arg, dict)), {})
        if response.get("error") == "invalid_grant":
            raise GmailAuthError(
                "Gmail credentials are invalid or revoked; reconnect Gmail"
            ) from exc
        raise GmailTransportError("Gmail credential refresh failed; try again") from exc
    except (httplib2.HttpLib2Error, OSError, TransportError) as exc:
        raise GmailTransportError("Gmail provider transport failed") from exc

def directory():
    return Path(os.getenv("GMAIL_CREDENTIAL_DIR", "./config/gmail"))

def token_path():
    return directory() / "token.json"

def mailbox_path():
    return directory() / "mailbox"

def new_flow(state=None):
    try:
        return Flow.from_client_secrets_file(
            str(directory() / "client_secret.json"), scopes=SCOPES,
            redirect_uri=REDIRECT_URI, state=state, autogenerate_code_verifier=True,
        )
    except (OSError, ValueError) as exc:
        raise GmailAuthError("Gmail desktop client configuration is missing or invalid") from exc

def atomic_write(path, value, prefix):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=prefix)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(value)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def save_credentials(credentials):
    atomic_write(token_path(), credentials.to_json(), ".token-")

def mailbox_needs_binding(email):
    try:
        existing = mailbox_path().read_text().strip()
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise GmailAuthError("Gmail mailbox identity cannot be read") from exc
    if not existing:
        raise GmailAuthError("Gmail mailbox identity is invalid")
    if existing.casefold() != email.casefold():
        raise GmailAuthError(
            "Gmail is already connected to a different mailbox; account switching is not supported"
        )
    return False

def save_mailbox(email):
    email = email.strip()
    if not email:
        raise GmailAuthError("Google did not identify the Gmail mailbox")
    atomic_write(mailbox_path(), email + "\n", ".mailbox-")

def load_credentials():
    try:
        credentials = Credentials.from_authorized_user_file(str(token_path()), SCOPES)
        if not credentials.valid:
            if not credentials.refresh_token:
                raise GmailAuthError("Gmail credentials expired; reconnect Gmail")
            gmail_transport_call(lambda: credentials.refresh(Request()))
            save_credentials(credentials)
        if not credentials.has_scopes(SCOPES):
            raise GmailAuthError("Gmail read-only permission missing; reconnect Gmail")
        return credentials
    except FileNotFoundError as exc:
        raise GmailAuthError("Gmail is not connected") from exc
    except (ValueError, json.JSONDecodeError) as exc:
        raise GmailAuthError("Gmail credentials are invalid or revoked; reconnect Gmail") from exc
