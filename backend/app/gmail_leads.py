import base64
import html
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlsplit


class Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'a':
            attrs = dict(attrs)
            self.current = [attrs.get('href', ''), [], attrs.get('title', '')]

    def handle_data(self, data):
        if self.current is not None:
            self.current[1].append(data)

    def handle_endtag(self, tag):
        if tag.lower() == 'a' and self.current is not None:
            url, pieces, title = self.current
            self.links.append((url, ' '.join(' '.join(pieces).split()) or title))
            self.current = None


def canonical_url(url):
    for _ in range(4):
        try:
            parts = urlsplit(html.unescape(url.strip()))
            if parts.scheme.lower() not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
                return None
            host = parts.hostname.lower().rstrip('.')
            path = unquote(parts.path)
            params = parse_qs(parts.query)
            if host == 'linkedin.com' or host.endswith('.linkedin.com'):
                match = re.search(r'/jobs/view/(?:[^/]*-)?(\d+)(?:/|$)', path)
                if match:
                    return f'https://www.linkedin.com/jobs/view/{match.group(1)}'
            if host == 'builtin.com' or host.endswith('.builtin.com'):
                match = re.fullmatch(r'/job/([^/]+)/(\d+)/?', path)
                if match:
                    return f'https://builtin.com/job/{match.group(1)}/{match.group(2)}'
                match = re.fullmatch(r'/job/([^/]*\d[^/]*)/?', path)
                if match:
                    return f'https://builtin.com/job/{match.group(1)}'
            if host == 'indeed.com' or host.endswith('.indeed.com'):
                key = params.get('jk', [''])[0]
                if path in ('/viewjob', '/rc/clk') and re.fullmatch(r'[A-Za-z0-9]+', key):
                    return f'https://www.indeed.com/viewjob?jk={key}'
            if host == 'glassdoor.com' or host.endswith('.glassdoor.com'):
                match = re.search(r'-JL(\d+)\.htm$', path, re.I)
                if match:
                    return f'https://www.glassdoor.com{path}'
                key = params.get('jobListingId', [''])[0]
                if path == '/partner/jobListing.htm' and key.isdigit():
                    return f'https://www.glassdoor.com/partner/jobListing.htm?jobListingId={key}'
            nested = next((params[k][0] for k in ('url', 'redirect', 'redirect_url', 'target', 'dest', 'destination') if params.get(k)), None)
            if not nested:
                return None
            url = nested
        except (ValueError, UnicodeError):
            return None
    return None


def message_links(payload):
    links = []
    def visit(part):
        mime = part.get('mimeType', '')
        data = part.get('body', {}).get('data')
        if data and mime in ('text/html', 'text/plain'):
            try:
                content = base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)).decode('utf-8', errors='replace')
            except (ValueError, TypeError):
                return
            if mime == 'text/html':
                parser = Links()
                parser.feed(content)
                links.extend(parser.links)
            else:
                for line in content.splitlines():
                    for url in re.findall(r'https?://[^\s<>"\']+', line):
                        links.append((url.rstrip('.,);'), line.replace(url, '').strip(' -|:')))
        for child in part.get('parts', []):
            visit(child)
    visit(payload)
    results = {}
    for url, text in links:
        canonical = canonical_url(url)
        if not canonical:
            continue
        fields = re.split(r'\s+(?:at|\|)\s+', text, maxsplit=2, flags=re.I)
        title = fields[0].strip()[:500]
        if not title or title.lower() in {'view job', 'apply', 'view', 'job details', 'see job'}:
            continue
        results[canonical] = {'external_id': canonical, 'title': title,
                              'company': fields[1].strip()[:200] if len(fields) > 1 else 'Unknown',
                              'location': fields[2].strip() if len(fields) > 2 else '',
                              'description': '', 'apply_url': canonical}
    return list(results.values())
