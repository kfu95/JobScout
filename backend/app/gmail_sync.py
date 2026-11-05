import fcntl
import hashlib
import json
import os
from datetime import datetime, timezone

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy import select

from .gmail_auth import directory, load_credentials
from .models import GmailMessage, GmailSyncState

class GmailSyncError(Exception):
    pass

def filters():
    query = os.getenv("GMAIL_QUERY", "").strip()
    labels = [label.strip() for label in os.getenv("GMAIL_LABELS", "").split(",") if label.strip()]
    if not query and not labels:
        raise GmailSyncError("Set GMAIL_QUERY or GMAIL_LABELS before syncing")
    return query, labels

def pages(method, key, **kwargs):
    token = None
    while True:
        page = method(pageToken=token, **kwargs).execute()
        yield from page.get(key, [])
        token = page.get("nextPageToken")
        if not token:
            break

def sync_gmail(db):
    query, labels = filters()
    directory().mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = directory() / ".sync.lock"
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _sync(db, query, labels)

def _sync(db, query, labels):
    service = build("gmail", "v1", credentials=load_credentials(), cache_discovery=False)
    users = service.users()
    messages = users.messages()
    filter_hash = hashlib.sha256(json.dumps([query, sorted(labels)]).encode()).hexdigest()
    state = db.get(GmailSyncState, 1)
    try:
        # Capture the watermark before listing, so changes during listing are replayed.
        watermark = users.getProfile(userId="me").execute()["historyId"]
        candidates = None
        if state and state.filter_hash == filter_hash:
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
        matching = pages(messages.list, "messages", userId="me", q=query or None,
                         labelIds=labels or None, includeSpamTrash=False)
        ids = {item["id"] for item in matching}
        # A search query can start matching without a Gmail history event.
        # Check all matching IDs while fetching metadata only for unseen messages.
        count = 0
        for message_id in ids:
            if db.scalar(select(GmailMessage.id).where(GmailMessage.provider_message_id == message_id)):
                continue
            try:
                message = messages.get(userId="me", id=message_id, format="metadata",
                                       metadataHeaders=["Message-ID", "Subject"]).execute()
            except HttpError as exc:
                if exc.resp.status == 404:
                    continue
                raise
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
        return {"added": count, "mode": "full" if candidates is None else "incremental"}
    except Exception:
        db.rollback()
        raise
