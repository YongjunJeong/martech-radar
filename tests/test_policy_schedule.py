from datetime import UTC, datetime, timedelta
import fcntl
import json

import pytest

from radar import policy_schedule as schedule, policy_references as policies
from radar.store import Store

NOW = datetime(2026, 11, 9, 12, tzinfo=schedule.ZONE)


def seed(home, count=8):
    with Store(home/'data/radar.db') as store:
        for n in range(count):
            store.upsert_target(id=f't{n}', company=f'T{n}', industry='retail', urls=[f'https://t{n}.test/'])


def save(store, target_id, days, status='READ'):
    value = {'target_id': target_id, 'checked_at': (NOW-timedelta(days=days)).astimezone(UTC).isoformat(), 'status': status}
    store.conn.execute('INSERT INTO policy_reference VALUES (?,?)', (target_id, json.dumps(value)))
    store.conn.commit()


def test_due_order_weekly_retry_and_no_disabled_targets(tmp_path):
    seed(tmp_path)
    with Store(tmp_path/'data/radar.db') as store:
        policies.initialize(store)
        save(store, 't0', 8)
        save(store, 't1', 6, 'UNAVAILABLE')  # not retried every day by automatic scheduling
        save(store, 't2', 10, 'UNAVAILABLE')
        store.conn.execute("UPDATE target SET enabled=0 WHERE id='t7'")
        store.conn.commit()
        assert schedule.due_targets(store, NOW) == ['t3', 't4', 't5', 't6', 't2']
        assert 't1' not in schedule.due_targets(store, NOW, 20)
        plan = store.conn.execute("EXPLAIN QUERY PLAN SELECT target_id FROM policy_reference WHERE json_extract(payload_json,'$.checked_at')<=? ORDER BY json_extract(payload_json,'$.checked_at')", (NOW.isoformat(),)).fetchall()
        assert any('policy_reference_checked' in r[3] for r in plan)


def test_nighttime_never_opens_database(tmp_path):
    result = schedule.run(tmp_path, NOW.replace(hour=3))
    assert result['state'] == 'outside_daytime_window'
    assert not list(tmp_path.iterdir())


def test_host_or_queue_busy_defers_without_network(tmp_path, monkeypatch):
    seed(tmp_path)
    monkeypatch.setattr(schedule, 'host_busy', lambda: True)
    monkeypatch.setattr(policies, 'collect', lambda *a, **kw: pytest.fail('unexpected collection'))
    assert schedule.run(tmp_path, NOW)['state'] == 'deferred_busy'
    monkeypatch.setattr(schedule, 'host_busy', lambda: False)
    with Store(tmp_path/'data/radar.db') as store:
        from radar.batch import Job
        store.queue_run([Job('t0','T0','https://t0.test/')])
    assert schedule.run(tmp_path, NOW)['state'] == 'deferred_busy'


def test_singleton_lock_prevents_concurrent_batch(tmp_path):
    seed(tmp_path)
    with (tmp_path/'data/policy-schedule.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert schedule.run(tmp_path, NOW)['state'] == 'already_running'


def test_batch_caps_at_five_and_preserves_browser_rows(tmp_path, monkeypatch):
    seed(tmp_path)
    monkeypatch.setattr(schedule, 'host_busy', lambda: False)
    calls = []
    def collect(store, ident, url, **kwargs):
        assert not kwargs['should_defer']()
        calls.append(ident)
        return {'target_id': ident, 'status': 'READ', 'request_count': 3}
    monkeypatch.setattr(policies, 'collect', collect)
    result = schedule.run(tmp_path, NOW)
    assert len(result['results']) == len(calls) == 5
    with Store(tmp_path/'data/radar.db') as store:
        for table in ('scan', 'detection', 'job'):
            assert store.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0


def test_primary_work_start_yields_without_consuming_next_target(tmp_path, monkeypatch):
    seed(tmp_path)
    monkeypatch.setattr(schedule, 'host_busy', lambda: False)
    calls = []
    def collect(store, ident, url, **kwargs):
        calls.append(ident)
        raise policies.PolicyDeferred()
    monkeypatch.setattr(policies, 'collect', collect)
    result = schedule.run(tmp_path, NOW)
    assert result['state'] == 'deferred_primary_work' and result['results'] == []
    assert calls == ['t0']


def test_fetch_guard_cancels_before_any_request(monkeypatch):
    fetcher = policies.Fetcher(['https://example.com/'])
    fetcher.should_defer = lambda: True
    monkeypatch.setattr(fetcher.opener, 'open', lambda *a, **kw: pytest.fail('unexpected HTTP'))
    with pytest.raises(policies.PolicyDeferred):
        fetcher.fetch('https://example.com/privacy')
    assert fetcher.requests == 0


def test_target_exception_is_recorded_and_does_not_starve_remaining_rotation(tmp_path, monkeypatch):
    seed(tmp_path)
    monkeypatch.setattr(schedule, 'host_busy', lambda: False)
    calls = []
    def collect(store, ident, url, **kwargs):
        calls.append(ident)
        if ident == 't0':
            raise TypeError('broken link with private data that must not be logged')
        return {'target_id': ident, 'status': 'READ', 'request_count': 1}
    monkeypatch.setattr(policies, 'collect', collect)
    result = schedule.run(tmp_path, NOW)
    assert calls == ['t0', 't1', 't2', 't3', 't4']
    assert result['state'] == 'completed_with_errors'
    assert result['results'][0]['status'] == 'ERROR'
    assert result['results'][0]['error_type'] == 'TypeError'
    with Store(tmp_path/'data/radar.db') as store:
        error = policies.read(store, 't0')
        assert error['status'] == 'ERROR' and error['documents'] == []
        assert 'private data' not in json.dumps(error)
        assert 't0' not in schedule.due_targets(store, datetime.now(UTC))
        assert store.conn.execute('SELECT count(*) FROM scan').fetchone()[0] == 0


def test_storage_failure_is_not_hidden_as_success(tmp_path, monkeypatch):
    import sqlite3
    seed(tmp_path)
    monkeypatch.setattr(schedule, 'host_busy', lambda: False)
    def failed(*args, **kwargs):
        raise sqlite3.OperationalError('write failed')
    monkeypatch.setattr(policies, 'collect', failed)
    with pytest.raises(sqlite3.OperationalError):
        schedule.run(tmp_path, NOW)


def test_collector_error_keeps_previous_readable_reference(tmp_path):
    seed(tmp_path, 1)
    with Store(tmp_path/'data/radar.db') as store:
        policies.initialize(store)
        original = {'source': {'roots':['https://t0.test/'], 'requested_policy_url':None},
                    'parser_version': policies.PARSER_VERSION, 'checked_at':NOW.isoformat(),
                    'status':'READ', 'documents':[{'url':'https://t0.test/privacy','mentions':[]}]}
        store.conn.execute('INSERT INTO policy_reference VALUES (?,?)', ('t0',json.dumps(original)))
        store.conn.commit()
        result = policies.record_error(store,'t0',TypeError('bad'))
        assert result['status']=='ERROR'
        assert result['previous_readable']=={'checked_at':original['checked_at'],'documents':original['documents']}
