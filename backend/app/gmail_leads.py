import base64
import html
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlsplit


class Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'a':
            self.urls.extend(value for key, value in attrs if key.lower() == 'href' and value)


def canonical_url(url):
    for _ in range(4):
        try:
            parts = urlsplit(html.unescape(url.strip()))
            if parts.scheme.lower() not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
                return None
            host = parts.hostname.lower().rstrip('.')
            path = unquote(parts.path)
            if host == 'linkedin.com' or host.endswith('.linkedin.com'):
                match = re.search(r'/jobs/view/(?:[^/]*-)?(\d+)(?:/|$)', path)
                if match:
                    return f'https://www.linkedin.com/jobs/view/{match.group(1)}/'
            if host == 'builtin.com' or host.endswith('.builtin.com'):
                match = re.search(r'^/job/([^/]+)/(\d+)/?$', path)
                if match:
                    return f'https://builtin.com/job/{match.group(1)}/{match.group(2)}'
                match = re.search(r'^/job/([^/]*\d[^/]*)/?$', path)
                if match:
                    return f'https://builtin.com/job/{match.group(1)}'
            params = parse_qs(parts.query)
            nested = next((params[k][0] for k in ('url', 'redirect', 'redirect_url', 'target', 'dest', 'destination') if params.get(k)), None)
            if not nested:
                return None
            url = nested
        except (ValueError, UnicodeError):
            return None
    return None


def message_links(payload):
    urls = []
    def visit(part):
        mime = part.get('mimeType', '')
        data = part.get('body', {}).get('data')
        if data and mime in ('text/html', 'text/plain'):
            try:
                content = base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)).decode('utf-8', errors='replace')
            except (ValueError, TypeError):
                content = ''
            if mime == 'text/html':
                parser = Links()
                parser.feed(content)
                urls.extend(parser.urls)
            else:
                urls.extend(re.findall(r'https?://[^\s<>"\']+', content))
        for child in part.get('parts', []):
            visit(child)
    visit(payload)
    return {canonical for url in urls if (canonical := canonical_url(url.rstrip('.,);')))}
