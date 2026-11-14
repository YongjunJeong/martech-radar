import json
from datetime import UTC, datetime, timedelta
import sqlite3

import pytest

from radar import telemetry
from radar.operations import review, summarize_load
from radar.store import Store
from radar.batch import Job


def sample(at, usage=0, oom=4, memory=200, quota="100000 100000"):
    return {
        "at": at.isoformat(),
        "event": "sample",
        "jobs": {"claimed": 1},
        "services": {
            "worker": {
                "cpu.stat": f"usage_usec {usage}\nthrottled_usec 0",
                "memory.current": str(memory),
                "memory.max": "1000",
                "memory.events": f"oom_kill {oom}",
                "cpu.max": quota,
            }
        },
    }


def test_container_namespace_and_host_namespace(tmp_path):
    root = tmp_path / "cgroup"
    root.mkdir()
    membership = tmp_path / "membership"
    membership.write_text("0::/nested/worker\n")
    child = root / "nested/worker"
    child.mkdir(parents=True)
    (child / "cpu.stat").write_text("usage_usec 5")
    assert telemetry.container_group(root, membership) == child
    membership.write_text("0::/\n")
    (root / "cpu.stat").write_text("usage_usec 6")
    assert telemetry.container_group(root, membership) == root


def test_container_avoids_systemctl(monkeypatch, tmp_path):
    monkeypatch.setenv("RADAR_CONTAINER", "1")
    monkeypatch.setenv("RADAR_TELEMETRY_ROLE", "web")
    monkeypatch.setattr(telemetry, "container_group", lambda: tmp_path)
    monkeypatch.setattr(
        telemetry.subprocess, "run", lambda *a, **kw: pytest.fail("systemctl called")
    )
    assert telemetry.service_groups() == {"web": tmp_path}
    (tmp_path / "cpu.max").write_text("50000 100000")
    row = telemetry.snapshot(tmp_path, {"web": tmp_path})
    assert row["scope"] == "container"
    assert 'stat' not in row and 'diskstats' not in row
    assert row["services"]["web"]["cpu.max"] == "50000 100000"


def test_cpu_quota_oom_delta_and_corrupt_record(tmp_path):
    start = datetime(2026, 11, 1, tzinfo=UTC)
    path = tmp_path / "samples.jsonl"
    path.write_text(
        json.dumps(sample(start))
        + "\n"
        + json.dumps(
            sample(
                start + timedelta(seconds=5), usage=2500000, oom=5, memory=950, quota="50000 100000"
            )
        )
        + "\n{broken\n"
    )
    summary = summarize_load(path, start, start + timedelta(days=1))
    values = summary["services"]["worker"]
    assert values["cpu_peak_cores"] == 0.5
    assert values["cpu_quota_peak_percent"] == 100
    assert values["memory_limit_peak_percent"] == 95
    assert values["oom_kills"] == 1  # historical 4 OOMs are not charged to this run
    assert summary["parse_errors"] == 1


def make_run(home, state="done"):
    (home / "data/logs").mkdir(parents=True)
    with Store(home / "data/radar.db") as store:
        store.upsert_industry("retail", "Retail")
        store.upsert_target(
            id="demo", company="Demo", industry="retail", urls=["https://example.com"]
        )
        plan = store.save_collection_plan(dict(created_at='2026-11-01T01:00:00+00:00', cadence={},
            items=[dict(target_id='demo', url='https://example.com', decision='scheduled')]), 'scheduled 2026-11-01')
        run = store.queue_run(
            [Job("demo", "Demo", "https://example.com")], note="scheduled 2026-11-01", plan_id=plan
        )
        store.conn.execute(
            "UPDATE run SET started_at='2026-11-01T01:00:00+00:00', finished_at='2026-11-01T01:00:10+00:00' WHERE id=?",
            (run,),
        )
        if state == "done":
            scan = store.conn.execute(
                "INSERT INTO scan(run_id,target_id,url,status,started_at) VALUES (?,'demo','https://example.com','OK','2026-11-01T01:00:00+00:00')",
                (run,),
            ).lastrowid
            store.conn.execute("UPDATE job SET state='done', scan_id=?", (scan,))
        store.conn.commit()
    at = datetime(2026, 11, 1, 1, tzinfo=UTC)
    rows = [sample(at), sample(at + timedelta(seconds=10), usage=10000)]
    (home / "data/logs/load-20261101T000000.jsonl").write_text("\n".join(map(json.dumps, rows)))


def test_review_snapshot_denominator_and_no_db_mutation(tmp_path):
    make_run(tmp_path)
    with sqlite3.connect(tmp_path / "data/radar.db") as db:
        db.execute("UPDATE target SET enabled=0")
    report = review(tmp_path, "2026-11-01")
    assert report["healthy"] and report["expected_jobs"] == report["recorded_jobs"] == 1
    with sqlite3.connect(tmp_path / "data/radar.db") as db:
        assert db.execute("SELECT enabled FROM target").fetchone()[0] == 0


def test_review_never_calls_missing_or_failed_work_success(tmp_path):
    make_run(tmp_path, "pending")
    report = review(tmp_path, "2026-11-01")
    assert not report["complete"] and report["invalid_job_ids"]
    assert not review(tmp_path, "2026-11-02")["healthy"]


def test_missing_db_is_not_created(tmp_path):
    (tmp_path / "data").mkdir()
    with pytest.raises(sqlite3.OperationalError):
        review(tmp_path, "2026-11-01")
    assert not (tmp_path / "data/radar.db").exists()


def test_cpu_counter_reset_not_negative_usage(tmp_path):
    at = datetime(2026, 11, 1, tzinfo=UTC)
    path = tmp_path / "reset.jsonl"
    path.write_text(
        "\n".join(
            map(json.dumps, [sample(at, usage=100), sample(at + timedelta(seconds=5), usage=0)])
        )
    )
    values = summarize_load(path, at, at + timedelta(days=1))["services"]["worker"]
    assert values["counter_resets"] == 1 and values["cpu_peak_cores"] == 0


def test_robots_exclusion_is_recorded_but_not_an_observation_error(tmp_path):
    make_run(tmp_path)
    with sqlite3.connect(tmp_path / "data/radar.db") as db:
        db.execute("UPDATE scan SET status='SKIPPED_BY_ROBOTS'")
    result = review(tmp_path, "2026-11-01")
    assert result["complete"] and result["healthy"] and result["observation_errors"] == 0
    assert result["statuses"] == {"SKIPPED_BY_ROBOTS": 1}


def test_recorded_over_concurrency_limit_is_not_healthy(tmp_path):
    make_run(tmp_path)
    (tmp_path / "radar.toml").write_text("[scan]\nconcurrency=1\n")
    path = next((tmp_path / "data/logs").glob("*.jsonl"))
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[-1]["jobs"]["claimed"] = 2
    path.write_text("\n".join(map(json.dumps, rows)))
    result = review(tmp_path, "2026-11-01")
    assert result["complete"] and not result["healthy"]
    assert "collection_concurrency_exceeded" in result["findings"]
