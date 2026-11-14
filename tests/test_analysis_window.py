from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from radar import config, detector, registry, signals
from radar.store import Store
from radar.targets import Target, TargetSet
from web import sales
from web.app import create_app


@pytest.fixture
def scoped(monkeypatch, tmp_path):
    settings = config.Config(analysis=config.AnalysisSettings(since='2026-10-15'),
                             ui=config.UISettings(language='ko'))
    monkeypatch.setattr(config, 'load', lambda *a, **k: settings)
    with Store(tmp_path/'radar.db') as s:
        s.import_watchlist(TargetSet((Target('acme','테스트기업','retail',('https://acme.test/',)),),
                                    {'retail':'쇼핑'},'test'))
        yield s


def scan(s, when, hosts=(), status='OK', page_host='acme.test'):
    run = s.start_run(1)
    result = {'scan': {'url':'https://acme.test/', 'page_host':page_host,
                      'status':status, 'started_at':when},
              'evidence': {'network': {'hosts':list(hosts)}}, 'counts':{},'warnings':[]}
    sid = s.record_scan(run,'acme',result)
    s.record_detections(sid,detector.detect(result,registry.load()))
    return sid


def test_kst_boundary_excludes_old_stack_shift_and_change(scoped):
    scan(scoped,'2026-10-14T14:59:59+00:00',['sdk.iad-03.braze.com'],page_host='old.test')
    scan(scoped,'2026-10-15T00:00:00+09:00',['cdn.moengage.com'])
    observations,_ = signals.timeline(scoped,'acme')
    assert len(observations)==1
    assert len(signals.timelines(scoped)['acme'][0])==1
    changes = signals.changes_for(scoped,'acme')
    assert changes and all(c.state==signals.STATE_BASELINE for c in changes)
    assert signals.all_signals(scoped)==[]
    assert 'Braze' not in str(scoped.stack_of('acme'))
    assert scoped.coverage('acme')['acme']['scans']==1
    assert scoped.conn.execute('select count(*) from scan').fetchone()[0]==2
    card=sales.company_cards(scoped, {})[0]
    assert card['bucket']=='baseline' and card['good_count']==1


def test_old_success_does_not_make_current_failure_usable(scoped):
    scan(scoped,'2026-09-01T00:00:00+00:00',['sdk.iad-03.braze.com'])
    scan(scoped,'2026-10-16T00:00:00+00:00',status='BLOCKED')
    assert not signals.summaries(scoped)['acme'].judged
    assert sales.company_cards(scoped,{})[0]['bucket']=='check'
    from radar.brief import company_brief
    _,usable=company_brief(scoped,'acme')
    assert not usable


def test_cutoff_change_invalidates_summaries_without_new_scan(scoped,monkeypatch):
    scan(scoped,'2026-09-01T00:00:00+00:00',['sdk.iad-03.braze.com'])
    configured=config.load()
    monkeypatch.setattr(config,'load',lambda *a,**k:replace(configured,analysis=config.AnalysisSettings()))
    assert signals.summaries(scoped)['acme'].judged
    monkeypatch.setattr(config,'load',lambda *a,**k:configured)
    assert 'acme' not in signals.summaries(scoped)
    assert sales.company_cards(scoped,{})[0]['bucket']=='waiting'


def test_regrading_and_redetection_refresh_sales_facts(scoped):
    scan(scoped, '2026-10-15T03:00:00+09:00', ['sdk.iad-03.braze.com'])
    latest = scan(scoped, '2026-10-16T03:00:00+09:00')
    card = sales.company_cards(scoped, {})[0]
    assert card['events'][0]['missing_count'] == 1
    # A corrected scan is not a new visit, but must change cached counts.
    scoped.conn.execute("UPDATE scan SET status='PARTIAL' WHERE id=?", (latest,))
    scoped.conn.commit()
    card = sales.company_cards(scoped, {})[0]
    assert card['good_count'] == 1 and card['events'] == []
    assert card['bucket'] == 'check'
    scoped.conn.execute("UPDATE scan SET status='OK' WHERE id=?", (latest,))
    scoped.conn.commit()
    rejudged = detector.detect({
        'scan': {'status': 'OK', 'url': 'https://acme.test/', 'page_host': 'acme.test'},
        'evidence': {'network': {'hosts': ['sdk.iad-03.braze.com']}},
    }, registry.load())
    scoped.record_detections(latest, rejudged)
    card = sales.company_cards(scoped, {})[0]
    assert card['good_count'] == 2 and card['events'] == []
    assert card['bucket'] == 'steady'
    assert card['tools'][0]['last_seen'] == '2026.10.16'


