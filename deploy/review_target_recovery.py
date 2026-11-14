"""Read-only scheduled follow-up for an explicit list of corrected target URLs."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3

from deploy import review_rollout
from radar import workspace


def focused_scans(database, day, targets):
    findings = []
    results = []
    with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True, timeout=2) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        for target in targets:
            scans = [dict(row) for row in db.execute('''
                SELECT s.id,s.run_id,s.url,s.final_url,s.status,s.duration_ms,s.started_at,
                       s.error,s.block_marker,
                       EXISTS(SELECT 1 FROM job j WHERE j.scan_id=s.id AND j.run_id=s.run_id
                              AND j.target_id=s.target_id AND j.url=s.url AND j.state='done') AS valid_job
                FROM scan s JOIN run r ON r.id=s.run_id
                WHERE r.note=? AND s.target_id=? ORDER BY s.id''',
                ('scheduled ' + day, target['id']))]
            expected = set(target['urls'])
            matched = [s for s in scans if s['url'] in expected and s['valid_job']]
            missing = sorted(expected - {s['url'] for s in matched})
            failures = [s['id'] for s in matched if s['status'] != 'OK']
            unexpected = [s['id'] for s in scans if s['url'] not in expected or not s['valid_job']]
            healthy = bool(expected) and not (missing or failures or unexpected)
            if not healthy:
                findings.append('target_recovery_not_confirmed:' + target['id'])
            results.append({**target, 'scans': scans, 'missing_urls': missing,
                            'failed_scan_ids': failures, 'unexpected_scan_ids': unexpected,
                            'healthy': healthy})
    return {'day': day, 'targets': results, 'expected_targets': len(targets),
            'healthy_targets': sum(t['healthy'] for t in results),
            'all_recovered': bool(targets) and not findings, 'findings': findings}


def run(home, day, output, backups, manifest):
    # Delegate date gating, bounded HTTP samples, coverage, load and backup checks.
    state = review_rollout.run(home, day, output, backups)
    if state['state'] not in ('reviewed', 'already_reviewed'):
        return state
    destination = output / f'target-recovery-{day}.json'
    if destination.exists():
        return {'state': 'already_checked', 'report': str(destination)}
    targets = json.loads(manifest.read_text())
    if not targets or len({t['id'] for t in targets}) != len(targets) or any(not t['urls'] for t in targets):
        raise ValueError('Manifest requires unique targets with expected URLs')
    report = focused_scans(home / 'data/radar.db', day, targets)
    report['checked_at'] = datetime.now(review_rollout.ZONE).isoformat()
    report['batch_report'] = str(output / f'review-{day}.json')
    report['interpretation'] = ('Only scheduled runs on the requested KST date count. '
                                'Manual verification does not confirm scheduled recovery. '
                                'BLOCKED, PARTIAL and robots refusals are not successful observations.')
    review_rollout.write_json(destination, report)
    return {'state': 'checked', 'report': str(destination),
            'healthy_targets': report['healthy_targets'], 'findings': report['findings']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--day', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--backups', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(workspace.home(), args.day, args.output, args.backups, args.manifest), ensure_ascii=False))
