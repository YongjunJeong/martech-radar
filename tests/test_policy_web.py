from datetime import UTC, datetime, timedelta
import json

from fastapi.testclient import TestClient
import pytest

from radar import policy_references as policies
from radar.store import Store
from web.app import create_app
from web.policy_references import view


def fixture(tmp_path):
    db = tmp_path/'radar.db'
    with Store(db) as store:
        store.upsert_industry('retail','유통')
        store.upsert_target(id='demo', company='테스트 기업', industry='retail', urls=['https://example.com/'])
        policies.initialize(store)
    return db


def save(db, *, status='READ', documents=None, previous=None):
    now = datetime.now(UTC)
    doc = {'url': 'https://example.com/privacy', 'document_date_labels': ['2025.01.01'],
           'mentions': [{'organization':'Google', 'category':'처리위탁',
                         'excerpt':'Google LLC 클라우드 저장 <script>alert(1)</script>'}]}
    value = {'source': {'roots':['https://example.com/']}, 'parser_version': policies.PARSER_VERSION,
             'checked_at':now.isoformat(), 'status':status, 'documents':[doc] if documents is None else documents,
             'issues':['robots_disallowed'] if status!='READ' else []}
    if previous:
        value['previous_readable'] = {'documents':[doc], 'checked_at':(now-timedelta(days=8)).isoformat()}
    with Store(db) as store:
        store.conn.execute('INSERT INTO policy_reference VALUES (?,?)', ('demo',json.dumps(value)))
        store.conn.commit()
    return value


def test_detail_reference_is_separate_escaped_and_no_fetch_on_view(tmp_path, monkeypatch):
    db = fixture(tmp_path)
    save(db)
    monkeypatch.setattr(policies, 'collect', lambda *a, **kw: pytest.fail('view triggered collection'))
    client = TestClient(create_app(str(db), language='ko'))
    response = client.get('/companies/demo')
    assert response.status_code == 200
    text = response.text
    assert text.count('id="policy-reference-title"') == 1
    assert '문서 내 언급' in text and '연락 우선순위에 반영하지 않습니다' in text
    assert 'Google LLC' in text and 'Google Analytics' not in text
    assert '<script>alert(1)</script>' not in text and '&lt;script&gt;' in text
    assert 'https://example.com/privacy' in text and 'noopener noreferrer' in text
    assert '2025.01.01' in text and '도입일을 의미하지 않습니다' in text
    with Store(db) as store:
        assert store.conn.execute('SELECT count(*) FROM scan').fetchone()[0] == 0
        assert store.conn.execute('SELECT count(*) FROM detection').fetchone()[0] == 0
    # References are company detail context, not another company-list signal/filter.
    assert 'policy-reference-title' not in client.get('/').text


def test_failure_keeps_old_date_separate_and_empty_is_not_absence(tmp_path):
    db = fixture(tmp_path)
    saved = save(db,status='UNAVAILABLE',documents=[],previous=True)
    with Store(db) as store:
        result = view(store, store.target('demo'))
    assert result['previous'] and result['checked_at'] != result['attempted_at']
    assert '이전 확인 자료' in result['headline']
    assert result['documents'] == [{**saved['previous_readable']['documents'][0], 'host':'example.com'}]
    client = TestClient(create_app(str(db),language='ko'))
    assert '현재 문서에도 같은 내용이 있는지는 확인하지 못했습니다' in client.get('/companies/demo').text


def test_changed_target_and_unsafe_link_do_not_surface_stale_evidence(tmp_path):
    db=fixture(tmp_path)
    save(db,documents=[{'url':'javascript:alert(1)', 'mentions':[]}])
    with Store(db) as store:
        assert view(store,store.target('demo'))['documents'] == []
        store.upsert_target(id='demo',company='Changed',industry='retail',urls=['https://different.test/'])
        assert view(store,store.target('demo'))['state']=='pending'


def test_uncollected_and_empty_document_states_render(tmp_path):
    db=fixture(tmp_path)
    client=TestClient(create_app(str(db),language='ko'))
    assert '아직 참고 문서를 확인하지 않았습니다' in client.get('/companies/demo').text
    save(db,documents=[{'url':'https://example.com/privacy','mentions':[],'document_date_labels':[]}])
    response=client.get('/companies/demo')
    assert '등록된 업체명은 추출되지 않았습니다' in response.text
    assert '솔루션 미사용을 뜻하지 않습니다' in response.text
