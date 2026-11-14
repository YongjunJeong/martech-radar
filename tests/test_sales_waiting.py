"""Waiting guidance agrees with scheduling, without changing live schedules."""
from dataclasses import replace
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from radar import batch, config
from web import sales
from web.app import create_app
import test_analysis_window as analysis

scoped = analysis.scoped
scan = analysis.scan


def setup_waiting(store, monkeypatch, *, absence=False):
    settings = replace(config.load(), cadence=config.CadenceSettings(spread_weekly=True),
                       scan=config.ScanSettings(timezone='Asia/Seoul'))
    monkeypatch.setattr(config, 'load', lambda *a, **k: settings)
    host = ['sdk.iad-03.braze.com']
    scan(store, '2026-10-15T03:10:00+09:00', host if absence else [])
    scan(store, '2026-10-19T03:10:00+09:00', [] if absence else host)
    store.conn.execute('INSERT INTO scan_schedule VALUES (?,?,?,?)',
                       ('acme', 'https://acme.test/', '2026-10-19', None))
    store.conn.commit()
    return sales.company_cards(store, {})


@pytest.mark.parametrize('absence', [False, True])
def test_projection_matches_scheduler_and_does_not_write(scoped, monkeypatch, absence):
    cards = setup_waiting(scoped, monkeypatch, absence=absence)
    before = scoped.conn.total_changes
    now = datetime.fromisoformat('2026-10-21T15:01:00+00:00')  # Oct 22 KST
    sales.attach_waiting_guidance(scoped, cards, now)
    guidance = cards[0]['waiting']
    assert guidance['date'] == '2026-10-26'
    assert guidance['relative'] == '4일 후'
    assert ('미검출' in guidance['results'][0]) == absence
    assert scoped.conn.total_changes == before
    assert scoped.conn.execute('SELECT next_due FROM scan_schedule').fetchone()[0] == '2026-10-19'
    assert batch.due_split(scoped, batch.jobs_for(scoped), now=now)[0] == []
    assert scoped.conn.execute('SELECT next_due FROM scan_schedule').fetchone()[0] == guidance['date']


def test_overdue_today_and_active_queue_are_not_completion_promises(scoped, monkeypatch):
    cards = setup_waiting(scoped, monkeypatch)
    for at, expected in [('2026-10-26T00:00:00+09:00', '오늘 예정'),
                         ('2026-10-27T00:00:00+09:00', '예정일 지남 · 수집 대기')]:
        sales.attach_waiting_guidance(scoped, cards, datetime.fromisoformat(at))
        assert cards[0]['waiting']['relative'] == expected
    run = scoped.start_run(1)
    scoped.conn.execute("INSERT INTO job(run_id,target_id,url,state,created_at) VALUES (?,?,?,'pending',?)",
                        (run, 'acme', 'https://acme.test/', '2026-10-26T00:00:00+09:00'))
    sales.attach_waiting_guidance(scoped, cards)
    assert cards[0]['waiting']['date_label'] == '수집 순서 대기 중'
    scoped.conn.execute("UPDATE job SET state='claimed'")
    sales.attach_waiting_guidance(scoped, cards)
    assert cards[0]['waiting']['date_label'] == '수집 중'


def test_board_and_detail_share_guidance_and_failed_scan_overrides_it(scoped, monkeypatch):
    setup_waiting(scoped, monkeypatch, absence=True)
    client = TestClient(create_app(str(scoped.path), language='ko'))
    for path in ('/?status=watch', '/companies/acme'):
        response = client.get(path)
        assert response.status_code == 200
        assert '다음 확인 예정' in response.text
        assert '2026.10.26' in response.text
        assert '최근 보이지 않은 솔루션 1개' in response.text
        assert '예정일은 연락 가능일이 아닙니다' in response.text
    detail = client.get('/companies/acme').text
    assert '2회 이상 연속 미검출' in detail and '7일 이상' in detail
    scan(scoped, '2026-10-20T03:10:00+09:00', status='BLOCKED')
    card = sales.company_cards(scoped, {})[0]
    assert card['bucket'] == 'check' and 'waiting' not in card
    assert '다음 확인 예정' not in client.get('/companies/acme').text


