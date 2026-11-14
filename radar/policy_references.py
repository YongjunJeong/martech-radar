"""Bounded public-policy references, independent of browser verdicts and sales signals.

Run explicitly with ``python -m radar.policy_references --only TARGET``.
No browser, account, consent interaction, or detector score is involved.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import hashlib
from html.parser import HTMLParser
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

from . import evidence, security, workspace
from .store import Store

MAX_BYTES = 1_500_000
MAX_DOCUMENTS = 2
TTL = timedelta(days=7)
PARSER_VERSION = 2
POLICY = re.compile(r'개인\s*정보\s*(?:처리|취급)|privacy|쿠키\s*정책|cookie\s*policy', re.I)
HISTORICAL = re.compile(r'이전|과거|개정\s*이력|archive|history', re.I)
SECTION_PATTERNS = (
    ('국외 이전', r'국외|해외.*이전|international transfer|overseas'),
    ('처리위탁', r'위탁|수탁|subprocessor|processor'),
    ('제3자 제공', r'제\s*3\s*자|third.part'),
    ('쿠키·행태정보', r'쿠키|행태\s*정보|cookie|behavio'),
)
# Organization aliases, deliberately not fingerprints: Google != Google Analytics,
# Adobe != Adobe Target. A mention never asserts a product was deployed.
ORGANIZATIONS = {
    'Insider': ('Insider', '인사이더'), 'Braze': ('Braze', '브레이즈'),
    'Bloomreach': ('Bloomreach', '블룸리치', 'Exponea'),
    'MoEngage': ('MoEngage', '모인게이지'), 'CleverTap': ('CleverTap', '클레버탭'),
    'Salesforce': ('Salesforce', '세일즈포스'), 'Adobe': ('Adobe', '어도비'),
    'Google': ('Google', '구글'), 'Meta': ('Meta Platforms', 'Facebook', '페이스북'),
    'Appier': ('Appier', '애피어'), 'Amplitude': ('Amplitude', '앰플리튜드'),
    'Mixpanel': ('Mixpanel', '믹스패널'), 'AppsFlyer': ('AppsFlyer', '앱스플라이어'),
    'Adjust': ('Adjust GmbH', 'Adjust Inc', '애드저스트'),
    'Airbridge': ('Airbridge', '에어브릿지', 'AB180', '에이비일팔공'),
    'Channel Corp.': ('Channel Corp', '채널코퍼레이션', '채널톡'),
    'Datarize': ('Datarize', '데이터라이즈'), 'Criteo': ('Criteo', '크리테오'),
    'Klaviyo': ('Klaviyo', '클라비요'), 'HubSpot': ('HubSpot', '허브스팟'),
    'Twilio': ('Twilio', '트윌리오'), 'Iterable': ('Iterable Inc', 'Iterable, Inc'),
    'OneSignal': ('OneSignal', '원시그널'), 'Emarsys': ('Emarsys', '이마시스'),
    'Dynamic Yield': ('Dynamic Yield', '다이나믹일드'),
}


class PolicyUnavailable(ValueError):
    pass


class PolicyDeferred(Exception):
    """Yield to primary collection without recording a policy fetch failure."""


def clean_url(url):
    p = urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise PolicyUnavailable('unsupported_url')
    if p.port not in (None, 80, 443):
        raise PolicyUnavailable('unsupported_port')
    # Keep only document selectors; drop tracking, account and session parameters.
    from urllib.parse import parse_qsl, urlencode
    query = urlencode([(k, v) for k, v in parse_qsl(p.query)
                       if k.lower() in ('id', 'no', 'seq', 'type', 'lang', 'language', 'version')
                       and re.fullmatch(r'[\w.-]{1,60}', v)])
    # HTML links and redirect Locations may contain literal Korean path segments.
    # urllib sends an ASCII request target; preserve existing escapes and delimiters.
    path = quote(p.path or '/', safe="/%:@!$&'()*+,;=-._~")
    return urlunsplit((p.scheme, p.netloc, path, query, ''))


class Document(HTMLParser):
    """Extract visible text rows and links, never JavaScript or form values."""
    def __init__(self, html, include_navigation=False):
        self.ignored_tags = {'script', 'style', 'noscript', 'template'}
        if not include_navigation:
            self.ignored_tags.update({'nav', 'footer', 'header'})
        super().__init__(convert_charrefs=True)
        self.blocks, self.links = [], []
        self.parts, self.anchor = [], None
        self.hidden, self.heading, self.section, self.row = 0, False, '', False
        self.feed(html)
        self.flush()

    def flush(self):
        text = ' '.join(' '.join(self.parts).split())
        self.parts = []
        if text:
            if self.heading or (not self.row and len(text) < 80
                                and re.match(r'^(?:제\s*)?\d+[.조)\s]', text)
                                and any(re.search(p, text, re.I) for _, p in SECTION_PATTERNS)):
                self.section = text
            self.blocks.append((self.section, text))

    def handle_starttag(self, tag, attrs):
        if tag in self.ignored_tags:
            self.hidden += 1
        if self.hidden:
            return
        attrs = dict(attrs)
        if tag == 'tr':
            self.flush()
            self.row = True
        elif tag in ('p', 'div', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'br') and not self.row:
            self.flush()
        if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            self.heading = True
        if tag == 'a':
            self.anchor = [attrs.get('href') or '', []]

    def handle_endtag(self, tag):
        if tag in self.ignored_tags:
            self.hidden = max(0, self.hidden - 1)
            return
        if self.hidden:
            return
        if tag == 'a' and self.anchor:
            self.links.append((self.anchor[0], ' '.join(self.anchor[1])))
            self.anchor = None
        if tag == 'tr':
            self.flush()
            self.row = False
        elif tag in ('p', 'div', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6') and not self.row:
            self.flush()
        if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            self.heading = False
            # Empty headings must also end the preceding section context.
            if not self.blocks:
                self.section = ''

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)
            if self.anchor:
                self.anchor[1].append(data)


def extract(html, url):
    doc = Document(html)
    text = '\n'.join(t for _, t in doc.blocks)
    if not POLICY.search(text) or not re.search(r'수집|위탁|제공|보유|collect|process|retain', text, re.I):
        raise PolicyUnavailable('not_a_policy_document')
    if evidence.detect_block(200, '', text[:20000]):
        raise PolicyUnavailable('blocked')
    mentions, seen = [], set()
    for section, block in doc.blocks:
        block = re.sub(r'\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b', '[이메일 생략]', block)
        if len(block) > 10000:
            # Huge unstructured script-rendering fallbacks are not usable row evidence.
            continue
        for name, aliases in ORGANIZATIONS.items():
            match = next((m for a in aliases if (m := re.search(
                r'(?<![A-Za-z0-9])' + re.escape(a) + r'(?![A-Za-z0-9])', block, re.I))), None)
            if not match or (name, block) in seen or len(block.strip()) <= len(match.group()) + 3:
                continue
            seen.add((name, block))
            category = ('국외 이전' if re.search(r'국외|이전\s*국가', block) else
                        next((label for label, pattern in SECTION_PATTERNS
                              if re.search(pattern, section, re.I)), '기타 문서 언급'))
            excerpt = block[max(0, match.start()-160):match.end()+260]
            # Public contact details are not needed to establish vendor mentions.
            excerpt = re.sub(r'\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b', '[이메일 생략]', excerpt)
            excerpt = re.sub(r'(?<!\d)\d{2,4}[- ]\d{3,4}[- ]\d{4}(?!\d)', '[전화번호 생략]', excerpt)
            mentions.append({'organization': name, 'matched_text': match.group(),
                             'section': evidence.redact(section, 160), 'category': category,
                             'excerpt': evidence.redact(excerpt, 500), 'basis': 'document_mention',
                             'requires_confirmation': True})
            if len(mentions) >= 60:
                break
        if len(mentions) >= 60:
            break
    dates = re.findall(r'(?:시행|적용|개정|업데이트|effective|updated)[^\n\d]{0,25}(20\d{2}[./년 -]+\d{1,2}[./월 -]+\d{1,2}일?)', text, re.I)
    return {'url': clean_url(url), 'content_hash': hashlib.sha256(text.encode()).hexdigest(),
            'document_date_labels': list(dict.fromkeys(dates))[:5], 'mentions': mentions,
            'status': 'READ', 'interpretation': '문서 내 언급입니다. 현재 사용 여부·제품·계약 관계는 확인이 필요합니다.'}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Fetcher:
    def __init__(self, roots):
        self.hosts = {(urlsplit(u).hostname or '').removeprefix('www.') for u in roots}
        self.deadline = time.monotonic() + 30
        self.requests = 0
        self.robots = {}
        self.last = {}
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.should_defer = None

    def allowed(self, url):
        p = urlsplit(clean_url(url))
        if not any(p.hostname == h or p.hostname.endswith('.'+h) for h in self.hosts if h):
            raise PolicyUnavailable('external_domain_requires_review')
        security.check_scan_target(url)

    def fetch(self, url, *, robots=False):
        url = clean_url(url)
        for _ in range(4):
            if self.should_defer and self.should_defer():
                raise PolicyDeferred('primary_work_or_host_busy')
            self.allowed(url)
            p = urlsplit(url)
            origin = f'{p.scheme}://{p.netloc}'
            delay = 1
            if not robots:
                if origin not in self.robots:
                    _, code, _, body = self.fetch(origin+'/robots.txt', robots=True)
                    if code == 404:
                        body = ''
                    elif code != 200:
                        raise PolicyUnavailable('robots_unavailable')
                    if body.strip().startswith('<'):
                        raise PolicyUnavailable('robots_invalid')
                    parser = RobotFileParser()
                    parser.parse(body.splitlines())
                    self.robots[origin] = parser
                parser = self.robots[origin]
                if not parser.can_fetch('*', url):
                    raise PolicyUnavailable('robots_disallowed')
                delay = max(1, parser.crawl_delay('*') or 0)
            remaining = self.deadline-time.monotonic()
            wait = max(0, delay-(time.monotonic()-self.last.get(origin, 0)))
            if self.requests >= 8 or remaining <= wait+0.1:
                raise PolicyUnavailable('request_budget_exceeded')
            time.sleep(wait)
            if self.should_defer and self.should_defer():
                raise PolicyDeferred('primary_work_or_host_busy')
            remaining = self.deadline-time.monotonic()
            if remaining <= 0:
                raise PolicyUnavailable('request_budget_exceeded')
            self.requests += 1
            self.last[origin] = time.monotonic()
            request = Request(url, headers={'User-Agent': 'MarTechRadar/0.2 public-policy-reference',
                                           'Accept': 'text/html,text/plain;q=0.9', 'Accept-Encoding': 'identity'})
            try:
                response = self.opener.open(request, timeout=min(5, remaining))
            except HTTPError as exc:
                response = exc
            with response:
                code, headers = response.code, response.headers
                if code in (301, 302, 303, 307, 308):
                    url = clean_url(urljoin(url, headers.get('Location', '')))
                    continue  # check domain and robots BEFORE every redirect destination
                if code != 200:
                    return url, code, headers, ''
                if headers.get_content_type() not in ('text/html', 'application/xhtml+xml', 'text/plain'):
                    raise PolicyUnavailable('unsupported_document_format')
                chunks, size = [], 0
                while True:
                    if time.monotonic() >= self.deadline:
                        raise PolicyUnavailable('request_budget_exceeded')
                    chunk = response.read1(min(65536, MAX_BYTES+1-size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise PolicyUnavailable('document_too_large')
                raw = b''.join(chunks)
                charset = headers.get_content_charset()
                if not charset:
                    hint = re.search(br'charset\s*=\s*["\']?([\w-]+)', raw[:4096], re.I)
                    charset = hint.group(1).decode('ascii') if hint else 'utf-8'
                try:
                    text = raw.decode(charset, errors='replace')
                except LookupError:
                    text = raw.decode('utf-8', errors='replace')
                return url, code, headers, text
        raise PolicyUnavailable('redirect_limit')


def discover(html, base):
    candidates = []
    for href, label in Document(html, include_navigation=True).links:
        if not href.strip() or href.lstrip().startswith('#'):
            continue
        if POLICY.search(label+' '+href) and not HISTORICAL.search(label+' '+href):
            try:
                url = clean_url(urljoin(base, href))
            except (ValueError, PolicyUnavailable):
                continue
            if url not in candidates:
                candidates.append(url)
    return sorted(candidates, key=lambda u: ('security' in u, 'infographic' in u))[:MAX_DOCUMENTS]


def initialize(store):
    store.conn.execute('''CREATE TABLE IF NOT EXISTS policy_reference (
        target_id TEXT PRIMARY KEY REFERENCES target(id), payload_json TEXT NOT NULL)''')
    store.conn.execute('''CREATE INDEX IF NOT EXISTS policy_reference_checked
        ON policy_reference(json_extract(payload_json, '$.checked_at'))''')
    store.conn.commit()


def read(store, target_id):
    if not store.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='policy_reference'").fetchone():
        return None
    row = store.conn.execute('SELECT payload_json FROM policy_reference WHERE target_id=?', (target_id,)).fetchone()
    return json.loads(row[0]) if row else None


def record_error(store, target_id, error, policy_url=None):
    """Back off one broken target without calling a collector bug a site refusal."""
    target = store.target(target_id)
    source = {'roots': [evidence.split_mobile(u)[1] for u in json.loads(target['urls_json'])],
              'requested_policy_url': policy_url}
    previous = read(store, target_id)
    result = {'target_id': target_id, 'checked_at': datetime.now(UTC).isoformat(),
              'parser_version': PARSER_VERSION, 'source': source, 'evidence_type': 'policy_reference',
              'status': 'ERROR', 'documents': [], 'issues': ['collector_error'],
              'error_type': type(error).__name__}
    if (previous and previous.get('parser_version') == PARSER_VERSION
            and previous.get('source') == source):
        old = previous.get('documents') or previous.get('previous_readable', {}).get('documents', [])
        if old:
            checked = previous['checked_at'] if previous.get('documents') else previous['previous_readable']['checked_at']
            result['previous_readable'] = {'checked_at': checked, 'documents': old}
    initialize(store)
    store.conn.execute('INSERT INTO policy_reference VALUES (?,?) ON CONFLICT(target_id) DO UPDATE SET payload_json=excluded.payload_json',
                       (target_id, json.dumps(result, ensure_ascii=False)))
    store.conn.commit()
    return result


def collect(store, target_id, policy_url=None, *, should_defer=None):
    target = store.target(target_id)
    if not target or not target['enabled']:
        raise ValueError('An active watchlist target is required')
    if store.has_unfinished_jobs():
        return {'target_id': target_id, 'status': 'DEFERRED_COLLECTION_ACTIVE'}
    roots = [evidence.split_mobile(u)[1] for u in json.loads(target['urls_json'])]
    source = {'roots': roots, 'requested_policy_url': clean_url(policy_url) if policy_url else None}
    previous = read(store, target_id)
    now = datetime.now(UTC)
    if (previous and previous.get('parser_version') == PARSER_VERSION and previous.get('source') == source
            and now-datetime.fromisoformat(previous['checked_at']) < (
            TTL if previous['status'] == 'READ' else timedelta(days=1))):
        return {**previous, 'cached': True}
    fetcher = Fetcher(roots)
    fetcher.should_defer = should_defer
    result = {'target_id': target_id, 'checked_at': now.isoformat(), 'parser_version': PARSER_VERSION,
              'evidence_type': 'policy_reference', 'source': source, 'status': 'UNAVAILABLE', 'documents': [], 'issues': []}
    started = time.monotonic()
    try:
        if policy_url:
            candidates = [policy_url]
        else:
            landing, code, _, body = fetcher.fetch(roots[0])
            if code != 200:
                raise PolicyUnavailable('homepage_http_'+str(code))
            candidates = discover(body, landing)
        if not candidates:
            raise PolicyUnavailable('policy_link_not_found')
        for url in candidates:
            try:
                final, code, headers, body = fetcher.fetch(url)
                if code != 200:
                    raise PolicyUnavailable('policy_http_'+str(code))
                doc = extract(body, final)
                doc['http_last_modified'] = evidence.redact(headers.get('Last-Modified', ''), 100)
                result['documents'].append(doc)
            except (PolicyUnavailable, security.UnsafeTarget, URLError, TimeoutError, OSError) as exc:
                result['issues'].append(str(exc) if isinstance(exc, PolicyUnavailable) else type(exc).__name__)
        if result['documents']:
            result['status'] = 'PARTIAL' if result['issues'] else 'READ'
    except (PolicyUnavailable, security.UnsafeTarget, URLError, TimeoutError, OSError) as exc:
        result['issues'].append(str(exc) if isinstance(exc, PolicyUnavailable) else type(exc).__name__)
    result.update(request_count=fetcher.requests, duration_ms=round((time.monotonic()-started)*1000))
    # Failure never deletes the last readable reference or becomes evidence of absence.
    if (previous and previous.get('parser_version') == PARSER_VERSION
            and previous.get('source') == source and not result['documents']):
        old = previous.get('documents') or previous.get('previous_readable', {}).get('documents', [])
        if old:
            result['previous_readable'] = {'checked_at': previous.get('previous_readable', {}).get('checked_at', previous['checked_at']),
                                           'documents': old}
    initialize(store)
    store.conn.execute('INSERT INTO policy_reference VALUES (?,?) ON CONFLICT(target_id) DO UPDATE SET payload_json=excluded.payload_json',
                       (target_id, json.dumps(result, ensure_ascii=False)))
    store.conn.commit()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home')
    parser.add_argument('--only', required=True, action='append', help='Active target ID, maximum 20; repeatable')
    parser.add_argument('--url', help='Explicit first-party policy URL for one target')
    parser.add_argument('--show', action='store_true', help='Read saved references without fetching')
    args = parser.parse_args()
    if len(args.only) > 20 or (args.url and len(args.only) != 1):
        parser.error('At most 20 targets; --url requires exactly one target')
    workspace.set_home(args.home)
    with Store() as store:
        for target_id in dict.fromkeys(args.only):
            result = read(store, target_id) if args.show else collect(store, target_id, args.url)
            print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
