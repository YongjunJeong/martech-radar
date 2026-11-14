"""Policy mentions must never change browser observations or imply deployed products."""
from datetime import UTC, datetime, timedelta
from email.message import Message
import json
from urllib.request import Request

import pytest

from radar import policy_references as policies
from radar.store import Store


PAGE = '''<h1>개인정보 처리방침</h1><h2>처리 업무의 위탁</h2><table>
<tr><th>수탁자</th><th>업무</th></tr><tr><td>Braze, Inc.</td><td>메시지 전송</td></tr>
<tr><td>Google LLC</td><td>클라우드 저장</td></tr></table>
<h2>국외 이전</h2><p>Insider Pte. Ltd. 맞춤형 서비스 제공</p>
<h2>담당자</h2><p>braze@example.com 02-1234-5678</p>
<script>window.fake='Salesforce'</script><style>.Adobe{}</style>
<p>시행일: 2026.11.01</p>'''


def headers():
    m = Message()
    m['Content-Type'] = 'text/html; charset=utf-8'
    return m


def test_row_context_organizations_only_and_no_script_evidence():
    result = policies.extract(PAGE, 'https://example.com/privacy')
    assert result['status'] == 'READ'
    refs = result['mentions']
    assert {r['organization'] for r in refs} == {'Braze', 'Google', 'Insider'}
    braze = next(r for r in refs if r['organization'] == 'Braze')
    assert braze['category'] == '처리위탁' and '메시지 전송' in braze['excerpt']
    assert next(r for r in refs if r['organization'] == 'Insider')['category'] == '국외 이전'
    assert next(r for r in refs if r['organization'] == 'Google')['organization'] == 'Google'
    assert result['document_date_labels'] == ['2026.11.01']
    assert all(r['requires_confirmation'] and r['basis'] == 'document_mention' for r in refs)
    assert 'verdict' not in json.dumps(result)
    assert 'braze@example.com' not in json.dumps(result)
    assert '02-1234-5678' not in json.dumps(result)


def test_no_match_is_not_absence_or_noncompliance():
    result = policies.extract('<h1>개인정보 처리방침</h1><p>정보 수집 및 보유에 관한 안내</p>', 'https://example.com/p')
    assert result['mentions'] == []
    assert result['status'] == 'READ'
    assert 'NOT_DETECTED' not in json.dumps(result)


def test_links_select_policy_not_history_or_javascript_and_drop_tracking():
    links = policies.discover('''<a href="/privacy?utm_source=x&version=2">개인정보 처리방침</a>
        <a href="/privacy-old">이전 개인정보 처리방침</a>
        <a href="javascript:privacy()">개인정보 처리방침</a>
        <a href="/shop">스토어</a>''', 'https://example.com/')
    assert links == ['https://example.com/privacy?version=2']


@pytest.mark.parametrize('url', ['http://127.0.0.1/privacy','https://evil.test/privacy',
                                 'https://example.com.evil.test/privacy','https://example.com:22/p',
                                 'https://user:pass@example.com/p'])
def test_external_private_and_credential_urls_rejected(url):
    fetcher = policies.Fetcher(['https://example.com/'])
    with pytest.raises(ValueError):
        fetcher.allowed(url)


def test_redirects_are_never_followed_implicitly():
    redirect = policies.NoRedirect()
    assert redirect.redirect_request(Request('https://example.com'), None, 302, '', {},
                                     'http://127.0.0.1') is None


class FakeFetcher:
    def __init__(self, roots):
        self.requests = 0

    def fetch(self, url):
        self.requests += 1
        if url.endswith('/privacy'):
            return url, 200, headers(), PAGE
        return url, 200, headers(), '<a href="/privacy">개인정보 처리방침</a>'


def target(store):
    store.upsert_target(id='demo', company='Demo', industry='retail', urls=['https://example.com/'])


