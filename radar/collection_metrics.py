"""Read-only per-stage collection report, streamed once after a scheduled batch."""
import argparse
from collections import Counter, defaultdict
import gzip
import json
import math
from pathlib import Path
import sqlite3
import statistics
import zlib


def distribution(values):
    values = sorted(values)
    return {'count': len(values), 'sum_ms': round(sum(values), 2),
            'median_ms': round(statistics.median(values), 2) if values else None,
            'p95_ms': round(values[math.ceil(len(values) * .95) - 1], 2) if values else None,
            'max_ms': round(values[-1], 2) if values else None}


def summarize(database, day):
    by_status, stages, versions, reasons = defaultdict(list), defaultdict(list), Counter(), Counter()
    scans = measured = invalid = 0
    slowest = []
    with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True, timeout=2) as db:
        db.execute('PRAGMA query_only=ON')
        # Do not decompress all historical evidence or retain a batch of blobs.
        for scan_id, target, status, duration, blob in db.execute('''
            SELECT s.id,s.target_id,s.status,s.duration_ms,s.evidence_gz
            FROM scan s JOIN run r ON r.id=s.run_id WHERE r.note=?''', ('scheduled ' + day,)):
            scans += 1
            by_status[status].append(duration or 0)
            metadata = {}
            if blob:
                try:
                    metadata = json.loads(gzip.decompress(blob)).get('scan', {})
                    if not isinstance(metadata, dict):
                        raise ValueError('invalid scan metadata')
                except (OSError, ValueError, AttributeError, TypeError, EOFError, zlib.error):
                    invalid += 1
                    metadata = {}
            version = metadata.get('collector_version', 'unknown')
            versions[str(version)] += 1
            timing = metadata.get('timings_ms')
            if isinstance(timing, dict) and timing and all(
                    isinstance(k, str) and type(v) in (float, int) and math.isfinite(v) and v >= 0
                    for k, v in timing.items()):
                measured += 1
                for stage, ms in timing.items():
                    stages[stage].append(ms)
            else:
                timing = None
            reason = metadata.get('stop_reason')
            if isinstance(reason, str):
                reasons[reason] += 1
            slowest.append({'scan_id': scan_id, 'target_id': target, 'status': status,
                            'duration_ms': duration, 'collector_version': version,
                            'stop_reason': reason, 'timings_ms': timing})
            slowest.sort(key=lambda row: row['duration_ms'] or 0, reverse=True)
            del slowest[10:]
    return {'day': day, 'scans': scans, 'measured_scans': measured,
            'unmeasured_scans': scans - measured, 'invalid_evidence': invalid,
            'collector_versions': dict(versions), 'stop_reasons': dict(reasons),
            'by_status': {key: distribution(values) for key, values in sorted(by_status.items())},
            'stages': {key: distribution(values) for key, values in sorted(stages.items())},
            'slowest': slowest,
            'timing_scope': 'Per-scan wall time; sums overlap across concurrent scans. Missing timings are unknown, not zero.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--day', required=True)
    args = parser.parse_args()
    print(json.dumps(summarize(args.database, args.day), ensure_ascii=False, indent=2))
