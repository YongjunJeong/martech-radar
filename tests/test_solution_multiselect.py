"""OR within each filter group, AND between groups on the same solution."""
import copy
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

import test_analysis_window as analysis
from test_observation_multiselect import links
from web import sales
from web.app import create_app

scoped = analysis.scoped
scan = analysis.scan
BRAZE = 'sdk.iad-03.braze.com'
ADS = 'googleadservices.com'
CHAT = 'channel.io'


def mixed(store):
    for day, hosts in [(15, [BRAZE]), (16, []), (17, [BRAZE, ADS, CHAT]), (18, [BRAZE, ADS, CHAT])]:
        scan(store, f'2026-10-{day}T03:00:00+09:00', hosts)


def test_two_type_union_and_two_tag_union_are_intersected(scoped):
    mixed(scoped)
    board = sales.board(scoped, {}, solution_type=['advertising', 'engagement'],
                        observation=['appeared', 'returned'])
    assert board['scoped_total'] == board['view_counts']['contact'] == 1
    card = board['cards'][0]
    assert {e['id'] for e in card['events']} == {'google_ads', 'braze'}
    assert card['decision']['allowed_count'] == 2
    assert board['solution_type_label'] == '광고 운영 또는 고객 메시지·개인화'
    ads = sales.board(scoped, {}, solution_type=['advertising', 'customer_support'], observation=['returned', 'missing'])
    assert ads['scoped_total'] == 0  # Reappearance is on a different solution.
    scan(scoped, '2026-10-19T03:00:00+09:00', status='BLOCKED')
    board = sales.board(scoped, {}, solution_type=['advertising', 'engagement'], observation=['appeared', 'returned'])
    assert board['counts']['check'] == 1 and board['cards'][0]['decision']['allowed_count'] == 0


def test_eligibility_and_waiting_use_only_matched_types(scoped):
    scan(scoped, '2026-10-15T03:00:00+09:00')
    scan(scoped, '2026-10-16T03:00:00+09:00', [CHAT])
    scan(scoped, '2026-10-17T03:00:00+09:00', [CHAT, ADS, BRAZE])
    assert sales.board(scoped, {})['view_counts']['contact'] == 1
    board = sales.board(scoped, {}, solution_type=['advertising', 'engagement'])
    assert board['view_counts']['watch'] == 1 and board['view_counts']['contact'] == 0
    card = board['cards'][0]
    assert card['decision']['allowed_count'] == 0 and len(card['events']) == 2
    assert '솔루션 2개' in card['waiting']['results'][0]


def test_duplicates_empty_values_and_no_tag_restriction(scoped):
    mixed(scoped)
    board = sales.board(scoped, {}, solution_type=['engagement', 'advertising', 'advertising', ''],
                        observation=['returned', 'appeared', 'returned'])
    assert board['solution_type'] == ('advertising', 'engagement')
    assert len(board['cards'][0]['events']) == 2
    assert parse_qs(board['cards'][0]['filter_query']) == {
        'solution_type': ['advertising', 'engagement'], 'observation': ['appeared', 'returned']}
    for value in ('', [], [''], None):
        full = sales.board(scoped, {}, solution_type=value, observation=['appeared', 'returned'])
        assert len(full['cards'][0]['events']) == 3
    assert len(sales.board(scoped, {}, solution_type=['advertising', 'engagement'])['cards'][0]['events']) == 2
    with pytest.raises(ValueError, match='unknown solution type'):
        sales.board(scoped, {}, solution_type=['advertising', 'invalid'])


def test_multiple_types_with_stable_tag_and_product_override(scoped):
    for day in (15, 16):
        scan(scoped, f'2026-10-{day}T03:00:00+09:00', [BRAZE, ADS, CHAT])
    board = sales.board(scoped, {}, solution_type=['engagement', 'advertising'], observation=['stable', 'returned'])
    assert board['view_counts']['steady'] == 1
    assert board['cards'][0]['tool_count'] == 2 and not board['cards'][0]['events']
    assert not sales.board(scoped, {}, solution_type=['platform', 'error_monitoring'], observation=['stable'])['cards']
    support = sales.board(scoped, {}, solution_type=['customer_support', 'platform'], observation=['stable'])
    assert [t['id'] for t in support['cards'][0]['tools']] == ['channel_talk']


def test_both_lists_survive_routes_detail_back_and_validation(scoped):
    mixed(scoped)
    client = TestClient(create_app(str(scoped.path), language='ko'))
    query = 'solution_type=advertising&solution_type=engagement&observation=appeared&observation=returned'
    for route in ('/', '/companies', '/calls'):
        response = client.get(route + '?' + query)
        assert response.status_code == 200
        text = response.text
        for value in ('advertising', 'engagement', 'appeared', 'returned'):
            assert f'value="{value}" checked' in text
        for url in (u for u in links(text) if '/companies/acme?' in u or 'status=watch' in u):
            args = parse_qs(urlsplit(url).query)
            assert args['solution_type'] == ['advertising', 'engagement']
            assert args['observation'] == ['appeared', 'returned']
        assert client.get(route + '?' + query + '&solution_type=invalid').status_code == 422
    detail = client.get('/companies/acme?' + query)
    assert detail.status_code == 200
    text = detail.text.split('<details class="technical">')[0]
    assert '광고 운영 또는 고객 메시지·개인화 · 미검출 → 검출 또는 다시 검출' in text
    assert 'Channel Talk' not in text
    back = next(u for u in links(text) if u.startswith('/?status='))
    args = parse_qs(urlsplit(back).query)
    assert args['solution_type'] == ['advertising', 'engagement']
    assert args['observation'] == ['appeared', 'returned']
    assert client.get('/companies/acme?' + query + '&solution_type=invalid').status_code == 422


def test_pagination_preserves_both_lists(scoped, monkeypatch):
    mixed(scoped)
    base = sales.company_cards(scoped, {}, solution_type=['advertising', 'engagement'], observation=['appeared', 'returned'])[0]
    cards = [dict(copy.deepcopy(base), id=f'co{i}', company=f'기업 {i}') for i in range(30)]
    monkeypatch.setattr(sales, 'board_candidates', lambda *a, **kw: cards)
    monkeypatch.setattr(sales, 'company_cards', lambda *a, **kw: [
        c for c in cards if c['id'] in kw['target_ids']])
    query = 'solution_type=advertising&solution_type=engagement&observation=appeared&observation=returned&status=contact&page=2'
    text = TestClient(create_app(str(scoped.path), language='ko')).get('/?' + query).text
    args = parse_qs(urlsplit(links(text.split('<nav class="pagination"')[1])[0]).query)
    assert args['solution_type'] == ['advertising', 'engagement']
    assert args['observation'] == ['appeared', 'returned']
    assert args['page'] == ['1'] and args['status'] == ['contact']
