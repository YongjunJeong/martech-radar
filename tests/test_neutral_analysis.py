"""Neutral analysis must cover every category without losing review history."""
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from radar import brief, calls, config, notify, signals
from radar.store import Store
from radar.targets import Target, TargetSet
from web.app import create_app

CATEGORIES = ('advertising', 'analytics', 'attribution', 'engagement', 'experimentation', 'platform')


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'load', lambda *a, **k: config.Config())
    with Store(tmp_path / 'radar.db') as s:
        s.import_watchlist(TargetSet(
            targets=(Target(id='shop', company='Shop', industry='retail',
                            urls=('https://shop.test/',)),),
            industries={'retail': 'Retail'}, source='test'))
        yield s


def record(store, *tools, status='OK'):
    run = store.start_run(1)
    timestamp = (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=7 * run)).isoformat()
    scan = store.record_scan(run, 'shop', {
        'schema_version': 1, 'scan': {'url': 'https://shop.test/', 'page_host': 'shop.test',
                                     'status': status, 'started_at': timestamp},
        'evidence': {}, 'counts': {}, 'warnings': [],
    })
    store.record_detections(scan, {'detections': [
        {'id': name, 'name': name, 'category': cat, 'verdict': 'DETECTED',
         'score': 90, 'direct_layers': ['network'], 'indirect_layers': [],
         'indirect_only': False, 'match_count': 1, 'matches': []}
        for name, cat in tools
    ]})
    store.finish_run(run)


@pytest.mark.parametrize('category', CATEGORIES)
def test_every_category_emits_arrival_and_confirmed_absence(store, category):
    record(store)
    record(store, ('tool', category))
    assert [s.kind for s in signals.all_signals(store)] == [signals.KIND_NEW]
    # A one-tool target is included; no maturity gate.
    assert calls.call_list(store)[0]['entries'][0]['vendors'] == ['tool']
    record(store, status='BLOCKED')
    assert signals.changes_for(store, 'shop')[0].state == signals.STATE_NEW
    record(store)
    assert signals.changes_for(store, 'shop')[0].state == signals.STATE_REMOVAL_CANDIDATE
    record(store)
    assert signals.changes_for(store, 'shop')[0].state == signals.STATE_REMOVED
    assert [s.kind for s in signals.all_signals(store)] == [signals.KIND_REMOVAL]


@pytest.mark.parametrize('category', CATEGORIES)
def test_same_category_changes_are_grouped_without_replacement_claim(store, category):
    record(store, ('old', category))
    record(store, ('new', category))
    found = signals.all_signals(store, language='ko')
    assert len(found) == 1 and found[0].kind == signals.KIND_MIGRATION
    assert '교체 확정은 아닙니다' in found[0].detail


def test_cross_category_changes_are_not_paired(store):
    record(store, ('old', 'analytics'))
    record(store, ('new', 'engagement'))
    assert {s.kind for s in signals.all_signals(store)} == {signals.KIND_NEW, signals.KIND_REMOVAL}


def test_counts_are_equal_weight_across_categories(store):
    record(store, *((c, c) for c in CATEGORIES))
    counts = signals.maturity_of(store, 'shop')
    assert counts.score == counts.total == len(CATEGORIES)


@pytest.mark.parametrize('legacy', ['HOME_NEW', 'COMPETITOR_NEW'])
def test_legacy_notification_and_ack_keys_remain_effective(store, legacy):
    record(store)
    record(store, ('tool', 'analytics'))
    signal = signals.all_signals(store)[0]
    key = notify.signal_key(signal)
    old_key = key.replace('NEW_DETECTION:', legacy + ':')
    store.record_notified([(old_key, legacy, 'shop')], '2026-01-20T00:00:00+00:00')
    store.conn.execute('INSERT INTO signal_ack (key,state,noted_at) VALUES (?,?,?)',
                       (old_key, 'DISMISSED', '2026-01-20T00:00:00+00:00'))
    store.conn.commit()
    assert notify.notify_new_signals(store, dry_run=True)['pending'] == 0
    assert calls.call_list(store) == []
    store.set_signal_ack(key, None)
    assert calls.call_list(store)[0]['entries'][0]['ack'] == ''


def test_delivery_limit_leaves_undelivered_changes_pending(store, monkeypatch):
    record(store)
    record(store, ('one', 'analytics'), ('two', 'advertising'))
    monkeypatch.setattr(config, 'load', lambda: config.Config(
        notify=config.NotifySettings(max_items=1)))
    sent = []
    first = notify.notify_new_signals(store, webhook='https://example.test',
                                     transport=lambda _, payload: sent.append(payload))
    assert first == {'pending': 2, 'sent': 1}
    assert len(store.notified_keys()) == 1
    second = notify.notify_new_signals(store, webhook='https://example.test',
                                      transport=lambda _, payload: sent.append(payload))
    assert second == {'pending': 1, 'sent': 1}


@pytest.mark.parametrize('language', ['ko', 'en'])
def test_web_and_reports_use_neutral_language_and_categories(store, language):
    record(store)
    record(store, ('analytics_tool', 'analytics'))
    client = TestClient(create_app(db_path=str(store.path), language=language))
    texts = [brief.weekly_digest(store, language), brief.company_brief(store, 'shop', language=language)[0],
             calls.render(calls.call_list(store, language), language)]
    for route in ('/', '/signals', '/calls', '/companies', '/companies/shop', '/companies/shop/brief'):
        response = client.get(route)
        assert response.status_code == 200
        texts.append(response.text)
    for text in texts:
        for word in ('경쟁사', '자사', '공략 가능', '성숙도', 'greenfield', 'competitor', 'maturity '):
            assert word not in text
    assert 'analytics_tool' in client.get('/').text
    assert 'Shop' in client.get('/companies?state=observed').text
    assert 'Shop' not in client.get('/companies?state=no_tools').text


def test_no_tools_filter_includes_observed_absence_but_not_blocked_targets(store):
    record(store, ('old', 'analytics'))
    record(store)
    client = TestClient(create_app(db_path=str(store.path), language='en'))
    assert 'Shop' in client.get('/companies?state=no_tools').text
    assert 'Shop' in client.get('/companies?state=moved').text
    store.conn.execute('UPDATE target SET enabled=0')
    store.conn.commit()
    assert 'Shop' not in client.get('/companies').text
