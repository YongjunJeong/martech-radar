import gzip
import json
import sqlite3

from radar.collection_metrics import summarize


def test_daily_metrics_report_distinguishes_old_missing_and_failed_scans(tmp_path):
    path = tmp_path/'radar.db'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE run(id INTEGER,note TEXT); CREATE TABLE scan(id INTEGER,run_id INTEGER,target_id TEXT,status TEXT,duration_ms INTEGER,evidence_gz BLOB);')
        db.executemany('INSERT INTO run VALUES (?,?)', [(1,'scheduled 2026-11-03'),(2,'scheduled 2026-11-02')])
        def blob(**scan):
            return gzip.compress(json.dumps({'scan': scan}).encode())
        db.executemany('INSERT INTO scan VALUES (?,?,?,?,?,?)', [
            (1,1,'normal','OK',1000,blob(collector_version='0.2.6',timings_ms={'navigation':100,'settle':800})),
            (2,1,'timeout','TIMEOUT',400,blob(collector_version='0.2.6',timings_ms={'navigation':350},stop_reason='navigation_uncommitted')),
            (3,1,'old','OK',900,blob(collector_version='0.2.5')),
            (4,1,'corrupt','OK',800,b'not gzip'),
            (5,2,'yesterday','OK',900,blob(collector_version='0.2.5')),
        ])
    report = summarize(path, '2026-11-03')
    assert report['scans'] == 4 and report['measured_scans'] == 2
    assert report['unmeasured_scans'] == 2 and report['invalid_evidence'] == 1
    assert report['by_status']['TIMEOUT']['sum_ms'] == 400
    assert report['stages']['navigation']['sum_ms'] == 450
    assert report['stages']['settle']['count'] == 1  # skipped stage is not a zero observation
    assert report['slowest'][0]['target_id'] == 'normal'
    assert report['stop_reasons'] == {'navigation_uncommitted': 1}
    assert summarize(path, '2026-11-04')['stages'] == {}
