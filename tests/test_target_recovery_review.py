from datetime import datetime, timedelta
import sqlite3

from deploy import review_target_recovery as review


def test_manual_success_never_counts_as_scheduled_recovery(tmp_path):
    database = tmp_path / 'radar.db'
    with sqlite3.connect(database) as db:
        db.executescript('''
            CREATE TABLE run(id INTEGER, note TEXT);
            CREATE TABLE scan(id INTEGER,run_id INTEGER,target_id TEXT,url TEXT,final_url TEXT,
                status TEXT,duration_ms INTEGER,started_at TEXT,error TEXT,block_marker TEXT);
            CREATE TABLE job(scan_id INTEGER,run_id INTEGER,target_id TEXT,url TEXT,state TEXT);
            INSERT INTO run VALUES(1,'manual verification'),(2,'scheduled 2026-11-07');
        ''')
        entries = [
            (1, 1, 'manual_only', 'https://a.example/', 'OK', 'done'),
            (2, 2, 'wrong_url', 'https://old.example/', 'OK', 'done'),
            (3, 2, 'partial', 'https://a.example/', 'PARTIAL', 'done'),
            (4, 2, 'healthy', 'https://a.example/', 'OK', 'done'),
            (5, 2, 'invalid_job', 'https://a.example/', 'OK', 'failed'),
            (6, 2, 'robots', 'https://a.example/', 'SKIPPED_BY_ROBOTS', 'done'),
            (7, 2, 'mixed', 'https://a.example/', 'BLOCKED', 'done'),
            (8, 2, 'mixed', 'https://a.example/', 'OK', 'done'),
        ]
        for ident, run, target, url, status, state in entries:
            db.execute('INSERT INTO scan VALUES(?,?,?,?,?,?,10,?,NULL,NULL)',
                       (ident, run, target, url, url, status, '2026-11-06T18:30:00+00:00'))
            db.execute('INSERT INTO job VALUES(?,?,?,?,?)', (ident, run, target, url, state))
    targets = [{'id': target, 'urls': ['https://a.example/']} for target in
               ('manual_only', 'wrong_url', 'partial', 'healthy', 'invalid_job', 'robots', 'mixed')]
    result = review.focused_scans(database, '2026-11-07', targets)
    assert result['healthy_targets'] == 1 and not result['all_recovered']
    assert [t['id'] for t in result['targets'] if t['healthy']] == ['healthy']
    assert result['targets'][0]['scans'] == []
    assert result['targets'][1]['missing_urls'] == ['https://a.example/']
    assert result['targets'][-1]['failed_scan_ids'] == [7]


def test_future_review_never_opens_database_or_manifest(tmp_path, monkeypatch):
    def unexpected(*args):
        raise AssertionError('Future check accessed runtime')
    monkeypatch.setattr(review.review_rollout, 'http_sample', unexpected)
    monkeypatch.setattr(review, 'focused_scans', unexpected)
    day = (datetime.now(review.review_rollout.ZONE) + timedelta(days=1)).date().isoformat()
    result = review.run(tmp_path, day, tmp_path/'out', tmp_path/'backups', tmp_path/'absent.json')
    assert result['state'] == 'waiting_for_date'
    assert not (tmp_path/'out').exists()


def test_unfinished_scheduled_run_does_not_publish_success(tmp_path, monkeypatch):
    monkeypatch.setattr(review.review_rollout, 'run', lambda *a: {'state': 'sampling'})
    assert review.run(tmp_path, '2026-11-07', tmp_path/'out', tmp_path/'backups',
                      tmp_path/'absent.json') == {'state': 'sampling'}