def test_repeated_absence_and_failed_latest_change_contact_advice(scoped):
    scan(scoped,'2026-10-15T00:00:00+00:00',['sdk.iad-03.braze.com'])
    scan(scoped,'2026-10-16T00:00:00+00:00')
    assert sales.company_cards(scoped,{})[0]['bucket']=='watch'
    scan(scoped,'2026-10-23T00:00:00+00:00')
    card=sales.company_cards(scoped,{})[0]
    assert card['bucket']=='contact'
    assert card['events'][0]['before']=='확인됨 · 2026.10.15'
    assert card['events'][0]['after']=='확인되지 않음'
    assert '확정하지' in card['events'][0]['confidence']
    scan(scoped,'2026-10-24T00:00:00+00:00',status='BLOCKED')
    assert sales.company_cards(scoped,{})[0]['bucket']=='check'
    client=TestClient(create_app(str(scoped.path), language='ko'))
    assert '연락할 때 확인할 질문' not in client.get('/companies/acme').text


def test_board_search_and_baseline_do_not_suggest_new_adoption(scoped):
    scan(scoped,'2026-10-15T00:00:00+00:00',['sdk.iad-03.braze.com'])
    client=TestClient(create_app(str(scoped.path),language='ko'))
    home=client.get('/').text
    assert '현재 재확인을 기다리는 기업이 없습니다.' in home
    company=client.get('/companies/acme').text
    assert '2026-10-15' in company
    assert '첫 수집에서 확인' in company and '고객 메시지·개인화' in company
    assert '연락할 때 확인할 질문' not in company
    assert '테스트기업' in client.get('/companies?q=테스트').text
    assert '테스트기업' not in client.get('/companies?q=없는기업').text
    assert client.get('/companies?page=-1').status_code==422


def test_repeated_new_detection_needs_absent_baseline(scoped):
    scan(scoped,'2026-10-15T00:00:00+00:00')
    scan(scoped,'2026-10-16T00:00:00+00:00',['sdk.iad-03.braze.com'])
    assert sales.company_cards(scoped,{})[0]['bucket']=='watch'
    scan(scoped,'2026-10-17T00:00:00+00:00',['sdk.iad-03.braze.com'])
    card=sales.company_cards(scoped,{})[0]
    assert card['bucket']=='contact'
    assert '실제 도입일' in card['events'][0]['confidence']


def test_reappearance_explains_full_sequence_and_latest_direct_evidence(scoped):
    scan(scoped, '2026-10-15T00:00:00+00:00', ['sdk.iad-03.braze.com'])
    scan(scoped, '2026-10-19T00:00:00+00:00')
    sid = scan(scoped, '2026-10-20T00:00:00+00:00', ['sdk.iad-03.braze.com'])
    card = sales.company_cards(scoped, {}, 'acme')[0]
    event = card['events'][0]
    assert card['bucket'] == 'watch'
    assert event['label'] == '미검출 후 다시 검출됨'
    assert '재도입을 뜻하지 않습니다' in event['confidence']
    assert [p['result'] for p in event['history']] == ['검출', '미검출', '검출']
    assert event['previous_date'] == '2026.10.19'
    assert event['latest'] == '2026.10.20'
    assert (event['detected_count'], event['missing_count'], event['streak']) == (2, 1, 1)
    assert event['evidence'][0]['scan_id'] == sid
    assert event['evidence'][0]['value'] == 'sdk.iad-03.braze.com'
    client = TestClient(create_app(str(scoped.path), language='ko'))
    for path in ('/', '/companies/acme'):
        text = client.get(path).text
        assert 'pattern-returned' in text and '다시 검출' in text
        assert '검출 2회 · 미검출 1회' in text
        assert ('sdk.iad-03.braze.com' in text) == (path == '/companies/acme')
        assert '2026.10.19' in text and '2026.10.20' in text


