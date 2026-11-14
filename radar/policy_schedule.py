"""Small daytime HTTP-only policy batches; never run the browser or send messages."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import resource
import sqlite3
import time
from zoneinfo import ZoneInfo

from . import policy_references as policies, workspace
from .store import Store

ZONE = ZoneInfo('Asia/Seoul')
MAX_TARGETS = 5
BATCH_SECONDS = 120
REFRESH_DAYS = 7  # Failed automatic attempts also wait a week; no daily retry storm.


def host_busy():
    try:
        available = next(int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines()
                         if line.startswith('MemAvailable:'))
        return available < 4*1024**3 or os.getloadavg()[0] >= max(1, (os.cpu_count() or 1)*0.5)
    except (OSError, StopIteration, ValueError):
        return True  # A missing resource sample is not evidence of spare capacity.


def busy(store):
    if store.has_unfinished_jobs() or host_busy():
        return True
    for table, states in [('task', ('pending', 'running')), ('preparation_job', ('queued', 'running'))]:
        if store.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            placeholders = ','.join('?' for _ in states)
            if store.conn.execute(f'SELECT 1 FROM {table} WHERE state IN ({placeholders}) LIMIT 1', states).fetchone():
                return True
    return False


def due_targets(store, now, limit=MAX_TARGETS):
    """Unseen targets first, then indexed oldest attempts; only selected JSON is read."""
    policies.initialize(store)
    unseen = [r[0] for r in store.conn.execute('''SELECT t.id FROM target t
        LEFT JOIN policy_reference p ON p.target_id=t.id
        WHERE t.enabled=1 AND p.target_id IS NULL ORDER BY t.id LIMIT ?''', (limit,))]
    if len(unseen) == limit:
        return unseen
    cutoff = (now-timedelta(days=REFRESH_DAYS)).astimezone(UTC).isoformat()
    due = [r[0] for r in store.conn.execute('''SELECT p.target_id FROM policy_reference p
        JOIN target t ON t.id=p.target_id
        WHERE t.enabled=1 AND json_extract(p.payload_json,'$.checked_at')<=?
        ORDER BY json_extract(p.payload_json,'$.checked_at'),p.target_id LIMIT ?''', (cutoff, limit-len(unseen)))]
    return unseen+due


def run(home, now=None, limit=MAX_TARGETS):
    now = now or datetime.now(ZONE)
    if not 1 <= limit <= MAX_TARGETS:
        raise ValueError('Policy batch limit must be between 1 and 5')
    if not 8 <= now.astimezone(ZONE).hour < 23:
        return {'state': 'outside_daytime_window', 'results': []}
    # The lock is shared by every scheduled invocation, including manual service starts.
    lock_path = home/'data/policy-schedule.lock'
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'state': 'already_running', 'results': []}
        with Store(home/'data/radar.db') as store:
            if busy(store):
                return {'state': 'deferred_busy', 'results': []}
            started = time.monotonic()
            results, state = [], 'completed'
            for target_id in due_targets(store, now, limit):
                if time.monotonic()-started >= BATCH_SECONDS or busy(store):
                    state = 'deferred_busy_or_budget'
                    break
                previous = policies.read(store, target_id)
                policy_url = (previous or {}).get('source', {}).get('requested_policy_url')
                try:
                    result = policies.collect(store, target_id, policy_url,
                                              should_defer=lambda: busy(store) or time.monotonic()-started >= BATCH_SECONDS)
                except policies.PolicyDeferred:
                    state = 'deferred_primary_work'
                    break
                except sqlite3.Error:
                    raise  # Storage faults cannot safely be recorded as a per-target error.
                except Exception as exc:  # One malformed site must not starve the rest of the rotation.
                    result = policies.record_error(store, target_id, exc, policy_url)
                if result['status'] == 'DEFERRED_COLLECTION_ACTIVE':
                    state = 'deferred_primary_work'
                    break
                results.append({key: result.get(key) for key in
                                ('target_id', 'status', 'request_count', 'duration_ms', 'issues', 'error_type')})
            if state == 'completed' and any(r['status'] == 'ERROR' for r in results):
                state = 'completed_with_errors'
            return {'state': state, 'checked_at': now.isoformat(), 'results': results,
                    'duration_ms': round((time.monotonic()-started)*1000)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=workspace.home())
    parser.add_argument('--limit', type=int, default=MAX_TARGETS)
    args = parser.parse_args()
    workspace.set_home(str(args.home))
    result = run(args.home, limit=args.limit)
    usage = resource.getrusage(resource.RUSAGE_SELF)
    result['process_load'] = {'cpu_seconds': round(usage.ru_utime+usage.ru_stime, 3),
                              'peak_rss_bytes': usage.ru_maxrss*1024}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
