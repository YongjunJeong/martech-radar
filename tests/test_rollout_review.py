"""Follow-up must preserve failures and never imply missing samples are healthy."""

from datetime import datetime, timedelta
import importlib.util
import json
import pytest
import sqlite3
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    'review_rollout', Path(__file__).resolve().parents[1] / 'deploy/review_rollout.py')
rollout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollout)


def test_future_probe_does_not_read_db_or_request_http(tmp_path, monkeypatch):
    def unexpected(*args):
        raise AssertionError('future probe touched live work')
    monkeypatch.setattr(rollout, 'http_sample', unexpected)
    day = (datetime.now(rollout.ZONE) + timedelta(days=1)).date().isoformat()
    assert rollout.run(tmp_path, day, tmp_path / 'out', tmp_path / 'backups')['state'] == 'waiting_for_date'
    assert not (tmp_path / 'out').exists()


def test_latency_groups_collection_and_errors_separately():
    samples = [dict(phase=phase, http={p: result for p in rollout.PATHS})
               for phase, result in [('during', {'ms': 20}), ('during', {'ms': 80}),
                                     ('during', {'error': 'TimeoutError'}), ('after', {'ms': 10})]]
    result = rollout.summarize_http(samples)
    assert result['during']['/'] == dict(samples=3, errors=1, median_ms=50, p95_ms=80, max_ms=80)
    assert result['after']['/']['median_ms'] == 10
    assert result['waiting']['/']['median_ms'] is None


def test_old_backup_and_excess_retention_are_findings(tmp_path, monkeypatch):
    for index in range(4):
        folder = tmp_path / f'radar-{index}'
        folder.mkdir()
        (folder / 'data').write_bytes(b'test')
    monkeypatch.setattr(rollout.recovery, 'verify', lambda _: {
        'created_at': '2026-11-02T00:00:00+09:00', 'database': {}})
    result = rollout.check_backup(tmp_path, '2026-11-03', [])
    assert result['verified']
    assert result['findings'] == ['fresh_backup_missing', 'backup_retention_exceeded']
    assert len(list(tmp_path.iterdir())) == 4  # probe never prunes


def test_corrupt_backup_is_recorded(tmp_path, monkeypatch):
    (tmp_path / 'radar-snapshot').mkdir()
    def fail(_):
        raise ValueError('corrupt')
    monkeypatch.setattr(rollout.recovery, 'verify', fail)
    result = rollout.check_backup(tmp_path, '2026-11-03', [])
    assert not result['verified']
    assert result['findings'] == ['backup_verification_failed:ValueError']


@pytest.mark.parametrize('timing_error', [False, True])
def test_final_review_flags_missing_during_samples_and_is_idempotent(tmp_path, monkeypatch, timing_error):
    day = (datetime.now(rollout.ZONE) - timedelta(days=1)).date().isoformat()
    monkeypatch.setattr(rollout, 'http_sample', lambda *a: {
        'phase': 'after', 'http': {p: {'ms': 10} for p in rollout.PATHS}})
    monkeypatch.setattr(rollout, 'queue_state', lambda *a: ([{
        'started_at': day + 'T03:00:00+09:00', 'finished_at': day + 'T04:00:00+09:00'}], 0))
    monkeypatch.setattr(rollout.operations, 'review', lambda *a: {'complete': True, 'findings': []})
    monkeypatch.setattr(rollout, 'check_backup', lambda *a: {'findings': []})
    def metrics(*args):
        if timing_error:
            raise sqlite3.OperationalError('fixture unavailable')
        return {'measured_scans': 1}
    monkeypatch.setattr(rollout.collection_metrics, 'summarize', metrics)
    result = rollout.run(tmp_path, day, tmp_path / 'out', tmp_path / 'backups')
    assert result['state'] == 'reviewed' and not result['healthy']
    assert all(f.startswith('http_samples_missing:during:') or f == 'collection_timing_report_unavailable'
               for f in result['findings'])
    assert ('collection_timing_report_unavailable' in result['findings']) == timing_error
    path = tmp_path / 'out' / f'http-{day}.jsonl'
    before = path.read_bytes()
    assert rollout.run(tmp_path, day, tmp_path / 'out', tmp_path / 'backups')['state'] == 'already_reviewed'
    assert path.read_bytes() == before
    report = json.loads((tmp_path / 'out' / f'review-{day}.json').read_text())
    assert report['http']['after']['/']['median_ms'] == 10
    assert report['collection_performance'] == ({'error': 'OperationalError'} if timing_error else {'measured_scans': 1})