def test_missing_evidence_is_prior_detection_not_recent_failure(scoped):
    sid = scan(scoped, '2026-10-15T00:00:00+00:00', ['sdk.iad-03.braze.com'])
    scan(scoped, '2026-10-19T00:00:00+00:00')
    scan(scoped, '2026-10-20T00:00:00+00:00', status='BLOCKED')
    scan(scoped, '2026-10-21T00:00:00+00:00')
    card = sales.company_cards(scoped, {}, 'acme')[0]
    e = card['events'][0]
    assert (e['observations'], e['detected_count'], e['missing_count']) == (3, 1, 2)
    assert (e['previous_date'], e['since'], e['latest']) == ('2026.10.15', '2026.10.19', '2026.10.21')
    assert e['evidence'][0]['scan_id'] == sid
    assert e['evidence'][0]['date'] == '2026.10.15'
    text = TestClient(create_app(str(scoped.path), language='ko')).get('/companies/acme').text
    assert '이전에 검출됐을 때의 근거' in text
    assert '마지막으로 검출된 근거 · 2026.10.15' in text


def test_evidence_excludes_old_cutoff_and_indirect_matches(scoped):
    import json
    scan(scoped, '2026-09-15T00:00:00+00:00', ['sdk.iad-03.braze.com'])
    scan(scoped, '2026-10-15T00:00:00+00:00')
    sid = scan(scoped, '2026-10-19T00:00:00+00:00', ['sdk.iad-03.braze.com'])
    scoped.conn.execute('UPDATE detection SET matches_json=? WHERE scan_id=?',
                        (json.dumps([{'layer': 'source', 'value': 'indirect-only.example'},
                                     {'layer': 'network', 'value': '<script>alert(1)</script>'}]), sid))
    scoped.conn.commit()
    card = sales.company_cards(scoped, {}, 'acme')[0]
    e = card['events'][0]
    assert e['state'] == signals.STATE_NEW
    assert e['observations'] == 2
    assert len(e['evidence']) == 1 and e['evidence'][0]['scan_id'] == sid
    text = TestClient(create_app(str(scoped.path), language='ko')).get('/companies/acme').text
    # The separate raw-history panel intentionally preserves indirect matches;
    # sales evidence must contain only direct matches, escaped throughout.
    sales_text = text.split('<details class="technical">')[0]
    assert 'indirect-only.example' not in sales_text
    assert '<script>alert(1)</script>' not in text
    assert '&lt;script&gt;' in sales_text


def test_evidence_fetch_batched_and_works_without_cutoff(scoped, monkeypatch):
    scan(scoped, '2026-10-15T00:00:00+00:00')
    scan(scoped, '2026-10-19T00:00:00+00:00', ['sdk.iad-03.braze.com', 'cdn.moengage.com'])
    settings = replace(config.load(), analysis=config.AnalysisSettings())
    monkeypatch.setattr(config, 'load', lambda *a, **k: settings)
    queries = []
    scoped.conn.set_trace_callback(queries.append)
    board = sales.board(scoped, {})
    scoped.conn.set_trace_callback(None)
    assert not any('WITH wanted(' in q for q in queries)
    assert not any(e['evidence'] for e in board['cards'][0]['events'])
    queries.clear()
    scoped.conn.set_trace_callback(queries.append)
    cards = sales.company_cards(scoped, {}, 'acme')
    scoped.conn.set_trace_callback(None)
    assert len([q for q in queries if 'WITH wanted(' in q]) == 1
    events = cards[0]['events']
    assert len(events) == 2 and all(e['evidence'] for e in events)


