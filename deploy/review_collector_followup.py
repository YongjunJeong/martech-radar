"""Read-only scheduled collector follow-up, including named targets and baseline."""
import argparse
from datetime import datetime, timedelta
import gzip
import json
from pathlib import Path
import sqlite3
import zlib

from deploy import review_rollout
from radar import workspace

FOCUS = ('sbi_savings', 'ok_savings', 'welcome_savings', 'lgtwins_ticket', 'samsung_shop')


def focused_scans(database, day):
    result = {target: [] for target in FOCUS}
    with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True, timeout=2) as db:
        db.row_factory = sqlite3.Row
        for row in db.execute('''SELECT s.id,s.target_id,s.status,s.duration_ms,s.started_at,s.evidence_gz,
                d.verdict,d.direct_layers,d.matches_json
            FROM scan s JOIN run r ON r.id=s.run_id
            LEFT JOIN detection d ON d.scan_id=s.id AND d.fingerprint_id='insider'
            WHERE r.note=? AND s.target_id IN (?,?,?,?,?) ORDER BY s.id''', ('scheduled ' + day, *FOCUS)):
            metadata = {}
            if row['evidence_gz']:
                try:
                    metadata = json.loads(gzip.decompress(row['evidence_gz'])).get('scan', {})
                    if not isinstance(metadata, dict):
                        metadata = {}
                except (OSError, ValueError, AttributeError, EOFError, zlib.error):
                    pass
            entry = {key: row[key] for key in ('id', 'status', 'duration_ms', 'started_at')}
            entry.update({key: metadata.get(key) for key in
                          ('collector_version', 'stop_reason', 'document_navigations', 'observation', 'timings_ms')})
            if row['target_id'] == 'samsung_shop':
                entry['insider'] = {
                    'judged': row['status'] == 'OK',
                    'verdict': (row['verdict'] or 'NOT_DETECTED') if row['status'] == 'OK' else 'UNJUDGED',
                    'direct_layers': row['direct_layers'],
                    'matches': json.loads(row['matches_json'] or '[]'),
                }
            result[row['target_id']].append(entry)
    return result


def batch_summary(report):
    services = {}
    for load in report.get('load', []):
        for name, metrics in load.get('services', {}).items():
            item = services.setdefault(name, {'memory_peak_bytes': 0, 'cpu_peak_cores': 0, 'oom_kills': 0})
            for key in ('memory_peak_bytes', 'cpu_peak_cores'):
                item[key] = max(item[key], metrics.get(key, 0))
            item['oom_kills'] += metrics.get('oom_kills', 0)
    jobs = report.get('recorded_jobs')
    errors = report.get('observation_errors')
    return {
        'day': report['day'], 'complete': report.get('complete'),
        'expected_jobs': report.get('expected_jobs'), 'recorded_jobs': jobs,
        'missing_expected_urls': report.get('missing_expected_urls'),
        'observation_errors': errors,
        'observation_error_rate': errors / jobs if jobs and errors is not None else None,
        'statuses': report.get('statuses'),
        'runs': report.get('runs'), 'services': services or None,
        'http': report.get('http'), 'findings': report.get('findings'),
    }


def run(home, day, output, backups, baseline_path, expected_version='0.2.7', include_policy=False):
    state = review_rollout.run(home, day, output, backups)
    if state['state'] not in ('reviewed', 'already_reviewed', 'incomplete_at_deadline'):
        return state
    if include_policy:
        from deploy import review_policy_references
        policy_path = output / f'policy-review-{day}.json'
        if not policy_path.exists():
            review_rollout.write_json(policy_path, review_policy_references.review(home, day))
    if state['state'] == 'incomplete_at_deadline':
        return {**state, **({'policy_review': str(policy_path)} if include_policy else {})}
    destination = output / f'comparison-{day}.json'
    if destination.exists():
        return {'state': 'already_compared', 'report': str(destination)}
    current = json.loads((output / f'review-{day}.json').read_text())
    previous = json.loads(baseline_path.read_text())
    expected_day = (datetime.fromisoformat(day) - timedelta(days=1)).date().isoformat()
    if previous['day'] != expected_day:
        raise ValueError('Baseline must be the previous calendar day')
    before = focused_scans(home / 'data/radar.db', previous['day'])
    after = focused_scans(home / 'data/radar.db', day)
    findings = []
    for target in FOCUS:
        if not after[target]:
            findings.append('focus_scan_missing:' + target)
        for row in after[target]:
            if row['collector_version'] != expected_version:
                findings.append('collector_version_unexpected:' + target)
            if target != 'samsung_shop' and (row['duration_ms'] is None or row['duration_ms'] >= 85000
                                             or row['stop_reason'] == 'hard_timeout'):
                findings.append('long_wait_unresolved:' + target)
    samsung = after['samsung_shop']
    confirmed = bool(samsung) and all(
        row['insider']['judged'] and row['insider']['verdict'] == 'DETECTED'
        and row['insider']['direct_layers'] for row in samsung)
    if not confirmed:
        findings.append('samsung_insider_not_confirmed')
    report = {'expected_collector_version': expected_version,
              'before': batch_summary(previous), 'after': batch_summary(current),
              'focus': {target: {'before': before[target], 'after': after[target]} for target in FOCUS},
              'samsung_insider_confirmed': confirmed, 'followup_findings': sorted(set(findings)),
              'interpretation': 'Faster BLOCKED/PARTIAL is bounded failure, not restored observation. '
                                'UNJUDGED is not absence. Compare batch coverage and error rate alongside counts. '
                                'Load is sampled; missing samples are unknown. Different dates are not a controlled benchmark.'}
    if include_policy:
        policy = json.loads(policy_path.read_text())
        report['policy_review'] = str(policy_path)
        report['followup_findings'] = sorted(set(report['followup_findings'] + policy['findings']))
    review_rollout.write_json(destination, report)
    return {'state': 'compared', 'report': str(destination), 'findings': report['followup_findings']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--day', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--backups', type=Path, required=True)
    parser.add_argument('--baseline-report', type=Path, required=True)
    parser.add_argument('--expected-version', default='0.2.7')
    parser.add_argument('--include-policy', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(workspace.home(), args.day, args.output, args.backups,
                         args.baseline_report, args.expected_version, args.include_policy), ensure_ascii=False))
