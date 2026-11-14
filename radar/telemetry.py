"""Best-effort, read-only Linux load samples for a scheduled scan's lifetime."""
from __future__ import annotations

import argparse
from contextlib import suppress
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import threading
import re


def container_group(root: Path = Path('/sys/fs/cgroup'),
                    membership: Path = Path('/proc/self/cgroup')) -> Path | None:
    """Resolve cgroup v2 both with and without a private cgroup namespace."""
    try:
        relative = next(line[3:] for line in membership.read_text().splitlines()
                        if line.startswith('0::'))
        candidate = root / relative.lstrip('/')
        if candidate.is_relative_to(root) and '..' not in candidate.parts and (candidate/'cpu.stat').is_file():
            return candidate
    except (OSError, StopIteration):
        pass
    return None


def service_groups() -> dict[str, Path]:
    if os.environ.get('RADAR_CONTAINER') == '1':
        path = container_group()
        return {os.environ.get('RADAR_TELEMETRY_ROLE', 'container'): path} if path else {}
    groups = {}
    for unit in ('radar-scan', 'radar-worker', 'radar-dashboard'):
        try:
            result = subprocess.run(
                ['systemctl', '--user', 'show', unit, '-p', 'ControlGroup', '--value'],
                capture_output=True, text=True, timeout=2, check=True)
            group = result.stdout.strip()
            if group.startswith('/'):
                groups[unit] = Path('/sys/fs/cgroup') / group.lstrip('/')
        except (OSError, subprocess.SubprocessError):
            pass
    return groups


def snapshot(home: Path, groups: dict[str, Path]) -> dict:
    row = {'at': datetime.now(UTC).isoformat(), 'cpu_count': os.cpu_count(),
           'scope': 'container' if os.environ.get('RADAR_CONTAINER') == '1' else 'host'}
    # Host-wide /proc counters are misleading and unnecessarily large in a
    # container. Its cgroup below provides scoped CPU, memory, I/O and pressure.
    proc_files = () if row['scope'] == 'container' else (
        'stat', 'loadavg', 'meminfo', 'diskstats',
        'pressure/cpu', 'pressure/memory', 'pressure/io')
    for name in proc_files:
        try:
            row[name] = (Path('/proc') / name).read_text()
        except OSError as exc:
            row[name] = {'error': type(exc).__name__}
    row['services'] = {}
    for unit, path in groups.items():
        values = {}
        for name in ('cpu.stat', 'memory.current', 'memory.peak', 'memory.events',
                     'memory.swap.current', 'io.stat', 'pids.current', 'cpu.max',
                     'memory.max', 'pids.max', 'cpuset.cpus.effective',
                     'cpu.pressure', 'memory.pressure', 'io.pressure'):
            with suppress(OSError):
                values[name] = (path / name).read_text().strip()
        row['services'][unit] = values
    if not groups:
        row['cgroup_error'] = 'unavailable'
    try:
        conn = sqlite3.connect((home / 'data/radar.db').resolve().as_uri() + '?mode=ro',
                               uri=True, timeout=0.1)
        try:
            row['jobs'] = dict(conn.execute(
                "SELECT state,count(*) FROM job WHERE state IN ('pending','claimed') GROUP BY state"))
        finally:
            conn.close()
    except sqlite3.Error as exc:
        row['jobs_error'] = type(exc).__name__
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument('--output', type=Path)
    output.add_argument('--output-dir', type=Path)
    parser.add_argument('--label', default='container')
    parser.add_argument('--parent', type=int, required=True)
    parser.add_argument('--interval', type=float, default=5)
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error('interval must be positive')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}', args.label):
        parser.error('invalid label')
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    groups = service_groups()
    directory = args.output_dir or args.output.parent
    directory.mkdir(parents=True, exist_ok=True)
    # Unique per process lifetime; daily rotation bounds always-on service logs.
    identity = datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f') + '-' + str(os.getpid())
    stream, current = None, None
    try:
        while True:
            path = args.output or directory / f'load-service-{args.label}-{datetime.now(UTC):%Y%m%d}-{identity}.jsonl'
            if path != current:
                if stream:
                    stream.close()
                stream, current = path.open('a'), path
                if args.output_dir:
                    cutoff = (datetime.now(UTC) - timedelta(days=30)).timestamp()
                    for old in directory.glob(f'load-service-{args.label}-*.jsonl'):
                        with suppress(OSError):
                            if old.stat().st_mtime < cutoff:
                                old.unlink()
            row = snapshot(args.home, groups)
            row['event'] = 'stop' if stopped.is_set() else 'sample'
            stream.write(json.dumps(row) + '\n')
            stream.flush()
            if stopped.is_set():
                break
            try:
                os.kill(args.parent, 0)
            except ProcessLookupError:
                break
            stopped.wait(args.interval)
    finally:
        if stream:
            stream.close()


if __name__ == '__main__':
    main()
