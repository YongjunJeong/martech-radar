"""OR across observation tags; AND with type on each solution, without duplicates."""
import copy
from html import unescape
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

import test_analysis_window as analysis
from web import sales
from web.app import create_app

scoped = analysis.scoped
scan = analysis.scan
BRAZE = 'sdk.iad-03.braze.com'
MOENGAGE = 'cdn.moengage.com'
ADS = 'googleadservices.com'
CHAT = 'channel.io'


def mixed(store):
    for day, hosts in [(15, [BRAZE, ADS, CHAT]), (16, [ADS, CHAT]),
                       (17, [BRAZE, MOENGAGE, CHAT]), (18, [BRAZE, MOENGAGE, CHAT])]:
        scan(store, f'2026-10-{day}T03:00:00+09:00', hosts)


def links(html):
    return [unescape(url) for url in re.findall(r'href="([^"]+)"', html)]


def test_two_arrival_tags_union_and_type_intersection(scoped):
    mixed(scoped)
    board = sales.board(scoped, {}, observation=['appeared', 'returned'], solution_type='engagement')
    assert board['scoped_total'] == board['view_counts']['contact'] == 1
    card = board['cards'][0]
    assert {e['id'] for e in card['events']} == {'braze', 'moengage'}
    assert card['decision']['allowed_count'] == 2
    assert board['observation'] == ('appeared', 'returned')
    assert board['observation_label'] == '미검출 → 검출 또는 다시 검출'
    assert not sales.board(scoped, {}, observation=['appeared', 'returned'], solution_type='advertising')['cards']
    scan(scoped, '2026-10-19T03:00:00+09:00', status='TIMEOUT')
    board = sales.board(scoped, {}, observation=['appeared', 'returned'], solution_type='engagement')
    assert board['counts']['check'] == 1 and board['cards'][0]['decision']['allowed_count'] == 0


def test_overlapping_direction_and_fluctuation_tags_do_not_double_count(scoped):
    for day, hosts in [(15, [BRAZE]), (16, []), (17, [BRAZE]), (18, []), (19, [BRAZE])]:
        scan(scoped, f'2026-10-{day}T03:00:00+09:00', hosts)
    board = sales.board(scoped, {}, observation=['returned', 'fluctuating', 'returned'])
    assert board['observation'] == ('returned', 'fluctuating')
    assert board['filtered'] == board['view_counts']['watch'] == 1
    assert len(board['cards'][0]['events']) == 1
    assert board['cards'][0]['events'][0]['pattern']['fluctuating']
    text = TestClient(create_app(str(scoped.path), language='ko')).get(
        '/?observation=returned&observation=fluctuating&observation=returned').text
    assert '변화가 관측된 솔루션 1개' in text and '위 변화 중 1개는' in text


def test_stable_can_be_selected_together_with_changes(scoped):
    mixed(scoped)
    board = sales.board(scoped, {}, observation=['stable', 'appeared'])
    assert {t['id'] for t in board['cards'][0]['tools']} == {'channel_talk', 'moengage'}
    assert len(board['cards'][0]['events']) == 1
    text = TestClient(create_app(str(scoped.path), language='ko')).get(
        '/?observation=stable&observation=appeared').text
    assert '함께 선택된 ‘계속 검출’ 솔루션 1개 보기' in text and 'Channel Talk' in text
    support = sales.board(scoped, {}, observation=['stable', 'appeared'], solution_type='customer_support')
    assert support['view_counts']['steady'] == 1 and not support['cards'][0]['events']


def test_no_checked_tags_and_old_empty_parameter_remove_restriction(scoped):
    mixed(scoped)
    full = sales.board(scoped, {}, status='all')
    for selected in ([], [''], '', None):
        board = sales.board(scoped, {}, observation=selected, status='all')
        assert board['observation'] == ()
        assert len(board['cards'][0]['tools']) == len(full['cards'][0]['tools'])
    assert sales.normalize_observations(['', 'returned', 'appeared', 'returned']) == ('appeared', 'returned')
    with pytest.raises(ValueError):
        sales.normalize_observations(['appeared', 'invalid'])


def test_repeated_query_params_survive_routes_status_detail_and_back(scoped):
    mixed(scoped)
    client = TestClient(create_app(str(scoped.path), language='ko'))
    query = 'solution_type=engagement&observation=appeared&observation=returned'
    for route in ('/', '/companies', '/calls'):
        response = client.get(route + '?' + query)
        assert response.status_code == 200
        text = response.text
        assert 'value="appeared" checked' in text and 'value="returned" checked' in text
        assert '태그 중 하나라도 해당하면' in text
        urls = [u for u in links(text) if '/companies/acme?' in u or 'status=watch' in u]
        assert len(urls) >= 4
        for url in urls:
            args = parse_qs(urlsplit(url).query)
            assert args['observation'] == ['appeared', 'returned']
            assert args['solution_type'] == ['engagement']
        assert client.get(route + '?' + query + '&observation=invalid').status_code == 422
    detail = client.get('/companies/acme?' + query)
    assert detail.status_code == 200
    text = detail.text.split('<details class="technical">')[0]
    assert '미검출 → 검출 또는 다시 검출' in text
    assert 'Google Ads' not in text and 'Channel Talk' not in text
    back = next(u for u in links(text) if u.startswith('/?status='))
    assert parse_qs(urlsplit(back).query)['observation'] == ['appeared', 'returned']
    assert client.get('/companies/acme?' + query + '&observation=invalid').status_code == 422


def test_pagination_keeps_all_selected_tags(scoped, monkeypatch):
    mixed(scoped)
    base = sales.company_cards(scoped, {}, observation=['appeared', 'returned'], solution_type='engagement')[0]
    cards = [dict(copy.deepcopy(base), id=f'co{i}', company=f'기업 {i}') for i in range(30)]
    monkeypatch.setattr(sales, 'board_candidates', lambda *a, **kw: cards)
    monkeypatch.setattr(sales, 'company_cards', lambda *a, **kw: [
        c for c in cards if c['id'] in kw['target_ids']])
    query = 'solution_type=engagement&observation=appeared&observation=returned&status=contact&page=2'
    text = TestClient(create_app(str(scoped.path), language='ko')).get('/?' + query).text
    pagination = text.split('<nav class="pagination"')[1]
    args = parse_qs(urlsplit(links(pagination)[0]).query)
    assert args['observation'] == ['appeared', 'returned']
    assert args['solution_type'] == ['engagement'] and args['page'] == ['1']
    assert args['status'] == ['contact']
