"""Read-only policy automation health, kept separate from document acquisition success."""

from collections import Counter
from datetime import UTC, datetime, timedelta
import json
import re
import sqlite3
import subprocess
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Asia/Seoul")


def summarize(rows, now=None):
    batches, failed, parse_errors = [], [], 0
    for row in rows:
        message = row.get("MESSAGE", "")
        if not isinstance(message, str):
            continue
        if re.search(
            r"Failed with result|Main process exited|timed out|oom-kill|Failed to start",
            message,
            re.I,
        ):
            failed.append(message[:240])
        if not message.startswith("{"):
            continue
        try:
            batch = json.loads(message)
            if "state" not in batch or "results" not in batch:
                continue
            batch["logged_at"] = datetime.fromtimestamp(
                int(row["__REALTIME_TIMESTAMP"]) / 1e6, UTC
            ).isoformat()
            batches.append(batch)
        except (ValueError, TypeError, KeyError):
            parse_errors += 1
    findings = []
    if not batches:
        findings.append("policy_execution_records_missing")
    if failed:
        findings.append("policy_service_failure_logged")
    if parse_errors:
        findings.append("policy_log_parse_errors")
    states, statuses, issues = Counter(), Counter(), Counter()
    cpu, memory, durations = [], [], []
    missing_load = 0
    for batch in batches:
        states[batch["state"]] += 1
        # A guarded no-op outside daytime is acceptable; real visits are not.
        if (
            batch["results"]
            and not 8 <= datetime.fromisoformat(batch["logged_at"]).astimezone(ZONE).hour < 23
        ):
            findings.append("policy_visits_outside_daytime")
        if len(batch["results"]) > 5:
            findings.append("policy_batch_limit_exceeded")
        load = batch.get("process_load") or {}
        if "cpu_seconds" in load and "peak_rss_bytes" in load:
            cpu.append(load["cpu_seconds"])
            memory.append(load["peak_rss_bytes"])
        else:
            missing_load += 1
        if batch.get("duration_ms") is not None:
            durations.append(batch["duration_ms"])
        for result in batch["results"]:
            statuses[result["status"]] += 1
            if result['status'] == 'ERROR':
                findings.append('policy_collector_errors')
            issues.update(result.get("issues") or [])
            if (result.get("request_count") or 0) > 8:
                findings.append("policy_request_limit_exceeded")
    # First installation tests can predate resource logging; expose missing coverage.
    if missing_load:
        findings.append("policy_load_samples_missing")
    if memory and max(memory) >= 192 * 1024**2:
        findings.append("policy_memory_budget_reached")
    if durations and max(durations) > 180000:
        findings.append("policy_runtime_budget_exceeded")
    by_day = {}
    for batch in batches:
        at = datetime.fromisoformat(batch["logged_at"]).astimezone(ZONE)
        if 8 <= at.hour < 23:
            by_day.setdefault(at.date(), []).append(at)
    for times in by_day.values():
        times.sort()
        if any((b - a).total_seconds() > 2400 for a, b in zip(times, times[1:], strict=False)):
            findings.append("policy_daytime_execution_gap")
        if (
            now
            and now >= times[-1].replace(hour=23, minute=0, second=0)
            and times[-1] < times[-1].replace(hour=22, minute=40, second=0)
        ):
            findings.append("policy_last_daytime_execution_missing")
    return {
        "executions": len(batches),
        "execution_states": dict(states),
        "automatic_document_statuses": dict(statuses),
        "automatic_document_issues": dict(issues),
        "attempted_targets": sum(statuses.values()),
        "last_execution_at": batches[-1]["logged_at"] if batches else None,
        "process_cpu_seconds_total": round(sum(cpu), 3) if cpu else None,
        "process_peak_rss_bytes": max(memory) if memory else None,
        "max_batch_duration_ms": max(durations) if durations else None,
        "missing_load_samples": missing_load,
        "service_failures": failed,
        "parse_errors": parse_errors,
        "findings": sorted(set(findings)),
    }


def review(home, day, now=None):
    now = now or datetime.now(ZONE)
    if now.date().isoformat() < day:
        return {"state": "waiting_for_date", "day": day}
    start = datetime.fromisoformat(day).replace(tzinfo=ZONE) - timedelta(days=1)
    end = min(now, datetime.fromisoformat(day).replace(tzinfo=ZONE) + timedelta(days=1))
    report = {
        "day": day,
        "checked_at": now.isoformat(),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "findings": [],
    }
    try:
        reply = subprocess.run(
            [
                "journalctl",
                "--user",
                "-u",
                "radar-policy-reference.service",
                "--since",
                start.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
                "--until",
                end.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
                "-n",
                "2000",
                "--no-pager",
                "-o",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        rows = [json.loads(line) for line in reply.stdout.splitlines() if line.strip()]
        report["automation"] = summarize(rows, now)
        report["findings"].extend(report["automation"]["findings"])
        if len(rows) >= 2000:
            report["findings"].append("policy_journal_may_be_truncated")
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        report["findings"].append("policy_journal_unavailable")
    for unit in ("radar-policy-reference.timer", "radar-policy-reference.service"):
        try:
            reply = subprocess.run(
                [
                    "systemctl",
                    "--user",
                    "show",
                    unit,
                    "-p",
                    "ActiveState",
                    "-p",
                    "UnitFileState",
                    "-p",
                    "Result",
                    "-p",
                    "MemoryMax",
                    "-p",
                    "CPUQuotaPerSecUSec",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            values = dict(line.split("=", 1) for line in reply.stdout.splitlines() if "=" in line)
            report[unit] = values
            if unit.endswith(".timer") and (
                values.get("ActiveState") != "active" or values.get("UnitFileState") != "enabled"
            ):
                report["findings"].append("policy_timer_not_enabled_active")
            if unit.endswith(".service") and values.get("Result") not in (None, "success"):
                report["findings"].append("policy_service_last_execution_failed")
        except (OSError, subprocess.SubprocessError):
            report["findings"].append("policy_unit_state_unavailable")
    with sqlite3.connect(
        (home / "data/radar.db").resolve().as_uri() + "?mode=ro", uri=True, timeout=2
    ) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        active = db.execute("SELECT count(*) FROM target WHERE enabled=1").fetchone()[0]
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='policy_reference'").fetchone()
        statuses, issues, invalid = Counter(), Counter(), 0
        if exists:
            for (payload,) in db.execute(
                "SELECT p.payload_json FROM policy_reference p JOIN target t ON t.id=p.target_id WHERE t.enabled=1"
            ):
                try:
                    value = json.loads(payload)
                    statuses[value["status"]] += 1
                    issues.update(value.get("issues") or [])
                except (ValueError, TypeError, KeyError):
                    invalid += 1
        else:
            report["findings"].append("policy_reference_table_missing")
        report["stored_references"] = {
            "active_targets": active,
            "latest_statuses": dict(statuses),
            "latest_issues": dict(issues),
            "invalid_records": invalid,
            "not_yet_attempted": active - sum(statuses.values()) - invalid,
        }
        if invalid:
            report["findings"].append("policy_reference_invalid_records")
    report["findings"] = sorted(set(report["findings"]))
    report["interpretation"] = (
        "Policy automation runs only during 08:00–22:59 KST; no overnight visits are expected. "
        "Review the previous daytime executions. Successful execution does not mean a readable document. "
        "UNAVAILABLE/PARTIAL and not-yet-attempted are not evidence of solution absence. "
        "Process RSS is not whole-host memory; missing metrics are unknown. No target expansion is performed."
    )
    return report
