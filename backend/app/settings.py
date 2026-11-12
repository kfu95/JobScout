import json
import os

from pydantic import BaseModel

from .onboarding import storage_dir, write_private
from .gmail_auth import directory


class IntegrationSettings(BaseModel):
    usajobs_api_key: str = ""
    usajobs_email: str = ""
    adzuna_app_id: str = ""
    adzuna_api_key: str = ""
    smtp_host: str = ""
    smtp_port: str = ""
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_to: str = ""
    gmail_client_json: str = ""
    gmail_query: str = ""
    gmail_labels: str = ""


def load_settings():
    path = storage_dir() / "integrations.json"
    return json.loads(path.read_text()) if path.exists() else {}


def save_settings(settings):
    if settings.gmail_client_json:
        try:
            client = json.loads(settings.gmail_client_json)
            installed = client["installed"]
            if not installed.get("client_id") or not installed.get("client_secret"):
                raise ValueError("Missing Gmail client credentials")
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError("Gmail credentials must be a valid Desktop app JSON file") from exc
        directory().mkdir(parents=True, exist_ok=True, mode=0o700)
        write_private(directory() / "client_secret.json", settings.gmail_client_json.encode())
    values = load_settings()
    values.update({key: value for key, value in settings.model_dump().items() if value})
    write_private(storage_dir() / "integrations.json", json.dumps(values).encode())


def setting(name):
    return load_settings().get(name.lower()) or os.getenv(name, "")
