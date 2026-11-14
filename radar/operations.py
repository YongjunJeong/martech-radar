"""Portable, read-only scheduled-run and cgroup load review; never retries work."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
from zoneinfo import ZoneInfo

from . import config, workspace
from .evidence import STATUS_OK, STATUS_SKIPPED_BY_ROBOTS


def counters(text):
    return {key: int(value) for key, value in (line.split() for line in text.splitlines())}


def summarize_load(path, start, end):
    """Stream samples; CPU/OOM values are deltas within each process lifetime."""
    result = {
        "file": path.name,
        "samples": 0,
        "parse_errors": 0,
        "services": {},
        "max_claimed": 0,
        "jobs_errors": 0,
        "cgroup_errors": 0,
    }
    previous = {}
    with path.open() as stream:
        for line in stream:
            try:
                row = json.loads(line)
                at = datetime.fromisoformat(row["at"])
                if not start <= at < end:
                    continue
                result["samples"] += 1
                result.setdefault("first_at", row["at"])
                result["last_at"] = row["at"]
                result["last_event"] = row.get("event")
                result["max_claimed"] = max(
                    result["max_claimed"], row.get("jobs", {}).get("claimed", 0)
                )
                result["jobs_errors"] += "jobs_error" in row
                result["cgroup_errors"] += "cgroup_error" in row or not row.get("services")
                for name, values in row.get("services", {}).items():
                    cpu = counters(values["cpu.stat"])
                    events = counters(values["memory.events"])
                    if "usage_usec" not in cpu or "oom_kill" not in events:
                        raise ValueError("Required cgroup counters unavailable")
                    memory = int(values["memory.current"])
                    item = result["services"].setdefault(
                        name,
                        {
                            "memory_peak_bytes": 0,
                            "cpu_peak_cores": 0,
                            "cpu_quota_peak_percent": 0,
                            "memory_limit_peak_percent": 0,
                            "oom_kills": 0,
                            "throttled_usec": 0,
                            "counter_resets": 0,
                            "max_sample_gap_seconds": 0,
                        },
                    )
                    item["memory_peak_bytes"] = max(item["memory_peak_bytes"], memory)
                    limit = values.get("memory.max", "max")
                    if limit != "max" and int(limit) > 0:
                        item["memory_limit_peak_percent"] = max(
                            item["memory_limit_peak_percent"], memory / int(limit) * 100
                        )
                    if name in previous:
                        last_at, last_cpu, last_events = previous[name]
                        elapsed = (at - last_at).total_seconds()
                        item["max_sample_gap_seconds"] = max(
                            item["max_sample_gap_seconds"], elapsed
                        )
                        delta = cpu.get("usage_usec", 0) - last_cpu.get("usage_usec", 0)
                        if delta < 0 or events.get("oom_kill", 0) < last_events.get("oom_kill", 0):
                            item["counter_resets"] += 1
                        elif elapsed > 0:
                            cores = delta / elapsed / 1e6
                            item["cpu_peak_cores"] = max(item["cpu_peak_cores"], cores)
                            quota, period = values.get("cpu.max", "max 100000").split()
                            if quota != "max" and int(quota) > 0:
                                item["cpu_quota_peak_percent"] = max(
                                    item["cpu_quota_peak_percent"],
                                    cores * int(period) / int(quota) * 100,
                                )
                            item["oom_kills"] += events.get("oom_kill", 0) - last_events.get(
                                "oom_kill", 0
                            )
                            item["throttled_usec"] += max(
                                0, cpu.get("throttled_usec", 0) - last_cpu.get("throttled_usec", 0)
                            )
                    previous[name] = at, cpu, events
            except (ValueError, TypeError, KeyError, AttributeError):
                result["parse_errors"] += 1
    return result


def review(home: Path, day: str, timezone="Asia/Seoul", max_run_hours=10):
    start = datetime.combine(date.fromisoformat(day), time(), ZoneInfo(timezone)).astimezone(UTC)
    end = datetime.combine(
        date.fromisoformat(day) + timedelta(days=1), time(), ZoneInfo(timezone)
    ).astimezone(UTC)
    report = {
        "day": day,
        "timezone": timezone,
        "checked_at": datetime.now(UTC).isoformat(),
        "findings": [],
        "load": [],
    }
    report["concurrency_limit"] = config.load(home / "radar.toml").scan.concurrency
    uri = (home / "data/radar.db").resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=2) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")  # consistent read snapshot, no schema changes
        runs = [
            dict(r)
            for r in db.execute(
                "SELECT id,started_at,finished_at,target_count FROM run WHERE note=?",
                ("scheduled " + day,),
            )
        ]
        jobs = [
            dict(r)
            for r in db.execute(
                """SELECT j.id,j.run_id,j.target_id,j.state,j.scan_id,
            CASE WHEN s.id IS NOT NULL AND s.run_id=j.run_id AND s.target_id=j.target_id AND s.url=j.url THEN 1 ELSE 0 END AS valid_scan
            FROM job j JOIN run r ON r.id=j.run_id LEFT JOIN scan s ON s.id=j.scan_id WHERE r.note=?""",
                ("scheduled " + day,),
            )
        ]
        statuses = dict(
            db.execute(
                """SELECT s.status,COUNT(*) FROM scan s JOIN run r ON s.run_id=r.id
            WHERE r.note=? GROUP BY s.status""",
                ("scheduled " + day,),
            )
        )
        report.update(
            runs=runs,
            jobs=dict(Counter(j["state"] for j in jobs)),
            statuses=statuses,
            invalid_job_ids=[j["id"] for j in jobs if not j["valid_scan"]],
            active_jobs=dict(
                db.execute(
                    "SELECT state,COUNT(*) FROM job WHERE state IN ('pending','claimed') GROUP BY state"
                )
            ),
        )
        # Queue is the immutable expected URL list for this run. Current target
        # edits must not retrospectively change a completed run's denominator.
        report["expected_jobs"] = len(jobs)
        report["recorded_jobs"] = sum(j["valid_scan"] for j in jobs)
        report["plan_count_mismatch"] = [
            r["id"]
            for r in runs
            if r["target_count"] != len({j["target_id"] for j in jobs if j["run_id"] == r["id"]})
        ]
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        plans = list(db.execute('SELECT * FROM collection_plan WHERE note=?', ('scheduled ' + day,))) if 'collection_plan' in tables else []
        expected = set()
        deferred = set()
        for plan in plans:
            for item in json.loads(plan['items_json']):
                key = (item['target_id'], item['url'])
                if item['decision'] != 'not_due':
                    expected.add(key)
                if item['decision'] in ('budget_deferred', 'filtered'):
                    deferred.add(key)
        recorded = set(map(tuple, db.execute('''SELECT DISTINCT s.target_id,s.url FROM scan s
            JOIN job j ON j.scan_id=s.id AND j.run_id=s.run_id AND j.target_id=s.target_id AND j.url=s.url
            WHERE j.state='done' AND s.started_at>=? AND s.started_at<?''', (start.isoformat(), end.isoformat()))))
        report['coverage_verified'] = bool(plans) and all(
            r['id'] in {p['run_id'] for p in plans} for r in runs)
        report['expected_urls_before_budget'] = len(expected)
        report['budget_or_filter_deferred'] = len(deferred)
        report['missing_expected_urls'] = [dict(target_id=tid, url=url) for tid, url in sorted(expected - recorded)]
        report['coverage_complete'] = report['coverage_verified'] and not report['missing_expected_urls']
    report["queue_complete"] = (
        bool(runs or plans)
        and all(r["finished_at"] for r in runs)
        and not (
            report["invalid_job_ids"]
            or report["plan_count_mismatch"]
            or any(j["state"] != "done" for j in jobs)
        )
    )
    report['complete'] = report['queue_complete'] and report['coverage_complete']
    if not report['coverage_verified']:
        report['findings'].append('pre_queue_plan_missing')
    if report['missing_expected_urls']:
        report['findings'].append('expected_urls_not_collected')
    for run in runs:
        elapsed = ((datetime.fromisoformat(run['finished_at']) if run['finished_at'] else datetime.now(UTC))
                   - datetime.fromisoformat(run['started_at'])).total_seconds()
        run['duration_seconds'] = elapsed
        if elapsed > max_run_hours * 3600:
            report['findings'].append('collection_duration_exceeded:' + str(run['id']))
    report['storage'] = {
        'database_bytes': (home / 'data/radar.db').stat().st_size,
        'wal_bytes': (home / 'data/radar.db-wal').stat().st_size if (home / 'data/radar.db-wal').exists() else 0,
        'free_bytes': shutil.disk_usage(home).free,
    }
    if report['storage']['free_bytes'] < max(2 * 1024**3, report['storage']['database_bytes']):
        report['findings'].append('database_disk_headroom_low')
    if not report["complete"]:
        report["findings"].append("scheduled_collection_incomplete_or_missing")
    errors = sum(
        n for status, n in statuses.items() if status not in (STATUS_OK, STATUS_SKIPPED_BY_ROBOTS)
    )
    report["observation_errors"] = errors
    if errors:
        report["findings"].append("site_observation_errors")
    # Files are small per day/process; avoid parsing the entire 30-day archive.
    prefixes = {start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), day.replace("-", "")}
    for path in sorted((home / "data/logs").glob("load-*.jsonl")):
        if not any(prefix in path.name for prefix in prefixes):
            continue
        sample = summarize_load(path, start, end)
        if sample["samples"] or sample["parse_errors"]:
            report["load"].append(sample)
    if not report["load"]:
        report["findings"].append("load_records_missing")
    for run in runs:
        if not run["finished_at"]:
            continue
        began, ended = (datetime.fromisoformat(run[key]) for key in ("started_at", "finished_at"))
        covered = any(
            sample["samples"] >= 2
            and datetime.fromisoformat(sample["first_at"]) <= began + timedelta(seconds=15)
            and datetime.fromisoformat(sample["last_at"]) >= ended - timedelta(seconds=15)
            for sample in report["load"]
        )
        if not covered:
            report["findings"].append("run_load_coverage_missing:" + str(run["id"]))
    for sample in report["load"]:
        if sample["max_claimed"] > report["concurrency_limit"]:
            report["findings"].append("collection_concurrency_exceeded")
        if (
            sample["samples"] < 2
            or sample["parse_errors"]
            or sample["jobs_errors"]
            or sample["cgroup_errors"]
        ):
            report["findings"].append("load_records_incomplete:" + sample["file"])
        for name, values in sample["services"].items():
            for field, threshold in [
                ("oom_kills", 0),
                ("memory_limit_peak_percent", 90),
                ("cpu_quota_peak_percent", 95),
                ("max_sample_gap_seconds", 30),
                ("counter_resets", 0),
            ]:
                if values[field] > threshold:
                    report["findings"].append(field + ":" + name)
    report["findings"] = sorted(set(report["findings"]))
    report["healthy"] = report["complete"] and not report["findings"]
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=workspace.home())
    parser.add_argument("--day", required=True)
    parser.add_argument("--timezone", default="Asia/Seoul")
    parser.add_argument("--output", type=Path)
    parser.add_argument('--max-run-hours', type=float, default=10)
    args = parser.parse_args()
    try:
        if args.max_run_hours <= 0:
            raise ValueError('max-run-hours must be positive')
        report = review(args.home, args.day, args.timezone, args.max_run_hours)
    except (OSError, sqlite3.Error, ValueError) as exc:
        report = {"healthy": False, "complete": False, "error": type(exc).__name__}
    content = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', dir=args.output.parent,
                                         prefix='.' + args.output.name, delete=False) as stream:
            stream.write(content)
            temporary = Path(stream.name)
        try:
            temporary.replace(args.output)
        finally:
            temporary.unlink(missing_ok=True)
    print(content, end="")
    raise SystemExit(0 if report["healthy"] else 2)


if __name__ == "__main__":
    main()