def test_persistence_cache_no_detector_rows_and_active_job_deferral(tmp_path, monkeypatch):
    monkeypatch.setattr(policies, 'Fetcher', FakeFetcher)
    with Store(tmp_path/'radar.db') as store:
        target(store)
        result = policies.collect(store, 'demo')
        assert result['status'] == 'READ' and result['request_count'] == 2
        for table in ('scan', 'detection', 'job', 'company_summary'):
            assert store.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
        monkeypatch.setattr(FakeFetcher, 'fetch', lambda *a: pytest.fail('cached document fetched'))
        assert policies.collect(store, 'demo')['cached']
        monkeypatch.setattr(store, 'has_unfinished_jobs', lambda: True)
        assert policies.collect(store, 'demo')['status'] == 'DEFERRED_COLLECTION_ACTIVE'


def test_failed_refresh_keeps_previous_evidence_but_marks_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(policies, 'Fetcher', FakeFetcher)
    with Store(tmp_path/'radar.db') as store:
        target(store)
        result = policies.collect(store, 'demo')
        result['checked_at'] = (datetime.now(UTC)-timedelta(days=8)).isoformat()
        store.conn.execute('UPDATE policy_reference SET payload_json=?', (json.dumps(result),))
        store.conn.commit()
        def blocked(*a):
            raise policies.PolicyUnavailable('robots_disallowed')
        monkeypatch.setattr(FakeFetcher, 'fetch', blocked)
        after = policies.collect(store, 'demo')
        assert after['status'] == 'UNAVAILABLE' and after['documents'] == []
        assert after['previous_readable']['documents'] == result['documents']
        assert after['previous_readable']['checked_at'] == result['checked_at']
        assert after['issues'] == ['robots_disallowed']


def test_social_links_footer_and_subprocessor_context_are_not_deployed_product_claims():
    page = '''<h1>개인정보 처리방침</h1><h2>국외 이전</h2><table>
      <tr><td>제공 업체</td><td>이전 목적(위탁 업무)</td></tr>
      <tr><td>Google LLC</td><td>이전 국가: 미국, 클라우드 저장</td></tr></table>
      <footer><p>Facebook</p><p>Salesforce 상담</p></footer><p>Facebook</p>'''
    result = policies.extract(page, 'https://example.com/privacy')
    assert len(result['mentions']) == 1
    assert result['mentions'][0]['organization'] == 'Google'
    assert result['mentions'][0]['category'] == '국외 이전'
    assert 'Google Analytics' not in json.dumps(result)


def test_contact_email_domain_is_not_vendor_evidence():
    result = policies.extract('<h1>개인정보 처리방침</h1><p>문의 및 수집 안내: help@braze.com</p>',
                              'https://example.com/privacy')
    assert result['mentions'] == []


class Response:
    def __init__(self, code=200, body=b'', location=None):
        self.code = code
        self.headers = headers()
        if location:
            self.headers['Location'] = location
        import io
        self.body = io.BytesIO(body)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read1(self, size):
        return self.body.read(size)


def transport(monkeypatch, responses):
    fetcher = policies.Fetcher(['https://example.com/'])
    visited = []
    monkeypatch.setattr(policies.security, 'check_scan_target', lambda url: url)
    monkeypatch.setattr(policies.time, 'sleep', lambda seconds: None)
    def open_(request, **kwargs):
        visited.append(request.full_url)
        return responses[request.full_url]
    monkeypatch.setattr(fetcher.opener, 'open', open_)
    return fetcher, visited


def test_robots_disallow_stops_before_policy_request(monkeypatch):
    fetcher, visited = transport(monkeypatch, {
        'https://example.com/robots.txt': Response(body=b'User-agent: *\nDisallow: /privacy')})
    with pytest.raises(policies.PolicyUnavailable, match='robots_disallowed'):
        fetcher.fetch('https://example.com/privacy')
    assert visited == ['https://example.com/robots.txt']


def test_redirect_destination_is_checked_before_network(monkeypatch):
    fetcher, visited = transport(monkeypatch, {
        'https://example.com/robots.txt': Response(body=b'User-agent: *\nAllow: /'),
        'https://example.com/privacy': Response(code=302, location='https://other.test/privacy')})
    with pytest.raises(policies.PolicyUnavailable, match='external_domain_requires_review'):
        fetcher.fetch('https://example.com/privacy')
    assert visited == ['https://example.com/robots.txt', 'https://example.com/privacy']


