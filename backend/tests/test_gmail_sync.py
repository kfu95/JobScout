from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError
from sqlalchemy import func, select

from app.gmail_auth import GmailAuthError
from app.gmail_sync import GmailSyncError, sync_gmail
from app.models import GmailMessage, GmailSyncState


def test_full_incremental_expiry_and_idempotence(gmail_setup):
    gmail, db = gmail_setup
    assert sync_gmail(db) == {"added": 2, "mode": "full"}
    assert len(
        db.scalar(
            select(GmailMessage).where(GmailMessage.provider_message_id == "a")
        ).snippet
    ) == 500
    assert gmail.full_list_calls == 2
    gmail.history_ids = [
        "spam",
        "trash",
        "history-contract",
        "c",
        "d",
        "wrong-query",
        "wrong-label",
    ]
    gmail.label_history_ids = ["label-only"]
    gmail.query_matches.update({"history-contract", "c", "d", "wrong-label"})
    gmail.message_labels["spam"] = ["SPAM"]
    gmail.message_labels["trash"] = ["TRASH"]
    gmail.message_labels["wrong-label"] = ["other"]
    assert sync_gmail(db) == {"added": 3, "mode": "incremental"}
    assert gmail.full_list_calls == 2
    assert set(gmail.history_ids).issubset(gmail.preflight_ids)
    assert "spam" not in gmail.get_ids
    assert "trash" not in gmail.get_ids
    assert "history-contract" in gmail.get_ids
    assert db.scalar(
        select(GmailMessage.id).where(
            GmailMessage.provider_message_id == "history-contract"
        )
    ) is not None
    assert db.scalar(
        select(GmailMessage.id).where(
            GmailMessage.provider_message_id == "label-only"
        )
    ) is None
    assert "label-only" not in gmail.preflight_ids
    assert gmail.history_start == ["100", "100"]
    assert sync_gmail(db) == {"added": 0, "mode": "incremental"}
    assert gmail.full_list_calls == 2
    gmail.expired = True
    gmail.ids += ["e"]
    gmail.query_matches.add("e")
    assert sync_gmail(db) == {"added": 1, "mode": "full"}
    assert db.scalar(select(func.count(GmailMessage.id))) == 6
    assert db.get(GmailSyncState, 1).history_id == "100"


def test_sync_rejects_a_different_mailbox_without_mutating_state(
    gmail_setup, tmp_path
):
    gmail, db = gmail_setup
    (tmp_path / "mailbox").write_text("other@example.com\n")
    with pytest.raises(GmailAuthError, match="account switching is not supported"):
        sync_gmail(db)
    assert db.get(GmailSyncState, 1) is None
    assert db.scalar(select(func.count(GmailMessage.id))) == 0
    assert gmail.full_list_calls == 0


def test_sync_requires_callback_mailbox_binding(gmail_setup, tmp_path):
    gmail, db = gmail_setup
    (tmp_path / "mailbox").unlink()
    with pytest.raises(GmailAuthError, match="identity is missing"):
        sync_gmail(db)
    assert not (tmp_path / "mailbox").exists()
    assert db.get(GmailSyncState, 1) is None
    assert gmail.full_list_calls == 0


def test_filter_change_stays_history_only(gmail_setup, monkeypatch):
    gmail, db = gmail_setup
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
    gmail_setup, current_labels, query_matches
):
    gmail, db = gmail_setup
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
    assert set(gmail.ids).issubset(gmail.preflight_ids)
    if set(current_labels).intersection({"SPAM", "TRASH"}):
        assert "a" not in gmail.get_ids


@pytest.mark.parametrize("stage", ["creation", "execution"])
def test_missing_preflight_candidate_advances_history_cursor(gmail_setup, stage):
    gmail, db = gmail_setup
    assert sync_gmail(db) == {"added": 2, "mode": "full"}
    gmail.profile_result["historyId"] = "200"
    gmail.history_ids = ["deleted", "later"]
    gmail.query_matches.add("later")
    error = HttpError(SimpleNamespace(status=404, reason="missing"), b"{}")
    errors = (
        gmail.preflight_creation_errors
        if stage == "creation"
        else gmail.preflight_errors
    )
    errors["deleted"] = error
    assert sync_gmail(db) == {"added": 1, "mode": "incremental"}
    assert db.get(GmailSyncState, 1).history_id == "200"
    assert db.scalar(
        select(GmailMessage.id).where(
            GmailMessage.provider_message_id == "deleted"
        )
    ) is None
    assert db.scalar(
        select(GmailMessage.id).where(GmailMessage.provider_message_id == "later")
    ) is not None


def test_transient_preflight_failure_preserves_history_cursor(gmail_setup):
    gmail, db = gmail_setup
    assert sync_gmail(db) == {"added": 2, "mode": "full"}
    gmail.profile_result["historyId"] = "200"
    gmail.history_ids = ["unavailable"]
    gmail.preflight_errors["unavailable"] = HttpError(
        SimpleNamespace(status=503, reason="unavailable"), b"{}"
    )
    with pytest.raises(HttpError):
        sync_gmail(db)
    assert db.get(GmailSyncState, 1).history_id == "100"
    assert db.scalar(
        select(GmailMessage.id).where(
            GmailMessage.provider_message_id == "unavailable"
        )
    ) is None


@pytest.mark.parametrize(
    "query", ['subject:"after: interview"', "subject:after:interview"]
)
def test_operator_like_subject_queries_are_accepted(
    gmail_setup, monkeypatch, query
):
    gmail, db = gmail_setup
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
    gmail_setup, monkeypatch, query, labels, message
):
    gmail, db = gmail_setup
    monkeypatch.setenv("GMAIL_QUERY", query)
    monkeypatch.setenv("GMAIL_LABELS", labels)
    with pytest.raises(GmailSyncError, match=message):
        sync_gmail(db)
    assert gmail.full_list_calls == 0
