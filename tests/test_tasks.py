"""Maintenance tasks queued from the dashboard and run by a worker.

The dashboard never does the work; it writes a row. What matters is that
the row travels the whole way — claimed, run on its own connection, its
output and failure recorded — and that a worker that died mid-task does
not leave the row "running" forever.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from radar import registry as reg
from radar import tasks
from radar import worker as wrk
from radar.collector import ScanConfig
from radar.store import Store
from radar.targets import Target, TargetSet


@pytest.fixture(scope="module")
def registry():
    return reg.load()


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "radar.db") as s:
        s.import_watchlist(TargetSet(
            targets=(Target(id="acme", company="에이스몰", industry="fashion",
                            urls=("https://acme.test/",)),),
            industries={"fashion": "패션"}, source="test"))
        yield s


def test_a_task_travels_from_queue_to_output(store, registry):
    task_id = store.queue_task("hygiene", {"streak": 2}, requested_by="dashboard")
    assert [t["kind"] for t in store.active_tasks()] == ["hygiene"]

    claimed = store.claim_task("worker-a")
    assert claimed["id"] == task_id and claimed["params"] == {"streak": 2}
    assert store.claim_task("worker-b") is None, "a claimed task is nobody else's"

    tasks.run_task(store.path, claimed, registry)
    row = store.task(task_id)
    assert row["state"] == "done" and "watchlist looks healthy" in row["output"]
    assert store.tasks()[0]["output_len"] == len(row["output"])
    assert store.active_tasks() == []


def test_backup_task_writes_a_snapshot_into_the_workspace(store, registry, isolated_workspace):
    store.queue_task("backup", {"keep": 3})
    claimed = store.claim_task("w")
    tasks.run_task(store.path, claimed, registry)
    row = store.task(claimed["id"])
    assert row["state"] == "done", row["error"]
    snapshots = list((isolated_workspace / "backups").glob("radar-*.db"))
    assert len(snapshots) == 1
    with Store(snapshots[0]) as copy:
        assert [r["id"] for r in copy.targets()] == ["acme"]


def test_a_failing_task_is_recorded_not_raised(store, registry):
    store.queue_task("no_such_task")
    claimed = store.claim_task("w")
    tasks.run_task(store.path, claimed, registry)      # must not raise
    row = store.task(claimed["id"])
    assert row["state"] == "failed" and "unknown task kind" in row["error"]


def test_a_task_left_running_by_a_dead_worker_is_failed_on_the_next_claim(store):
    store.queue_task("digest")
    dead = store.claim_task("gone")
    long_ago = (datetime.now(UTC) - timedelta(seconds=Store.TASK_TIMEOUT_SECONDS + 60)).isoformat()
    store.conn.execute("UPDATE task SET started_at = ? WHERE id = ?", (long_ago, dead["id"]))
    store.conn.commit()
    assert store.claim_task("alive") is None
    assert store.task(dead["id"])["state"] == "failed"


def test_the_worker_runs_queued_tasks_and_leaves_a_heartbeat(store, registry):
    store.queue_task("hygiene")
    report = asyncio.run(wrk.drain_queue(
        store, registry, scan_config=ScanConfig(settle_ms=200), worker="wk", idle_exit=True))
    assert report.done == 0                       # nothing to scan
    assert store.tasks()[0]["state"] == "done"    # but the chore got done


def test_radar_task_runs_inline_for_a_terminal(store, registry, capsys):
    from radar import workspace
    from radar.cli import main
    try:
        assert main(["--home", str(store.path.parent), "task", "hygiene",
                     "--db", str(store.path)]) == 0
    finally:
        workspace.set_home(None)
    assert "watchlist looks healthy" in capsys.readouterr().out