def test_sales_view_counts_membership_and_search_use_same_scope(scoped, monkeypatch):
    scan(scoped, '2026-10-15T00:00:00+00:00')
    base = sales.company_cards(scoped, {})[0]
    cards = [dict(base, id=key, company=f'기업 {key}', bucket=key, label=label)
             for key, label in sales.BUCKETS.items()]
    monkeypatch.setattr(sales, 'board_candidates', lambda *a, **kw: cards)
    monkeypatch.setattr(sales, 'company_cards', lambda *a, **kw: [
        c for c in cards if c['id'] in kw['target_ids']])
    result = sales.board(scoped, {})
    assert result['status'] == 'contact'
    assert result['view_counts'] == {'contact': 1, 'watch': 1, 'steady': 1, 'pending': 3}
    assert sum(result['view_counts'].values()) == result['scoped_total'] == 6
    for status, view in sales.VIEWS.items():
        result = sales.board(scoped, {}, status=status)
        assert result['filtered'] == result['view_counts'][status]
        assert {c['bucket'] for c in result['cards']} == set(view['buckets'])
        assert result['current_view']['label'] == view['label']
    filtered = sales.board(scoped, {}, q='watch')
    assert filtered['status'] == 'watch'
    assert filtered['view_counts']['contact'] == 0
    assert filtered['filtered'] == filtered['scoped_total'] == 1
    empty = sales.board(scoped, {}, status='contact', q='없는기업')
    assert empty['filtered'] == 0 and not any(empty['view_counts'].values())
    for legacy in ('baseline', 'waiting', 'check'):
        assert sales.board(scoped, {}, status=legacy)['status'] == 'pending'


def test_home_selects_watch_and_contact_empty_explains_where_to_go(scoped):
    scan(scoped, '2026-10-15T00:00:00+00:00')
    scan(scoped, '2026-10-19T00:00:00+00:00', ['sdk.iad-03.braze.com'])
    client = TestClient(create_app(str(scoped.path), language='ko'))
    text = client.get('/').text
    assert '재확인 대기 <span>1곳</span>' in text
    assert 'watch selected" aria-current="true"' in text
    assert '변화가 있는 기업' not in text
    assert '아래 기업은 재확인을 기다리는 단계' in text
    empty = client.get('/calls').text
    assert '연락 전 확인 <span>0곳</span>' in empty
    assert '재확인 대기 1곳 보기' in empty
    assert 'contact selected" aria-current="true"' in empty
    text = client.get('/?q=테스트&industry=retail&page=3').text
    assert 'name="status" value="all"' in text
    assert 'q=' in text and 'industry=retail' in text
    assert 'page=1' in text


def test_detail_allows_only_repeated_tools_in_mixed_company(scoped):
    scan(scoped, '2026-10-15T00:00:00+00:00', ['sdk.iad-03.braze.com'])
    scan(scoped, '2026-10-19T00:00:00+00:00', ['cdn.moengage.com'])
    scan(scoped, '2026-10-20T00:00:00+00:00', ['cdn.moengage.com'])
    card = sales.company_cards(scoped, {}, 'acme')[0]
    events = {e['id']: e for e in card['events']}
    assert card['bucket'] == 'contact' and card['decision']['allowed_count'] == 1
    assert events['moengage']['contact_allowed']
    assert not events['braze']['contact_allowed']
    assert '7일 이상' in events['braze']['review_reason']
    client = TestClient(create_app(str(scoped.path), language='ko'))
    overview = client.get('/').text
    assert '변화가 관측된 솔루션 2개' in overview
    assert '반복 관측 기준 충족 1개 · 재확인 필요 1개' in overview
    assert '미검출 → 검출</span> <b>1개</b>' in overview
    assert '검출 → 미검출</span> <b>1개</b>' in overview
    text = client.get('/companies/acme').text
    assert '관측된 변화를 질문하며 연락을 검토할 수 있습니다.' in text
    assert '변화가 관측된 솔루션 2개' in text
    assert '지금 확인 질문에 활용할 수 있는 솔루션 <strong>1개</strong>' in text
    assert events['moengage']['question'] in text
    assert events['braze']['question'] not in text
    assert text.count('연락할 때 확인할 질문') == 1
    scan(scoped, '2026-10-21T00:00:00+00:00', status='BLOCKED')
    card = sales.company_cards(scoped, {}, 'acme')[0]
    assert card['decision']['ready_count'] == 1 and card['decision']['allowed_count'] == 0
    assert all(not e['contact_allowed'] for e in card['events'])
    overview = client.get('/?status=pending').text
    assert '마지막 정상 수집까지의 변화 2개' in overview
    assert '이전 기록의 집계입니다.' in overview
    text = client.get('/companies/acme').text
    assert '연락 근거로 사용 보류' in text and '마지막 정상 수집까지의 기록' in text
    assert '지금 확인 질문에 활용할 수 있는 솔루션 <strong>0개</strong>' in text
    assert '연락할 때 확인할 질문' not in text


