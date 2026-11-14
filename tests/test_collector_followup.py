"""Scheduled follow-up reads only its day and never equates incomplete with absent."""
from datetime import datetime, timedelta
import gzip
import json
import sqlite3
from pathlib import Path
import pytest

with pytest.MonkeyPatch.context() as imports:
    imports.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    from deploy import review_collector_followup as followup


def database(home):
    (home / 'data').mkdir()
    path = home / 'data/radar.db'
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE run(id INTEGER,note TEXT);
            CREATE TABLE scan(id INTEGER,run_id INTEGER,target_id TEXT,status TEXT,duration_ms INTEGER,started_at TEXT,evidence_gz BLOB);
            CREATE TABLE detection(scan_id INTEGER,fingerprint_id TEXT,verdict TEXT,direct_layers TEXT,matches_json TEXT);''')
        db.executemany('INSERT INTO run VALUES (?,?)', [(1, 'scheduled 2026-11-03'), (2, 'scheduled 2026-11-04')])
        for i, (run_id, target, status, duration) in enumerate([
            (1, 'samsung_shop', 'OK', 12000), (2, 'samsung_shop', 'PARTIAL', 35000),
            (2, 'sbi_savings', 'BLOCKED', 1500), (2, 'ok_savings', 'TIMEOUT', 90100),
        ], start=1):
            blob = gzip.compress(json.dumps({'scan': {'collector_version': '0.2.7', 'stop_reason': 'test'}}).encode())
            db.execute('INSERT INTO scan VALUES (?,?,?,?,?,?,?)', (i, run_id, target, status, duration, '2026-11-04', blob))
        db.execute("INSERT INTO detection VALUES (1,'insider','DETECTED','network','[]')")
    return path


def test_future_review_never_opens_database(tmp_path):
    day = (datetime.now(followup.review_rollout.ZONE) + timedelta(days=1)).date().isoformat()
    result = followup.run(tmp_path, day, tmp_path / 'out', tmp_path / 'backups', tmp_path / 'missing.json')
    assert result['state'] == 'waiting_for_date'
    assert not (tmp_path / 'out').exists()
    assert not (tmp_path / 'data').exists()


def test_baseline_positive_is_not_reported_as_today_and_incomplete_is_unjudged(tmp_path):
    path = database(tmp_path)
    before = followup.focused_scans(path, '2026-11-03')
    after = followup.focused_scans(path, '2026-11-04')
    assert before['samsung_shop'][0]['insider']['verdict'] == 'DETECTED'
    assert after['samsung_shop'][0]['insider']['verdict'] == 'UNJUDGED'
    assert not after['samsung_shop'][0]['insider']['judged']
    assert after['sbi_savings'][0]['status'] == 'BLOCKED'
    assert after['welcome_savings'] == []


def test_comparison_preserves_failures_and_does_not_rewrite_finished_result(tmp_path, monkeypatch):
    database(tmp_path)
    out = tmp_path / 'out'
    out.mkdir()
    baseline = out / 'baseline.json'
    baseline.write_text(json.dumps({'day': '2026-11-03', 'recorded_jobs': 784, 'observation_errors': 75}))
    (out / 'review-2026-11-04.json').write_text(json.dumps({
        'day': '2026-11-04', 'recorded_jobs': 784, 'observation_errors': 74, 'findings': ['site_observation_errors']}))
    monkeypatch.setattr(followup.review_rollout, 'run', lambda *a: {'state': 'already_reviewed'})
    result = followup.run(tmp_path, '2026-11-04', out, tmp_path / 'backups', baseline)
    assert result['state'] == 'compared'
    report_path = out / 'comparison-2026-11-04.json'
    report = json.loads(report_path.read_text())
    assert 'long_wait_unresolved:ok_savings' in report['followup_findings']
    assert 'long_wait_unresolved:sbi_savings' not in report['followup_findings']
    assert 'focus_scan_missing:welcome_savings' in report['followup_findings']
    assert not report['samsung_insider_confirmed']
    assert report['after']['findings'] == ['site_observation_errors']
    assert report['after']['services'] is None
    assert report['after']['observation_error_rate'] == 74 / 784
    before = report_path.read_bytes()
    assert followup.run(tmp_path, '2026-11-04', out, tmp_path / 'backups', baseline)['state'] == 'already_compared'
    assert report_path.read_bytes() == before


def test_expected_version_is_configurable_and_mixed_samsung_results_do_not_pass(tmp_path, monkeypatch):
    path = database(tmp_path)
    with sqlite3.connect(path) as db:
        blob = gzip.compress(json.dumps({'scan': {'collector_version': '0.2.8'}}).encode())
        db.execute('UPDATE scan SET evidence_gz=? WHERE run_id=2', (blob,))
        db.execute("INSERT INTO scan VALUES (99,2,'samsung_shop','OK',12000,'2026-11-04',?)", (blob,))
        db.execute("INSERT INTO detection VALUES (99,'insider','DETECTED','network,runtime','[]')")
    out = tmp_path/'out'
    out.mkdir()
    baseline = out/'baseline.json'
    baseline.write_text(json.dumps({'day': '2026-11-03'}))
    (out/'review-2026-11-04.json').write_text(json.dumps({'day': '2026-11-04'}))
    monkeypatch.setattr(followup.review_rollout, 'run', lambda *a: {'state': 'already_reviewed'})
    followup.run(tmp_path, '2026-11-04', out, tmp_path/'backups', baseline, expected_version='0.2.8')
    result = json.loads((out/'comparison-2026-11-04.json').read_text())
    assert result['expected_collector_version'] == '0.2.8'
    assert not result['samsung_insider_confirmed']
    assert not any(f.startswith('collector_version_unexpected') for f in result['followup_findings'])
