"""Tag/type intersections share one scope from home totals through detail advice."""
import copy

import pytest
from fastapi.testclient import TestClient

import test_analysis_window as analysis
from web import sales
from web.app import create_app

scoped = analysis.scoped
scan = analysis.scan
BRAZE = 'sdk.iad-03.braze.com'
ADS = 'googleadservices.com'


def mixed(store):
    scan(store, '2026-10-15T03:00:00+09:00', [BRAZE])
    scan(store, '2026-10-16T03:00:00+09:00', [ADS])
    scan(store, '2026-10-17T03:00:00+09:00', [ADS])


def test_intersection_applies_to_same_solution_and_recalculates_status(scoped):
    mixed(scoped)
    assert sales.board(scoped, {})['view_counts']['contact'] == 1
    missing = sales.board(scoped, {}, observation='missing')
    assert missing['status'] == 'all'
    assert missing['current_view']['label'] == '조건에 맞는 기업'
    assert missing['scoped_total'] == missing['view_counts']['watch'] == 1
    card = missing['cards'][0]
    assert [e['id'] for e in card['events']] == ['braze']
    assert not card['decision']['allowed_count']
    assert len(card['waiting']['results']) == 1
    assert '미검출' in card['waiting']['results'][0]
    # This company has both a missing solution and an ad solution, but they
    # are different solutions, so the intersection must exclude it.
    assert sales.board(scoped, {}, observation='missing', solution_type='advertising')['scoped_total'] == 0
    appeared = sales.board(scoped, {}, observation='appeared', solution_type='advertising')
    assert appeared['view_counts']['contact'] == 1
    assert [e['id'] for e in appeared['cards'][0]['events']] == ['google_ads']
    assert sales.board(scoped, {}, observation='missing', q='없는기업')['scoped_total'] == 0
    assert sales.board(scoped, {}, observation='missing', industry='finance')['scoped_total'] == 0


@pytest.mark.parametrize('tag,days,bucket', [('baseline', 1, 'baseline'), ('stable', 2, 'steady')])
def test_non_change_tags_work_with_solution_type(scoped, tag, days, bucket):
    for day in range(15, 15 + days):
        scan(scoped, f'2026-10-{day}T03:00:00+09:00', [BRAZE])
    board = sales.board(scoped, {}, observation=tag, solution_type='engagement')
    assert board['status'] == 'all' and board['filtered'] == 1
    assert board['counts'][bucket] == 1
    assert not board['cards'][0]['events']
    assert board['cards'][0]['tools'][0]['pattern']['key'] == tag
    assert sales.board(scoped, {}, observation=tag, solution_type='advertising')['filtered'] == 0
    text = TestClient(create_app(str(scoped.path), language='ko')).get(
        f'/?observation={tag}&solution_type=engagement').text
    assert '테스트기업' in text and f'value="{tag}" checked' in text


def test_fluctuation_filter_retains_direction_and_counts_company_once(scoped):
    for day, hosts in [(15, [BRAZE, ADS]), (16, []), (17, [BRAZE, ADS]), (18, []), (19, [BRAZE])]:
        scan(scoped, f'2026-10-{day}T03:00:00+09:00', hosts)
    board = sales.board(scoped, {}, observation='fluctuating')
    assert board['scoped_total'] == 1
    assert {e['pattern']['key'] for e in board['cards'][0]['events']} == {'returned', 'missing'}
    returned = sales.board(scoped, {}, observation='returned')
    assert [e['id'] for e in returned['cards'][0]['events']] == ['braze']
    ads = sales.board(scoped, {}, observation='fluctuating', solution_type='advertising')
    assert [e['id'] for e in ads['cards'][0]['events']] == ['google_ads']


def test_failure_overrides_contact_and_is_not_a_missing_detection(scoped):
    mixed(scoped)
    scan(scoped, '2026-10-18T03:00:00+09:00', status='BLOCKED')
    board = sales.board(scoped, {}, observation='appeared', solution_type='advertising')
    assert board['view_counts'] == {'contact': 0, 'watch': 0, 'steady': 0, 'pending': 1}
    assert not board['cards'][0]['events'][0]['contact_allowed']
    assert sales.board(scoped, {}, observation='missing', solution_type='advertising')['scoped_total'] == 0


def test_routes_detail_links_and_validation(scoped):
    mixed(scoped)
    client = TestClient(create_app(str(scoped.path), language='ko'))
    query = 'solution_type=engagement&observation=missing'
    for path in ('/', '/companies', '/calls'):
        result = client.get(path + '?' + query + '&status=all')
        assert result.status_code == 200
        text = result.text
        assert 'name="observation"' in text and 'value="missing" checked' in text
        assert '/companies/acme?solution_type=engagement&amp;observation=missing' in text
        assert 'Google Ads' not in text
        assert 'name="status" value="all"' in text
        assert client.get(path + '?observation=unknown').status_code == 422
    detail = client.get('/companies/acme?' + query)
    assert detail.status_code == 200
    text = detail.text.split('<details class="technical">')[0]
    assert 'Google Ads' not in text
    assert '고객 메시지·개인화 · 검출 → 미검출' in text
    assert '/?status=watch&amp;solution_type=engagement&amp;observation=missing' in text
    assert '전체 솔루션 보기' in text and '아직 근거가 부족' in text
    assert client.get('/companies/acme?observation=unknown').status_code == 422
    assert '/companies/acme?observation=missing' in client.get('/?observation=missing').text
    with pytest.raises(ValueError, match='unknown observation'):
        sales.board(scoped, {}, observation='unknown')


def test_pagination_preserves_both_filters_and_status(scoped, monkeypatch):
    mixed(scoped)
    base = sales.company_cards(scoped, {}, observation='missing', solution_type='engagement')[0]
    cards = [dict(copy.deepcopy(base), id=f'co{i}', company=f'기업 {i}') for i in range(30)]
    monkeypatch.setattr(sales, 'board_candidates', lambda *a, **kw: cards)
    monkeypatch.setattr(sales, 'company_cards', lambda *a, **kw: [
        c for c in cards if c['id'] in kw['target_ids']])
    board = sales.board(scoped, {}, observation='missing', solution_type='engagement', status='watch', page=2)
    assert board['scoped_total'] == board['view_counts']['watch'] == board['filtered'] == 30
    assert len(board['cards']) == 6
    text = TestClient(create_app(str(scoped.path), language='ko')).get(
        '/?observation=missing&solution_type=engagement&status=watch&page=2').text
    pagination = text.split('<nav class="pagination"')[1]
    assert 'observation=missing' in pagination and 'solution_type=engagement' in pagination
    assert 'status=watch' in pagination and 'page=1' in pagination
