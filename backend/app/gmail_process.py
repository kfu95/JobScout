from googleapiclient.errors import HttpError
from sqlalchemy import select

from .aggregators import store_aggregator
from .gmail_auth import gmail_transport_call
from .gmail_classify import classify
from .gmail_leads import message_links
from .models import GmailMessage, now
from .preferences import load_preferences
from .scoring import load_profile


def process_pending(db, messages):
    processed = 0
    pending = db.scalars(select(GmailMessage).where(GmailMessage.classification_status == 'pending')).all()
    for message in pending:
        try:
            preflight = gmail_transport_call(lambda: messages.get(userId='me', id=message.provider_message_id, format='minimal', fields='id,labelIds').execute())
            if set(preflight.get('labelIds', [])).intersection({'SPAM', 'TRASH'}):
                continue
            metadata = gmail_transport_call(lambda: messages.get(userId='me', id=message.provider_message_id, format='metadata', metadataHeaders=['From']).execute())
            if set(metadata.get('labelIds', [])).intersection({'SPAM', 'TRASH'}):
                continue
            headers = {h['name'].lower(): h['value'] for h in metadata.get('payload', {}).get('headers', [])}
            category = classify(message.subject, message.snippet, headers.get('from', ''))
            if category == 'job_alert':
                full = gmail_transport_call(lambda: messages.get(userId='me', id=message.provider_message_id, format='full').execute())
                if set(full.get('labelIds', [])).intersection({'SPAM', 'TRASH'}):
                    continue
                postings = message_links(full.get('payload', {}))
                store_aggregator(db, 'gmail', postings, load_preferences(load_profile()))
            message.classification_status = category
            message.processed_at = now()
            db.commit()
            processed += 1
        except HttpError as exc:
            if exc.resp.status != 404:
                raise
            message.classification_status = 'other'
            message.processed_at = now()
            db.commit()
    return processed
