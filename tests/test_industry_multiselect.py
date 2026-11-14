"""Industry union combines with the per-solution type/tag intersection."""
import copy
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

import test_analysis_window as analysis
from test_observation_multiselect import links
from radar import detector, registry
from radar.targets import Target, TargetSet
from web import sales
from web.app import create_app

scoped = analysis.scoped
BRAZE = 'sdk.iad-03.braze.com'
ADS = 'googleadservices.com'
CHAT = 'channel.io'
INDUSTRIES = {'retail': '쇼핑', 'finance': '금융', 'media': '미디어', 'custom_sector': '사용자 정의 업종'}


def seed(store):
    entries = [('acme', 'retail', [ADS]), ('bank', 'finance', [BRAZE]),
               ('publisher', 'media', [ADS]), ('custom', 'custom_sector', [CHAT])]
    store.import_watchlist(TargetSet(tuple(Target(key, key, industry, (f'https://{key}.test/',))
                                          for key, industry, _ in entries), INDUSTRIES, 'test'))
    fingerprints = registry.load()
    for day in (15, 16, 17):
        run = store.start_run(4)
        for key, _, hosts in entries:
            result = {'scan': {'url': f'https://{key}.test/', 'page_host': f'{key}.test/',
                              'status': 'OK', 'started_at': f'2026-10-{day}T03:00:00+09:00'},
                      'evidence': {'network': {'hosts': hosts if day > 15 else []}}, 'counts': {}, 'warnings': []}
            sid = store.record_scan(run, key, result)
            store.record_detections(sid, detector.detect(result, fingerprints))


def test_union_of_industries_combines_with_both_solution_filters(scoped):
    seed(scoped)
    selected = dict(industry=['retail', 'finance'], solution_type=['advertising', 'engagement'],
                    observation=['appeared', 'returned'])
    board = sales.board(scoped, INDUSTRIES, **selected)
    assert {c['id'] for c in board['cards']} == {'acme', 'bank'}
    assert board['scoped_total'] == board['view_counts']['contact'] == 2
    assert board['total'] == 4
    assert sum(board['view_counts'].values()) == board['scoped_total']
    assert sales.board(scoped, INDUSTRIES, **dict(selected, q='bank'))['scoped_total'] == 1
    assert sales.board(scoped, INDUSTRIES, **dict(selected, solution_type=['advertising']))['scoped_total'] == 1
    assert sales.board(scoped, INDUSTRIES, **dict(selected, observation=['missing']))['scoped_total'] == 0
    board = sales.board(scoped, INDUSTRIES, **dict(selected, industry=['finance', 'retail', 'retail', '']))
    assert board['industry'] == ('retail', 'finance') and board['scoped_total'] == 2


def test_empty_industries_only_clear_industry_scope_and_custom_codes_work(scoped):
    seed(scoped)
    for selection in ('', [], [''], None):
        board = sales.board(scoped, INDUSTRIES, industry=selection, solution_type=['advertising', 'engagement'],
                            observation=['appeared', 'returned'])
        assert board['industry'] == () and board['scoped_total'] == 3
        assert 'custom' not in {c['id'] for c in board['cards']}
    custom = sales.board(scoped, INDUSTRIES, industry=['custom_sector', 'finance'], observation='appeared')
    assert {c['id'] for c in custom['cards']} == {'custom', 'bank'}
    with pytest.raises(ValueError, match='unknown industry'):
        sales.board(scoped, INDUSTRIES, industry=['finance', 'unknown'])


def test_routes_preserve_three_filter_lists_and_old_single_industry_links(scoped):
    seed(scoped)
    client = TestClient(create_app(str(scoped.path), language='ko'))
    expected = {'industry': ['retail', 'finance'], 'solution_type': ['advertising', 'engagement'],
                'observation': ['appeared', 'returned']}
    query = '&'.join(f'{key}={v}' for key, values in expected.items() for v in values)
    for route in ('/', '/companies', '/calls'):
        response = client.get(route + '?' + query)
        assert response.status_code == 200
        text = response.text
        for value in ('retail', 'finance', 'advertising', 'engagement', 'appeared', 'returned'):
            assert f'value="{value}" checked' in text
        selected_links = [u for u in links(text) if '/companies/acme?' in u or 'status=watch' in u]
        assert len(selected_links) >= 4
        for url in selected_links:
            args = parse_qs(urlsplit(url).query)
            for key, values in expected.items():
                assert args[key] == values
        assert client.get(route + '?industry=retail&status=all').status_code == 200
        assert client.get(route + '?industry=finance&industry=unknown').status_code == 422
        assert client.get(route + '?industry=custom_sector&status=all').status_code == 200
    detail = client.get('/companies/acme?' + query)
    assert detail.status_code == 200 and '목록에서 선택한 업종:' in detail.text
    back = next(u for u in links(detail.text) if u.startswith('/?status='))
    args = parse_qs(urlsplit(back).query)
    for key, values in expected.items():
        assert args[key] == values
    assert client.get('/companies/acme?industry=unknown').status_code == 422


def test_pagination_and_counts_follow_combined_scope(scoped, monkeypatch):
    seed(scoped)
    base = sales.company_cards(scoped, INDUSTRIES, 'acme', solution_type=['advertising', 'engagement'],
                               observation=['appeared', 'returned'])[0]
    cards = [dict(copy.deepcopy(base), id=f'co{i}', company=f'기업 {i}',
                  industry='media' if i >= 30 else ('finance' if i % 2 else 'retail')) for i in range(35)]
    monkeypatch.setattr(sales, 'board_candidates', lambda *a, **kw: cards)
    monkeypatch.setattr(sales, 'company_cards', lambda *a, **kw: [
        c for c in cards if c['id'] in kw['target_ids']])
    board = sales.board(scoped, INDUSTRIES, industry=['retail', 'finance'], solution_type=['advertising', 'engagement'],
                        observation=['appeared', 'returned'], status='contact', page=2)
    assert board['total'] == 35 and board['scoped_total'] == board['filtered'] == 30
    assert board['view_counts']['contact'] == 30 and len(board['cards']) == 6
    query = 'industry=retail&industry=finance&solution_type=advertising&solution_type=engagement&observation=appeared&observation=returned&status=contact&page=2'
    text = TestClient(create_app(str(scoped.path), language='ko')).get('/?' + query).text
    args = parse_qs(urlsplit(links(text.split('<nav class="pagination"')[1])[0]).query)
    assert args['industry'] == ['retail', 'finance'] and args['page'] == ['1']
    assert args['solution_type'] == ['advertising', 'engagement']
    assert args['observation'] == ['appeared', 'returned']


def test_legacy_company_view_keeps_multi_industry_links(scoped):
    seed(scoped)
    client = TestClient(create_app(str(scoped.path), language='en'))
    text = client.get('/companies?industry=retail&industry=finance&state=observed').text
    assert 'acme' in text and 'bank' in text
    nav = text.split('<nav class="seg">')[1].split('</nav>')[0]
    for url in links(nav):
        assert parse_qs(urlsplit(url).query)['industry'] == ['retail', 'finance']
