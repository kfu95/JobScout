import fcntl
import os
import re
from datetime import datetime, timezone

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy import select

from .gmail_auth import (
    GmailAuthError,
    directory,
    gmail_transport_call,
    load_credentials,
    mailbox_needs_binding,
)
from .models import GmailMessage, GmailSyncState

SPAM_TRASH_QUERY = re.compile(
    r"(?:^|[\s({])(?:in|label):(spam|trash|anywhere)(?=$|[\s)}])", re.IGNORECASE
)
TIME_RELATIVE_QUERY = re.compile(
    r"(?:^|[\s({])[-+]?(?:after|before|older|newer|older_than|newer_than):",
    re.IGNORECASE,
)
QUOTED_QUERY = re.compile(r'"(?:\\.|[^"\\])*"')

class GmailSyncError(Exception):
    pass

def filters():
    query = os.getenv("GMAIL_QUERY", "").strip()
    labels = [label.strip() for label in os.getenv("GMAIL_LABELS", "").split(",") if label.strip()]
    operators = QUOTED_QUERY.sub("", query)
    if not query and not labels:
        raise GmailSyncError("Set GMAIL_QUERY or GMAIL_LABELS before syncing")
    if any(label.upper() in {"SPAM", "TRASH"} for label in labels) or SPAM_TRASH_QUERY.search(operators):
        raise GmailSyncError("Gmail filters cannot target Spam or Trash")
    if TIME_RELATIVE_QUERY.search(operators):
        raise GmailSyncError("Time-relative Gmail query operators are not supported")
    return query, labels

def pages(method, key, **kwargs):
    token = None
    while True:
        request = gmail_transport_call(lambda: method(pageToken=token, **kwargs))
        page = gmail_transport_call(request.execute)
        yield from page.get(key, [])
        token = page.get("nextPageToken")
        if not token:
            break

def matches_filters(messages, message_id, message, query, labels):
    message_labels = set(message.get("labelIds", []))
    if message_labels.intersection({"SPAM", "TRASH"}):
        return False
    if not set(labels).issubset(message_labels):
        return False
    if not query:
        return True
    received_at = int(message["internalDate"]) // 1000
    scoped_query = f"({query}) after:{received_at - 1} before:{received_at + 1}"
    matching = pages(messages.list, "messages", userId="me", q=scoped_query,
                     labelIds=labels or None, includeSpamTrash=False)
    return any(item["id"] == message_id for item in matching)

def sync_gmail(db):
    query, labels = filters()
    directory().mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = directory() / ".sync.lock"
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _sync(db, query, labels)

def _sync(db, query, labels):
    credentials = load_credentials()
    service = gmail_transport_call(
        lambda: build("gmail", "v1", credentials=credentials, cache_discovery=False)
    )
    users = service.users()
    messages = users.messages()
    try:
        # Capture the watermark before listing, so changes during listing are replayed.
        profile = gmail_transport_call(
            lambda: users.getProfile(userId="me").execute()
        )
        email = profile.get("emailAddress", "").strip()
        if not email:
            raise GmailAuthError("Google did not identify the Gmail mailbox")
        if mailbox_needs_binding(email):
            raise GmailAuthError("Gmail mailbox identity is missing; reconnect Gmail")
        watermark = profile["historyId"]
        state = db.get(GmailSyncState, 1)
        candidates = None
        if state:
            try:
                history = pages(users.history().list, "history", userId="me",
                                startHistoryId=state.history_id)
                candidates = set()
                for record in history:
                    candidates.update(
                        item["message"]["id"] for item in record.get("messagesAdded", [])
                    )
            except HttpError as exc:
                if exc.resp.status != 404:
                    raise
                candidates = None
        incremental = candidates is not None
        if not incremental:
            matching = pages(messages.list, "messages", userId="me", q=query or None,
                             labelIds=labels or None, includeSpamTrash=False)
            candidates = {item["id"] for item in matching}
        count = 0
        for message_id in candidates:
            if db.scalar(select(GmailMessage.id).where(GmailMessage.provider_message_id == message_id)):
                continue
            try:
                preflight_request = gmail_transport_call(
                    lambda: messages.get(
                        userId="me", id=message_id, format="minimal", fields="id,labelIds"
                    )
                )
                preflight = gmail_transport_call(preflight_request.execute)
            except HttpError as exc:
                if exc.resp.status == 404:
                    continue
                raise
            if set(preflight.get("labelIds", [])).intersection({"SPAM", "TRASH"}):
                continue
            try:
                request = gmail_transport_call(
                    lambda: messages.get(
                        userId="me", id=message_id, format="metadata",
                        metadataHeaders=["Message-ID", "Subject"],
                    )
                )
                message = gmail_transport_call(request.execute)
            except HttpError as exc:
                if exc.resp.status == 404:
                    continue
                raise
            if not matches_filters(messages, message_id, message, query, labels):
                continue
            headers = {h["name"].lower(): h["value"] for h in message.get("payload", {}).get("headers", [])}
            db.add(GmailMessage(provider_message_id=message_id, thread_id=message["threadId"],
                                rfc_message_id=headers.get("message-id"), subject=headers.get("subject", ""),
                                received_at=datetime.fromtimestamp(int(message["internalDate"]) / 1000, timezone.utc),
                                snippet=message.get("snippet", "")[:500]))
            count += 1
        if state is None:
            state = GmailSyncState(id=1, history_id=watermark)
            db.add(state)
        else:
            state.history_id = watermark
        db.commit()
        return {"added": count, "mode": "incremental" if incremental else "full"}
    except Exception:
        db.rollback()
        raise
