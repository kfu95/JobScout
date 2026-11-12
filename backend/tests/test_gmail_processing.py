import base64
from datetime import datetime, timezone
from urllib.parse import quote

from sqlalchemy import select

from app.collectors import canonical_url
from app.gmail_classify import Classification, classify
from app.gmail_leads import message_links
from app.gmail_process import process_pending
from app.models import Board, GmailMessage, Job
from gmail_fakes import Request


def encoded(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip('=')


def test_classification_and_ai_fallback(monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('GMAIL_AI_CLASSIFY', raising=False)
    for subject, expected in [
        ('New jobs for you', 'job_alert'),
        ('Application received', 'submission_confirmation'),
        ('A recruiter is reaching out', 'recruiter_outreach'),
        ('Schedule your interview', 'interview_invite'),
        ('Unfortunately we are not moving forward', 'rejection'),
        ('Hello', 'other'),
    ]:
        assert classify(subject, '', '') == expected
    assert classify('New jobs for you', 'Get interview tips', '') == 'job_alert'
    assert classify('Jobs', '', 'LinkedIn.com Support <offers@evil.test>') == 'other'
    assert classify('Jobs', '', 'alerts@mail.linkedin.com') == 'job_alert'
    calls = []
    monkeypatch.setenv('OPENAI_API_KEY', 'test')
    monkeypatch.setattr('app.gmail_classify._ai_parse', lambda *args: calls.append(args) or Classification(category='job_alert'))
    assert classify('Hello', '', '') == 'other'
    assert calls == []
    monkeypatch.setenv('GMAIL_AI_CLASSIFY', 'true')
    assert classify('Hello', '', '') == 'job_alert'
    assert len(calls) == 1

    def fail(*args):
        raise RuntimeError('unavailable')
    monkeypatch.setattr('app.gmail_classify._ai_parse', fail)
    assert classify('Hello', '', '') == 'other'


def test_alert_samples_and_wrappers():
    linkedin = 'https://www.linkedin.com/jobs/view/senior-engineer-12345/?trackingId=abc'
    builtin = 'https://builtin.com/job/software-engineer/6789?utm_source=email'
    wrapped = 'https://tracker.example/click?url=' + quote(linkedin, safe='')
    html = f'''<a href="{wrapped}">Senior Engineer at Example | New York</a>
    <a href="{builtin}">Software Engineer at Acme | Remote</a>
    <a href="https://www.indeed.com/viewjob?jk=abc123&amp;from=email">Engineer at A</a>
    <a href="https://www.glassdoor.com/job-listing/engineer-JL987.htm">Engineer at B</a>
    <a href="https://linkedin.com.evil.test/jobs/view/12345">Bad</a>
    <a href="https://[bad">Broken</a>'''
    parts = [
        {'mimeType': 'text/html', 'body': {'data': encoded(html)}},
        {'mimeType': 'text/plain', 'body': {'data': encoded('Senior Engineer https://www.linkedin.com/jobs/view/12345/')}},
    ]
    for ordered_parts in (parts, parts[::-1]):
        jobs = message_links({'mimeType': 'multipart/alternative', 'parts': ordered_parts})
        assert len(jobs) == 4
        linked = next(job for job in jobs if 'linkedin.com' in job['apply_url'])
        assert (linked['title'], linked['company'], linked['location']) == ('Senior Engineer', 'Example', 'New York')
        assert linked['apply_url'] == 'https://www.linkedin.com/jobs/view/12345'
    assert canonical_url('https://builtin.com/job/other/6789') == canonical_url(builtin)
    assert canonical_url('https://www.glassdoor.com/job-listing/other-JL987.htm') == canonical_url('https://www.glassdoor.com/job-listing/engineer-JL987.htm')
    assert canonical_url('https://builtin.com/job/c%23-engineer/6789') == 'https://builtin.com/job/c%23-engineer/6789'
    assert canonical_url('https://builtin.com/job/c%23-engineer-6789') == 'https://builtin.com/job/c%23-engineer-6789'
    assert canonical_url('https://www.glassdoor.com/job-listing/c%23-engineer-JL987.htm') == 'https://www.glassdoor.com/job-listing/c%23-engineer-JL987.htm'


def test_alert_ignores_valueless_anchor_attributes():
    payload = {
        'mimeType': 'text/html',
        'body': {'data': encoded(
            '<a href>Senior Engineer</a>'
            '<a href="https://www.linkedin.com/jobs/view/12345" title></a>'
            '<a href="https://www.linkedin.com/jobs/view/67890">Staff Engineer at Acme | Remote</a>'
        )},
    }
    assert message_links(payload) == [{
        'external_id': 'https://www.linkedin.com/jobs/view/67890',
        'title': 'Staff Engineer',
        'company': 'Acme',
        'location': 'Remote',
        'description': '',
        'apply_url': 'https://www.linkedin.com/jobs/view/67890',
    }]


def test_alert_card_metadata_from_html_and_plaintext():
    linkedin_payload = {
        'mimeType': 'text/html',
        'body': {'data': encoded(
            '<a href="https://www.linkedin.com/jobs/view/12345">Senior Engineer</a>'
            '<div>Acme</div><div>New York</div>'
        )},
    }
    builtin_payload = {
        'mimeType': 'text/plain',
        'body': {'data': encoded(
            'Software Engineer\nAcme\nRemote\nhttps://builtin.com/job/software-engineer/6789'
        )},
    }
    assert message_links(linkedin_payload)[0] == {
        'external_id': 'https://www.linkedin.com/jobs/view/12345',
        'title': 'Senior Engineer',
        'company': 'Acme',
        'location': 'New York',
        'description': '',
        'apply_url': 'https://www.linkedin.com/jobs/view/12345',
    }
    assert message_links(builtin_payload)[0] == {
        'external_id': 'https://builtin.com/job/software-engineer/6789',
        'title': 'Software Engineer',
        'company': 'Acme',
        'location': 'Remote',
        'description': '',
        'apply_url': 'https://builtin.com/job/software-engineer/6789',
    }


def test_alert_rejects_external_id_over_database_limit():
    url = f'https://builtin.com/job/{"x" * 600}1'
    payload = {'mimeType': 'text/html', 'body': {'data': encoded(f'<a href="{url}">Senior Engineer</a>')}}
    assert message_links(payload) == []


def test_processing_deduplicates_and_skips_spam(gmail_setup, monkeypatch):
    _, db = gmail_setup
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.setattr('app.gmail_process.load_preferences', lambda profile: {'target_titles': ['senior engineer'], 'locations': ['New York'], 'work_modes': [], 'seniority': []})
    class Messages:
        def __init__(self):
            self.calls = []
        def get(self, **kwargs):
            self.calls.append((kwargs['id'], kwargs['format']))
            if kwargs['id'] == 'spam':
                return Request({'labelIds': ['SPAM']})
            if kwargs['format'] == 'metadata':
                return Request({'payload': {'headers': [{'name': 'From', 'value': 'alerts@linkedin.com'}]}})
            if kwargs['format'] == 'full':
                url = 'https://www.linkedin.com/jobs/view/12345/?trk=email'
                return Request({'payload': {'mimeType': 'text/html', 'body': {'data': encoded(f'<a href="{url}">Senior Engineer at Example | New York</a>')}}})
            return Request({'labelIds': ['INBOX']})
    messages = Messages()
    for message_id in ('one', 'two', 'spam'):
        db.add(GmailMessage(provider_message_id=message_id, thread_id='thread', subject='New jobs for you', snippet='', received_at=datetime.now(timezone.utc)))
    db.commit()
    assert process_pending(db, messages) == 2
    assert process_pending(db, messages) == 0
    jobs = db.scalars(select(Job)).all()
    assert len(jobs) == 1
    assert jobs[0].title == 'Senior Engineer'
    assert db.get(Board, jobs[0].board_id).company == 'Example'
    assert ('spam', 'full') not in messages.calls
    assert ('spam', 'metadata') not in messages.calls


def test_alert_deduplicates_existing_aggregator_job(gmail_setup, monkeypatch):
    _, db = gmail_setup
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.setattr('app.gmail_process.load_preferences', lambda profile: {'target_titles': ['senior engineer'], 'locations': ['New York'], 'work_modes': [], 'seniority': []})
    board = Board(provider='remotive', slug='example', company='Example')
    db.add(board)
    db.flush()
    db.add(Job(board_id=board.id, external_id='old', title='Senior Engineer', location='New York', description='', apply_url='https://www.linkedin.com/jobs/view/senior-engineer-12345/?trk=source', hash='old'))
    db.add(GmailMessage(provider_message_id='alert', thread_id='t', subject='New jobs for you', snippet='', received_at=datetime.now(timezone.utc)))
    db.commit()
    class Messages:
        def get(self, **kwargs):
            if kwargs['format'] == 'metadata':
                return Request({'payload': {'headers': []}})
            if kwargs['format'] == 'full':
                content = '<a href="https://www.linkedin.com/jobs/view/12345/">Senior Engineer at Example | New York</a>'
                return Request({'payload': {'mimeType': 'text/html', 'body': {'data': encoded(content)}}})
            return Request({'labelIds': ['INBOX']})
    assert process_pending(db, Messages()) == 1
    assert len(db.scalars(select(Job)).all()) == 1
    assert db.scalars(select(Job)).one().board_id == board.id