def test_detail_reappearance_uses_recent_streak_not_total_detections(scoped):
    scan(scoped, '2026-10-15T00:00:00+00:00', ['sdk.iad-03.braze.com'])
    scan(scoped, '2026-10-19T00:00:00+00:00')
    scan(scoped, '2026-10-20T00:00:00+00:00', ['sdk.iad-03.braze.com'])
    card = sales.company_cards(scoped, {}, 'acme')[0]
    assert card['events'][0]['detected_count'] == 2
    assert '최근 연속 검출은 1회' in card['events'][0]['review_reason']
    assert not card['events'][0]['contact_allowed']
    text = TestClient(create_app(str(scoped.path), language='ko')).get('/companies/acme').text
    assert '이 변화로 연락하기에는 아직 근거가 부족합니다.' in text
    assert '연락할 때 확인할 질문' not in text


def test_detail_absence_requires_duration_and_disabled_target_overrides(scoped):
    scan(scoped, '2026-10-15T00:00:00+00:00', ['sdk.iad-03.braze.com'])
    scan(scoped, '2026-10-16T00:00:00+00:00')
    scan(scoped, '2026-10-17T00:00:00+00:00')
    card = sales.company_cards(scoped, {}, 'acme')[0]
    assert not card['events'][0]['contact_allowed']
    scan(scoped, '2026-10-23T00:00:00+00:00')
    card = sales.company_cards(scoped, {}, 'acme')[0]
    assert card['events'][0]['contact_allowed']
    scoped.set_target_enabled('acme', False)
    card = sales.company_cards(scoped, {}, 'acme')[0]
    assert card['decision']['allowed_count'] == 0
    assert '모니터링이 중지' in card['events'][0]['review_reason']


@pytest.mark.parametrize('case,expected', [
    ('waiting', '첫 수집 결과를 기다려 주세요.'),
    ('baseline', '비교할 기록이 더 쌓일 때까지 기다려 주세요.'),
    ('steady', '변화를 이유로 새로 연락할 근거는 없습니다.'),
    ('check', '수집 상태를 확인하기 전에는 연락 근거로 쓰지 마세요.'),
])
def test_detail_explains_each_no_change_state(scoped, case, expected):
    if case in ('baseline', 'steady'):
        scan(scoped, '2026-10-15T00:00:00+00:00', ['sdk.iad-03.braze.com'])
    if case == 'steady':
        scan(scoped, '2026-10-19T00:00:00+00:00', ['sdk.iad-03.braze.com'])
    if case == 'check':
        scan(scoped, '2026-10-19T00:00:00+00:00', status='BLOCKED')
    card = sales.company_cards(scoped, {}, 'acme')[0]
    assert card['bucket'] == case
    assert card['decision']['action'] == expected
    text = TestClient(create_app(str(scoped.path), language='ko')).get('/companies/acme').text
    assert expected in text
    assert '연락할 때 확인할 질문' not in text
    if case in ('waiting', 'check'):
        assert '비교에 사용할 정상 수집 기록이 없습니다.' in text