def test_reappearance_waits_for_detection_and_not_adoption(scoped, monkeypatch):
    setup_waiting(scoped, monkeypatch, absence=True)
    scan(scoped, '2026-10-20T03:10:00+09:00', ['sdk.iad-03.braze.com'])
    card = sales.company_cards(scoped, {})[0]
    assert card['events'][0]['state'] == 'REAPPEARED'
    assert '검출되는지' in card['events'][0]['waiting_result']
    assert '미검출인지' not in card['events'][0]['waiting_result']
    assert card['waiting']['date'] == '2026-10-26'


def test_multiple_urls_show_earliest_date_without_per_company_queries(scoped, monkeypatch):
    cards = setup_waiting(scoped, monkeypatch)
    cards[0]['urls'].append('https://acme.test/shop')
    scoped.conn.execute('INSERT INTO scan_schedule VALUES (?,?,?,?)',
                       ('acme', 'https://acme.test/shop', '2026-10-24', None))
    queries = []
    scoped.conn.set_trace_callback(queries.append)
    sales.attach_waiting_guidance(scoped, cards * 25, datetime.fromisoformat('2026-10-22T00:00:00+09:00'))
    scoped.conn.set_trace_callback(None)
    assert len(queries) == 3
    assert cards[0]['waiting']['date'] == '2026-10-24'
    assert cards[0]['waiting']['multiple_urls']


def test_daily_rollout_ignores_weekly_rows_and_uses_next_kst_day(scoped, monkeypatch):
    cards = setup_waiting(scoped, monkeypatch)
    settings = replace(config.load(), cadence=config.CadenceSettings(default='1d', daily_limit=1000))
    monkeypatch.setattr(config, 'load', lambda *a, **k: settings)
    # Weekly row remains on Oct 19, but the last attempt is Oct 19 KST.
    now = datetime.fromisoformat('2026-10-19T15:01:00+00:00')  # Oct 20 KST
    sales.attach_waiting_guidance(scoped, cards, now)
    assert cards[0]['waiting']['date'] == '2026-10-20'
    assert cards[0]['waiting']['relative'] == '오늘 예정'
    assert len(batch.due_split(scoped, batch.jobs_for(scoped), now=now)[0]) == 1
    scan(scoped, '2026-10-20T03:00:00+09:00', status='TIMEOUT')
    # Failed attempts also receive a full day's quiet, regardless of stale rows.
    sales.attach_waiting_guidance(scoped, cards, datetime.fromisoformat('2026-10-20T04:00:00+09:00'))
    assert cards[0]['waiting']['date'] == '2026-10-21'
    assert not batch.due_split(scoped, batch.jobs_for(scoped), now=datetime.fromisoformat('2026-10-20T04:00:00+09:00'))[0]


def test_daily_absence_still_requires_seven_days(scoped, monkeypatch):
    setup_waiting(scoped, monkeypatch, absence=True)
    settings = replace(config.load(), cadence=config.CadenceSettings(default='1d', daily_limit=1000))
    monkeypatch.setattr(config, 'load', lambda *a, **k: settings)
    for day in range(20, 26):
        scan(scoped, f'2026-10-{day}T03:10:00+09:00')
    card = sales.company_cards(scoped, {})[0]
    assert card['bucket'] == 'watch' and card['events'][0]['streak'] == 7
    assert card['waiting']['date'] == '2026-10-26'
    scan(scoped, '2026-10-26T03:10:00+09:00')
    assert sales.company_cards(scoped, {})[0]['bucket'] == 'contact'


def test_daily_start_date_gates_collection_and_display_at_kst_midnight(scoped, monkeypatch):
    cards = setup_waiting(scoped, monkeypatch)
    settings = replace(config.load(), cadence=config.CadenceSettings(
        default='1d', daily_limit=1000, not_before='2026-10-23'))
    monkeypatch.setattr(config, 'load', lambda *a, **k: settings)
    before = datetime.fromisoformat('2026-10-22T14:59:59+00:00')
    after = datetime.fromisoformat('2026-10-22T15:00:00+00:00')
    assert not batch.due_split(scoped, batch.jobs_for(scoped), now=before)[0]
    assert len(batch.due_split(scoped, batch.jobs_for(scoped), now=after)[0]) == 1
    sales.attach_waiting_guidance(scoped, cards, before)
    assert cards[0]['waiting']['date'] == '2026-10-23'
    assert cards[0]['waiting']['relative'] == '1일 후'
