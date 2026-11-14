"""Solution filters share one scope across counts, advice, evidence and links."""
import copy

from fastapi.testclient import TestClient

import test_analysis_window as analysis
from web import sales
from web.app import create_app

scoped = analysis.scoped
scan = analysis.scan


def mixed(store):
    scan(store, '2026-10-15T03:00:00+09:00')
    scan(store, '2026-10-16T03:00:00+09:00', ['sdk.iad-03.braze.com', 'channel.io'])
    scan(store, '2026-10-17T03:00:00+09:00', ['sdk.iad-03.braze.com', 'channel.io', 'googleadservices.com'])


def test_selected_change_controls_contact_status_and_counts(scoped):
    mixed(scoped)
    assert sales.board(scoped, {})['view_counts']['contact'] == 1
    ads = sales.board(scoped, {}, solution_type='advertising')
    assert ads['status'] == 'watch'
    assert ads['view_counts'] == {'contact': 0, 'watch': 1, 'steady': 0, 'pending': 0}
    card = ads['cards'][0]
    assert [e['id'] for e in card['events']] == ['google_ads']
    assert card['decision']['allowed_count'] == 0
    assert not card['events'][0]['contact_allowed']
    assert '솔루션 1개' in card['waiting']['results'][0]
    engagement = sales.board(scoped, {}, solution_type='engagement')
    assert engagement['view_counts']['contact'] == 1
    assert [e['id'] for e in engagement['cards'][0]['events']] == ['braze']
    assert 'waiting' not in engagement['cards'][0]


def test_product_purpose_and_type_filter_agree(scoped):
    mixed(scoped)
    support = sales.board(scoped, {}, solution_type='customer_support')
    assert [e['id'] for e in support['cards'][0]['events']] == ['channel_talk']
    assert support['cards'][0]['events'][0]['use'] == support['solution_type_label'] == '고객 상담'
    platform = sales.board(scoped, {}, solution_type='platform')
    assert platform['total'] == 1 and platform['scoped_total'] == 0
    assert not platform['cards'] and not any(platform['view_counts'].values())


def test_filter_keeps_quality_override_and_search_intersection(scoped):
    mixed(scoped)
    assert sales.board(scoped, {}, solution_type='engagement', q='없는기업')['scoped_total'] == 0
    assert sales.board(scoped, {}, solution_type='engagement', industry='finance')['scoped_total'] == 0
    scan(scoped, '2026-10-18T03:00:00+09:00', status='TIMEOUT')
    board = sales.board(scoped, {}, solution_type='engagement', status='all')
    assert board['view_counts'] == {'contact': 0, 'watch': 0, 'steady': 0, 'pending': 1}
    assert not board['cards'][0]['events'][0]['contact_allowed']
    assert 'waiting' not in board['cards'][0]


def test_type_selects_changes_not_stable_installed_tools(scoped):
    scan(scoped, '2026-10-15T03:00:00+09:00', ['sdk.iad-03.braze.com'])
    scan(scoped, '2026-10-16T03:00:00+09:00', ['sdk.iad-03.braze.com', 'googleadservices.com'])
    assert sales.board(scoped, {}, solution_type='engagement', status='all')['scoped_total'] == 0
    assert sales.board(scoped, {}, solution_type='advertising', status='all')['scoped_total'] == 1


def test_routes_preserve_type_into_detail_and_reject_unknown_values(scoped):
    mixed(scoped)
    client = TestClient(create_app(str(scoped.path), language='ko'))
    for path in ('/', '/calls', '/companies'):
        text = client.get(path + '?solution_type=advertising&status=watch').text
        assert 'name="solution_type"' in text and 'value="advertising" checked' in text
        assert '/companies/acme?solution_type=advertising' in text
        assert '선택한 유형 기준' in text
        assert 'Braze' not in text and 'Channel Talk' not in text
        assert client.get(path + '?solution_type=unknown').status_code == 422
    result = client.get('/companies/acme?solution_type=advertising')
    assert result.status_code == 200
    overview = result.text.split('<details class="technical">')[0]
    assert '광고 운영 변화 요약' in overview and '아직 근거가 부족' in overview
    assert 'Braze' not in overview and 'Channel Talk' not in overview
    assert '전체 유형 보기' in overview and 'solution_type=advertising' in overview
    assert client.get('/companies/acme?solution_type=unknown').status_code == 422
    assert '전체 변화 요약' in client.get('/companies/acme').text


def test_pagination_and_counts_use_filtered_scope(scoped, monkeypatch):
    mixed(scoped)
    base = sales.company_cards(scoped, {}, solution_type='advertising')[0]
    cards = [dict(copy.deepcopy(base), id=f'co{i}', company=f'기업 {i}') for i in range(30)]
    monkeypatch.setattr(sales, 'board_candidates', lambda *a, **kw: cards)
    monkeypatch.setattr(sales, 'company_cards', lambda *a, **kw: [
        c for c in cards if c['id'] in kw['target_ids']])
    board = sales.board(scoped, {}, solution_type='advertising', status='watch', page=2)
    assert board['scoped_total'] == board['filtered'] == board['view_counts']['watch'] == 30
    assert len(board['cards']) == 6 and board['pages'] == 2
    text = TestClient(create_app(str(scoped.path), language='ko')).get('/?solution_type=advertising&status=watch&page=2').text
    assert 'solution_type=advertising' in text and 'page=1' in text
