import fcntl
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy import select

from .gmail_auth import directory, load_credentials
from .models import GmailMessage, GmailSyncState

SPAM_TRASH_QUERY = re.compile(
    r"(?:^|[\s({])(?:in|label):(spam|trash|anywhere)(?=$|[\s)}])", re.IGNORECASE
)

class GmailSyncError(Exception):
    pass

def filters():
    query = os.getenv("GMAIL_QUERY", "").strip()
    labels = [label.strip() for label in os.getenv("GMAIL_LABELS", "").split(",") if label.strip()]
    if not query and not labels:
        raise GmailSyncError("Set GMAIL_QUERY or GMAIL_LABELS before syncing")
    return query, labels

def reconciliation_interval():
    try:
        seconds = int(os.getenv("GMAIL_RECONCILE_SECONDS", "86400"))
    except ValueError as exc:
        raise GmailSyncError("GMAIL_RECONCILE_SECONDS must be a positive integer") from exc
    if seconds < 1:
        raise GmailSyncError("GMAIL_RECONCILE_SECONDS must be a positive integer")
    return seconds

def pages(method, key, **kwargs):
    token = None
    while True:
        page = method(pageToken=token, **kwargs).execute()
        yield from page.get(key, [])
        token = page.get("nextPageToken")
        if not token:
            break

def spam_trash_requested(query, labels):
    return any(label.upper() in {"SPAM", "TRASH"} for label in labels) or bool(
        SPAM_TRASH_QUERY.search(query)
    )

def reconciliation_due(seconds):
    try:
        last_full_sync = (directory() / ".last-full-sync").stat().st_mtime
    except FileNotFoundError:
        return True
    return time.time() - last_full_sync >= seconds

def mark_reconciled():
    (directory() / ".last-full-sync").touch(mode=0o600)

def matches_filters(messages, message_id, message, query, labels, include_spam_trash):
    message_labels = set(message.get("labelIds", []))
    if not include_spam_trash and message_labels.intersection({"SPAM", "TRASH"}):
        return False
    if not set(labels).issubset(message_labels):
        return False
    if not query:
        return True
    received_at = int(message["internalDate"]) // 1000
    scoped_query = f"({query}) after:{received_at - 1} before:{received_at + 1}"
    matching = pages(messages.list, "messages", userId="me", q=scoped_query,
                     labelIds=labels or None, includeSpamTrash=include_spam_trash)
    return any(item["id"] == message_id for item in matching)

def sync_gmail(db):
    query, labels = filters()
    reconcile_seconds = reconciliation_interval()
    directory().mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = directory() / ".sync.lock"
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _sync(db, query, labels, reconcile_seconds)

def _sync(db, query, labels, reconcile_seconds):
    service = build("gmail", "v1", credentials=load_credentials(), cache_discovery=False)
    users = service.users()
    messages = users.messages()
    filter_hash = hashlib.sha256(json.dumps([query, sorted(labels)]).encode()).hexdigest()
    include_spam_trash = spam_trash_requested(query, labels)
    state = db.get(GmailSyncState, 1)
    try:
        # Capture the watermark before listing, so changes during listing are replayed.
        watermark = users.getProfile(userId="me").execute()["historyId"]
        candidates = None
        if state and state.filter_hash == filter_hash and not reconciliation_due(reconcile_seconds):
            try:
                history = pages(users.history().list, "history", userId="me",
                                startHistoryId=state.history_id)
                candidates = set()
                for record in history:
                    for kind in ("messagesAdded", "labelsAdded", "labelsRemoved"):
                        candidates.update(item["message"]["id"] for item in record.get(kind, []))
            except HttpError as exc:
                if exc.resp.status != 404:
                    raise
                candidates = None
        incremental = candidates is not None
        if not incremental:
            matching = pages(messages.list, "messages", userId="me", q=query or None,
                             labelIds=labels or None, includeSpamTrash=include_spam_trash)
            candidates = {item["id"] for item in matching}
        count = 0
        for message_id in candidates:
            if db.scalar(select(GmailMessage.id).where(GmailMessage.provider_message_id == message_id)):
                continue
            try:
                message = messages.get(userId="me", id=message_id, format="metadata",
                                       metadataHeaders=["Message-ID", "Subject"]).execute()
            except HttpError as exc:
                if exc.resp.status == 404:
                    continue
                raise
            if incremental and not matches_filters(
                messages, message_id, message, query, labels, include_spam_trash
            ):
                continue
            headers = {h["name"].lower(): h["value"] for h in message.get("payload", {}).get("headers", [])}
            db.add(GmailMessage(provider_message_id=message_id, thread_id=message["threadId"],
                                rfc_message_id=headers.get("message-id"), subject=headers.get("subject", ""),
                                received_at=datetime.fromtimestamp(int(message["internalDate"]) / 1000, timezone.utc),
                                snippet=message.get("snippet", "")[:500]))
            count += 1
        if state is None:
            state = GmailSyncState(id=1, history_id=watermark, filter_hash=filter_hash)
            db.add(state)
        else:
            state.history_id, state.filter_hash = watermark, filter_hash
        db.commit()
        if not incremental:
            mark_reconciled()
        return {"added": count, "mode": "incremental" if incremental else "full"}
    except Exception:
        db.rollback()
        raise
