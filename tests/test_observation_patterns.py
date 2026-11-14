"""Observation direction must not be confused with collection health or readiness."""
import pytest
from fastapi.testclient import TestClient

import test_analysis_window as analysis
from web import sales
from web.app import create_app

scoped = analysis.scoped
scan = analysis.scan
BRAZE = 'sdk.iad-03.braze.com'


def trace(store, values):
    for day, present in enumerate(values, 15):
        scan(store, f'2026-10-{day:02}T03:00:00+09:00', [BRAZE] if present else [])
    return sales.company_cards(store, {}, 'acme')[0]


@pytest.mark.parametrize('values,key,bucket', [
    ([False, True], 'appeared', 'watch'),
    ([True, False], 'missing', 'watch'),
    ([True, False, True], 'returned', 'watch'),
    ([True], 'baseline', 'baseline'),
    ([True, True], 'stable', 'steady'),
    ([False, True, True], 'appeared', 'contact'),
    ([True, False, True, True], 'returned', 'contact'),
])
def test_direction_is_separate_from_contact_readiness(scoped, values, key, bucket):
    card = trace(scoped, values)
    assert card['bucket'] == bucket
    assert card['tools'][0]['pattern']['key'] == key
    assert not card['tools'][0]['pattern']['fluctuating']
    client = TestClient(create_app(str(scoped.path), language='ko'))
    # The guide is separate; verify actual card and solution tags.
    home = client.get('/?status=all').text.split('<div class="sales-grid">')[1]
    section = ('<details class="change-story solution-detail">' if card['events']
               else '<details class="observed-tools">')
    detail = client.get('/companies/acme').text.split(section)[1]
    assert f'observation-tag pattern-{key}' in home
    assert f'observation-tag pattern-{key}' in detail


@pytest.mark.parametrize('values,key', [
    ([True, False, True, False], 'missing'),
    ([False, True, False, True], 'returned'),
])
def test_alternation_is_secondary_and_not_double_counted(scoped, values, key):
    card = trace(scoped, values)
    pattern = card['events'][0]['pattern']
    assert pattern['key'] == key and pattern['fluctuating']
    assert '4회' in pattern['fluctuation_note'] and '3번' in pattern['fluctuation_note']
    client = TestClient(create_app(str(scoped.path), language='ko'))
    for url in ('/', '/companies/acme'):
        text = client.get(url).text
        assert '변화가 관측된 솔루션 1개' in text
        assert '위 변화 중 1개는' in text
        assert text.count('<b>1개</b>') == 1
        assert pattern['fluctuation_note'] in text


def test_old_alternation_expires_from_recent_history(scoped):
    card = trace(scoped, [True, False, True, False] + [True] * 8)
    event = card['events'][0]
    assert event['pattern']['key'] == 'returned'
    assert not event['pattern']['fluctuating']
    assert len(event['history']) == 8
    assert all(h['result'] == '검출' for h in event['history'])


def test_failures_do_not_create_absences_and_override_contact(scoped):
    trace(scoped, [True, True])
    scan(scoped, '2026-10-17T03:00:00+09:00', status='BLOCKED')
    scan(scoped, '2026-10-18T03:00:00+09:00', [BRAZE])
    card = sales.company_cards(scoped, {})[0]
    assert card['tools'][0]['pattern']['key'] == 'stable'
    assert not card['events']
    scan(scoped, '2026-10-19T03:00:00+09:00')
    scan(scoped, '2026-10-26T03:00:00+09:00')
    assert sales.company_cards(scoped, {})[0]['bucket'] == 'contact'
    scan(scoped, '2026-10-27T03:00:00+09:00', status='TIMEOUT')
    card = sales.company_cards(scoped, {})[0]
    assert card['bucket'] == 'check'
    assert card['events'][0]['pattern']['key'] == 'missing'
    assert not card['events'][0]['contact_allowed']
    assert not card['events'][0]['pattern']['fluctuating']


def test_cutoff_and_site_reset_exclude_prior_patterns(scoped):
    for day, present in ((11, True), (12, False), (13, True), (14, False)):
        scan(scoped, f'2026-10-{day}T03:00:00+09:00', [BRAZE] if present else [])
    card = trace(scoped, [True])
    assert card['tools'][0]['pattern']['key'] == 'baseline'
    assert not card['tools'][0]['pattern']['fluctuating']
    scan(scoped, '2026-10-16T03:00:00+09:00')
    scan(scoped, '2026-10-17T03:00:00+09:00', [BRAZE])
    scan(scoped, '2026-10-18T03:00:00+09:00', [BRAZE], page_host='new.test')
    card = sales.company_cards(scoped, {})[0]
    assert card['bucket'] == 'check'
    assert card['tools'][0]['pattern']['key'] == 'baseline'
    assert not card['tools'][0]['pattern']['fluctuating']


def test_filter_keeps_only_matching_patterns_in_totals(scoped):
    scan(scoped, '2026-10-15T03:00:00+09:00', [BRAZE])
    scan(scoped, '2026-10-16T03:00:00+09:00', ['googleadservices.com'])
    full = sales.company_cards(scoped, {})[0]
    assert {e['pattern']['key'] for e in full['events']} == {'appeared', 'missing'}
    ads = sales.board(scoped, {}, solution_type='advertising')['cards'][0]
    assert len(ads['events']) == 1 and ads['events'][0]['pattern']['key'] == 'appeared'
    text = TestClient(create_app(str(scoped.path), language='ko')).get('/?solution_type=advertising').text
    cards = text.split('<div class="sales-grid">')[1]
    assert 'pattern-appeared' in cards and 'pattern-missing' not in cards