def test_korean_policy_links_and_redirects_use_ascii_request_targets(monkeypatch):
    encoded = 'https://example.com/article/%EA%B3%B5%EC%A7%80/1/'
    assert policies.discover('<a href="/article/공지/1/">개인정보 처리방침</a>',
                             'https://example.com/') == [encoded]
    assert policies.clean_url(encoded) == encoded  # no double encoding
    fetcher, visited = transport(monkeypatch, {
        'https://example.com/robots.txt': Response(body=b'User-agent: *\nAllow: /'),
        'https://example.com/privacy': Response(code=302, location='/article/공지/1/'),
        encoded: Response(body=PAGE.encode()),
    })
    final, code, _, body = fetcher.fetch('https://example.com/privacy')
    assert final == encoded and code == 200
    assert policies.extract(body, final)['status'] == 'READ'
    assert all(url.isascii() for url in visited)


def test_encoded_policy_path_still_obeys_robots(monkeypatch):
    fetcher, visited = transport(monkeypatch, {
        'https://example.com/robots.txt': Response(
            body='User-agent: *\nDisallow: /공지/'.encode()),
    })
    with pytest.raises(policies.PolicyUnavailable, match='robots_disallowed'):
        fetcher.fetch('https://example.com/공지/privacy')
    assert visited == ['https://example.com/robots.txt']


def test_byte_budget_and_unavailable_robots_are_not_assumed_readable(monkeypatch):
    fetcher, visited = transport(monkeypatch, {
        'https://example.com/robots.txt': Response(code=403)})
    with pytest.raises(policies.PolicyUnavailable, match='robots_unavailable'):
        fetcher.fetch('https://example.com/privacy')
    assert len(visited) == 1
    fetcher, _ = transport(monkeypatch, {
        'https://example.com/robots.txt': Response(body=b'User-agent: *\nAllow: /'),
        'https://example.com/privacy': Response(body=b'x'*(policies.MAX_BYTES+1))})
    with pytest.raises(policies.PolicyUnavailable, match='document_too_large'):
        fetcher.fetch('https://example.com/privacy')


def test_target_url_change_invalidates_cached_reference(tmp_path, monkeypatch):
    monkeypatch.setattr(policies, 'Fetcher', FakeFetcher)
    with Store(tmp_path/'radar.db') as store:
        target(store)
        policies.collect(store, 'demo')
        store.upsert_target(id='demo', company='Demo', industry='retail', urls=['https://new.example.com/'])
        result = policies.collect(store, 'demo')
        assert not result.get('cached')
        assert result['documents'][0]['url'] == 'https://new.example.com/privacy'


def test_policy_reference_is_in_existing_database_backup_and_restore(tmp_path, monkeypatch):
    from radar import recovery
    monkeypatch.setattr(policies, 'Fetcher', FakeFetcher)
    home = tmp_path/'home'
    home.mkdir()
    (home/'radar.toml').write_text('')
    with Store(home/'data/radar.db') as store:
        target(store)
        result = policies.collect(store, 'demo')
    snapshot = recovery.backup(home, tmp_path/'backups')
    recovery.verify(snapshot)
    restored = tmp_path/'restored'
    recovery.restore(snapshot, restored, 'hosting')
    with Store(restored/'data/radar.db') as store:
        assert policies.read(store, 'demo') == result
        assert store.conn.execute('SELECT count(*) FROM scan').fetchone()[0] == 0


def test_valueless_blank_and_fragment_links_do_not_crash_or_become_policy_pages():
    html = '''<a href>개인정보 처리방침</a><a href="">privacy</a>
        <a href="  ">privacy</a><a href="#privacy">privacy</a>
        <a>privacy</a><a href="/real-policy">개인정보 처리방침</a>'''
    assert policies.discover(html, 'https://example.com/') == ['https://example.com/real-policy']
