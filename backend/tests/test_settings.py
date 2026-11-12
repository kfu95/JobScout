import json

import pytest

from app.settings import IntegrationSettings, load_settings, save_settings, setting


def test_settings_persist_and_override_environment(monkeypatch):
    monkeypatch.setenv("USAJOBS_API_KEY", "environment")
    assert setting("USAJOBS_API_KEY") == "environment"
    save_settings(IntegrationSettings(usajobs_api_key="browser", smtp_password="secret"))
    save_settings(IntegrationSettings(usajobs_email="person@example.com"))
    assert setting("USAJOBS_API_KEY") == "browser"
    assert load_settings()["smtp_password"] == "secret"


def test_gmail_client_is_validated_and_saved_privately(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAIL_CREDENTIAL_DIR", str(tmp_path / "gmail"))
    with pytest.raises(ValueError):
        save_settings(IntegrationSettings(gmail_client_json="{}"))
    assert not load_settings()
    client = json.dumps({"installed": {"client_id": "id", "client_secret": "secret"}})
    save_settings(IntegrationSettings(gmail_client_json=client))
    path = tmp_path / "gmail" / "client_secret.json"
    assert path.read_text() == client
    assert path.stat().st_mode & 0o777 == 0o600
