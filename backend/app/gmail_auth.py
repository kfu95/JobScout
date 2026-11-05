import json
import os
import tempfile
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
REDIRECT_URI = "http://127.0.0.1:8000/"

class GmailAuthError(Exception):
    pass

def directory():
    return Path(os.getenv("GMAIL_CREDENTIAL_DIR", "./config/gmail"))

def token_path():
    return directory() / "token.json"

def new_flow(state=None):
    try:
        return Flow.from_client_secrets_file(
            str(directory() / "client_secret.json"), scopes=SCOPES,
            redirect_uri=REDIRECT_URI, state=state, autogenerate_code_verifier=True,
        )
    except (OSError, ValueError) as exc:
        raise GmailAuthError("Gmail desktop client configuration is missing or invalid") from exc

def save_credentials(credentials):
    path = token_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".token-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(credentials.to_json())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def load_credentials():
    try:
        credentials = Credentials.from_authorized_user_file(str(token_path()), SCOPES)
        if not credentials.valid:
            if not credentials.refresh_token:
                raise GmailAuthError("Gmail credentials expired; reconnect Gmail")
            credentials.refresh(Request())
            save_credentials(credentials)
        if not credentials.has_scopes(SCOPES):
            raise GmailAuthError("Gmail read-only permission missing; reconnect Gmail")
        return credentials
    except FileNotFoundError as exc:
        raise GmailAuthError("Gmail is not connected") from exc
    except (ValueError, json.JSONDecodeError, RefreshError) as exc:
        raise GmailAuthError("Gmail credentials are invalid or revoked; reconnect Gmail") from exc
