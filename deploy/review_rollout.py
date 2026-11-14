"""Bounded, read-only rollout checks. Never collect, restart, or send messages."""

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import sqlite3
import statistics
import time
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from radar import collection_metrics, config, operations, recovery, security, workspace

ZONE = ZoneInfo('Asia/Seoul')
PATHS = ('/', '/?solution_type=advertising&solution_type=engagement&observation=missing',
         '/companies/samsung_shop')


def queue_state(home, day):
    with sqlite3.connect((home / 'data/radar.db').as_uri() + '?mode=ro', uri=True, timeout=2) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        runs = [dict(r) for r in db.execute(
            'SELECT id,started_at,finished_at FROM run WHERE note=?', ('scheduled ' + day,))]
        claimed = db.execute('''SELECT COUNT(*) FROM job j JOIN run r ON r.id=j.run_id
            WHERE r.note=? AND j.state='claimed' ''', ('scheduled ' + day,)).fetchone()[0]
    return runs, claimed


def http_sample(home, day):
    _, claimed_before = queue_state(home, day)
    cookie = security.COOKIE_NAME + '=' + security.session_value(config.load(home / 'radar.toml').server.token)
    sample = {'at': datetime.now(ZONE).isoformat(), 'http': {}}
    for path in PATHS:
        started = time.perf_counter()
        try:
            url = 'http://127.0.0.1:8848' + path
            with urlopen(Request(url, headers={'Cookie': cookie}), timeout=5) as response:
                body = response.read()
                if response.status != 200 or response.url != url or '기업'.encode() not in body:
                    raise ValueError('Unexpected response or authentication redirect')
            sample['http'][path] = {'ms': round((time.perf_counter() - started) * 1000, 2)}
        except Exception as exc:
            sample['http'][path] = {'error': type(exc).__name__}
    runs, claimed_after = queue_state(home, day)
    sample['claimed_before'] = claimed_before
    sample['claimed_after'] = claimed_after
    sample['phase'] = ('during' if claimed_before and claimed_after else
                       'after' if runs and all(r['finished_at'] for r in runs) else 'waiting')
    return sample


def summarize_http(samples):
    result = {}
    for phase in ('waiting', 'during', 'after'):
        result[phase] = {}
        for path in PATHS:
            rows = [s['http'][path] for s in samples if s['phase'] == phase]
            values = sorted(r['ms'] for r in rows if 'ms' in r)
            result[phase][path] = {
                'samples': len(rows), 'errors': sum('error' in r for r in rows),
                'median_ms': round(statistics.median(values), 2) if values else None,
                'p95_ms': values[math.ceil(len(values) * .95) - 1] if values else None,
                'max_ms': max(values) if values else None,
            }
    return result


def check_backup(destination, day, runs):
    # Reuse the verified format. Hash only the latest backup, after collection.
    snapshots = sorted(p for p in destination.glob('radar-*') if p.is_dir() and not p.is_symlink())
    result = {'snapshots': len(snapshots), 'findings': []}
    if not snapshots:
        result['findings'].append('backup_missing')
        return result
    result['total_bytes'] = sum(p.stat().st_size for folder in snapshots
                                for p in folder.rglob('*') if p.is_file())
    latest = snapshots[-1]
    result['latest'] = latest.name
    try:
        manifest = recovery.verify(latest)
        created = datetime.fromisoformat(manifest['created_at'])
        result['verified'] = True
        result['created_at'] = created.isoformat()
        result['database'] = manifest['database']
        if created.astimezone(ZONE).date().isoformat() != day:
            result['findings'].append('fresh_backup_missing')
        if runs and created > min(datetime.fromisoformat(r['started_at']) for r in runs):
            result['findings'].append('backup_not_before_collection')
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        result['verified'] = False
        result['findings'].append('backup_verification_failed:' + type(exc).__name__)
    if len(snapshots) > 3 or result['total_bytes'] > 80 * 1024**3:
        result['findings'].append('backup_retention_exceeded')
    # A first rollout may not yet have three snapshots; report the actual count.
    result['full_retention_set_present'] = len(snapshots) == 3
    return result


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def run(home, day, output, backups):
    now = datetime.now(ZONE)
    # Future probes never touch the DB or issue HTTP requests.
    if now.date().isoformat() < day:
        return {'state': 'waiting_for_date', 'day': day}
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    final = output / f'review-{day}.json'
    if final.exists():
        return {'state': 'already_reviewed', 'report': str(final)}
    samples_path = output / f'http-{day}.jsonl'
    sample = http_sample(home, day)
    with samples_path.open('a') as stream:
        stream.write(json.dumps(sample) + '\n')
    runs, _ = queue_state(home, day)
    ended = bool(runs) and all(r['finished_at'] for r in runs)
    deadline = now.date().isoformat() > day or now.hour >= 15
    if not ended and not deadline:
        return {'state': 'sampling', 'phase': sample['phase']}
    samples = [json.loads(line) for line in samples_path.read_text().splitlines() if line.strip()]
    if ended and not deadline and sum(s['phase'] == 'after' for s in samples) < 3:
        return {'state': 'sampling_after_collection', 'phase': sample['phase']}
    report = operations.review(home, day)
    if ended:
        try:
            report['collection_performance'] = collection_metrics.summarize(home / 'data/radar.db', day)
        except (OSError, ValueError, sqlite3.Error) as exc:
            # Optional profiling must not suppress coverage, backup or HTTP results.
            report['collection_performance'] = {'error': type(exc).__name__}
            report['findings'].append('collection_timing_report_unavailable')
    report['http'] = summarize_http(samples)
    report['backup'] = check_backup(backups, day, runs)
    report['findings'].extend(report['backup']['findings'])
    for phase in ('during', 'after'):
        for path, value in report['http'][phase].items():
            if not value['samples']:
                report['findings'].append('http_samples_missing:' + phase + ':' + path)
            elif value['errors']:
                report['findings'].append('http_request_failed:' + phase + ':' + path)
            if value['p95_ms'] is not None and value['p95_ms'] > 500:
                report['findings'].append('http_p95_over_500ms:' + phase + ':' + path)
    report['findings'] = sorted(set(report['findings']))
    report['healthy'] = report['complete'] and not report['findings']
    # Do not freeze an incomplete report while a run is still working.
    dest = final if ended else output / f'pending-{day}.json'
    write_json(dest, report)
    return {'state': 'reviewed' if ended else 'incomplete_at_deadline',
            'healthy': report['healthy'], 'report': str(dest), 'findings': report['findings']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--day', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--backups', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(workspace.home(), args.day, args.output, args.backups), ensure_ascii=False))
