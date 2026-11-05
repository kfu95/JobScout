from sqlalchemy import select

from .gmail_auth import gmail_transport_call
from .gmail_classify import classify
from .gmail_leads import message_links
from .models import GmailMessage, Lead, now


def process_pending(db, messages):
    processed = 0
    pending = db.scalars(select(GmailMessage).where(GmailMessage.classification_status == 'pending')).all()
    for message in pending:
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
            for url in message_links(full.get('payload', {})):
                if not db.scalar(select(Lead.id).where(Lead.url == url)):
                    source = 'LinkedIn' if 'linkedin.com' in url else 'Built In'
                    db.add(Lead(source=source, title='Job listing', company='', url=url))
                    db.flush()
        message.classification_status = category
        message.processed_at = now()
        processed += 1
    db.commit()
    return processed
