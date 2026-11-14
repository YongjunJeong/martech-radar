"""Scale safety: bounded rendering, pre-queue omissions and recoverable backups."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from radar import batch, config, recovery, signals
from radar.operations import review
from radar.store import Store
from web import sales
from test_capacity import seed
import test_analysis_window as analysis

from test_container_operations import make_run
from test_hosting_readiness import source

scoped = analysis.scoped
scan = analysis.scan


def test_board_only_renders_visible_targets_and_detail_only_loads_one(scoped, monkeypatch):
    seed(scoped, 60)
    scan(scoped, "2026-10-15T03:00:00+09:00")
    signals.refresh_summaries(scoped)
    original = scoped.summary_rows
    loaded = []

    def measured(target_ids=None):
        assert target_ids is not None, "whole summary payload loaded"
        loaded.append(tuple(target_ids))
        return original(target_ids)

    monkeypatch.setattr(scoped, "summary_rows", measured)
    page = sales.board(scoped, {}, status="all", page=2)
    assert page["total"] == 61 and len(page["cards"]) == 24
    assert len(loaded) == 1 and len(loaded[0]) == 24
    sales.company_cards(scoped, {}, "acme")
    assert loaded[-1] == ("acme",)


def test_filter_projection_refreshes_after_regrade_and_old_writer(scoped):
    scan(scoped, "2026-10-15T03:00:00+09:00", ["sdk.iad-03.braze.com"])
    sid = scan(scoped, "2026-10-16T03:00:00+09:00")
    assert sales.board(scoped, {}, observation="missing")["counts"]["watch"] == 1
    scoped.conn.execute("UPDATE scan SET status='BLOCKED' WHERE id=?", (sid,))
    scoped.conn.commit()
    assert sales.board(scoped, {}, status="all")["counts"]["check"] == 1
    scoped.conn.execute("DELETE FROM company_filter_summary")
    scoped.conn.execute("UPDATE meta SET value='old|s7' WHERE key='summary_state'")
    scoped.conn.commit()
    assert sales.board(scoped, {}, status="all")["counts"]["check"] == 1
    assert scoped.conn.execute("SELECT COUNT(*) FROM company_filter_summary").fetchone()[0] == 1


def test_cap_audit_records_omissions_and_oldest_targets_go_first(tmp_path):
    now = datetime(2026, 11, 2, tzinfo=UTC)
    with Store(tmp_path / "r.db") as store:
        seed(store, 5)
        audit = {}
        due, _ = batch.due_split(
            store,
            batch.jobs_for(store),
            config.CadenceSettings(default="1d", daily_limit=2),
            now,
            audit=audit,
        )
        assert len(due) == 2
        assert sum(i["decision"] == "budget_deferred" for i in audit["items"]) == 3
        for job in due:
            run = store.start_run(1)
            store.record_scan(
                run,
                job.target_id,
                {"scan": {"url": job.url, "status": "OK", "started_at": now.isoformat()}},
                False,
            )
        next_day, _ = batch.due_split(
            store,
            batch.jobs_for(store),
            config.CadenceSettings(default="1d", daily_limit=2),
            now + timedelta(days=1),
        )
        assert not {j.target_id for j in due} & {j.target_id for j in next_day}
        all_due, _ = batch.due_split(
            store,
            batch.jobs_for(store),
            config.CadenceSettings(default="1d", daily_limit=0),
            now + timedelta(days=1),
        )
        assert len(all_due) == 5


def test_review_finds_prequeue_omission_even_when_queue_is_complete(tmp_path):
    make_run(tmp_path)
    with Store(tmp_path / "data/radar.db") as store:
        row = store.conn.execute("SELECT items_json FROM collection_plan").fetchone()
        items = json.loads(row[0]) + [
            dict(target_id="omitted", url="https://omitted.test", decision="budget_deferred")
        ]
        store.conn.execute("UPDATE collection_plan SET items_json=?", (json.dumps(items),))
        store.conn.commit()
    result = review(tmp_path, "2026-11-01")
    assert result["queue_complete"] and not result["complete"]
    assert result["expected_urls_before_budget"] == 2 and result["recorded_jobs"] == 1
    assert result["missing_expected_urls"] == [
        dict(target_id="omitted", url="https://omitted.test")
    ]
    assert "expected_urls_not_collected" in result["findings"]


def test_legacy_run_cannot_prove_prequeue_coverage(tmp_path):
    make_run(tmp_path)
    with Store(tmp_path / "data/radar.db") as store:
        store.conn.execute("DELETE FROM collection_plan")
        store.conn.commit()
    result = review(tmp_path, "2026-11-01")
    assert result["queue_complete"] and not result["coverage_verified"] and not result["healthy"]


def test_noop_plan_and_crash_before_queue_are_distinguished(tmp_path):
    (tmp_path / "data").mkdir()
    with Store(tmp_path / "data/radar.db") as store:
        store.save_collection_plan(
            dict(
                created_at="2026-11-01T00:00:00+00:00",
                cadence={},
                items=[dict(target_id="done_today", url="https://example.com", decision="not_due")],
            ),
            "scheduled 2026-11-01",
        )
    assert review(tmp_path, "2026-11-01")["complete"]
    with Store(tmp_path / "data/radar.db") as store:
        store.save_collection_plan(
            dict(
                created_at="2026-11-01T00:00:00+00:00",
                cadence={},
                items=[
                    dict(target_id="not_queued", url="https://example.com", decision="scheduled")
                ],
            ),
            "scheduled 2026-11-01",
        )
    assert not review(tmp_path, "2026-11-01")["complete"]


def test_backup_default_keeps_three_and_restore_is_verified(tmp_path):
    home = source(tmp_path / "home")
    dest = tmp_path / "backups"
    snapshots = [recovery.backup(home, dest) for _ in range(4)]
    assert not snapshots[0].exists() and all(p.exists() for p in snapshots[1:])
    assert recovery.restore(snapshots[-1], tmp_path / "restore")["counts"]["target"] == 1


def test_low_space_never_prunes_existing_recovery_points(tmp_path, monkeypatch):
    home = source(tmp_path / "home")
    dest = tmp_path / "backups"
    old = recovery.backup(home, dest)
    monkeypatch.setattr(recovery.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    with pytest.raises(ValueError, match="headroom"):
        recovery.backup(home, dest, keep=1)
    assert old.exists() and recovery.verify(old)


def test_backup_byte_budget_cannot_remove_last_two_copies(tmp_path):
    home = source(tmp_path / "home")
    dest = tmp_path / "backups"
    old = recovery.backup(home, dest)
    with pytest.raises(ValueError, match="two recovery points"):
        recovery.backup(home, dest, max_bytes=1)
    assert old.exists() and recovery.verify(old)


def test_cli_records_full_plan_before_queueing(tmp_path, monkeypatch):
    from radar import cli

    settings = config.Config(cadence=config.CadenceSettings(default="1d", daily_limit=2))
    monkeypatch.setattr(config, "load", lambda *a, **kw: settings)
    path = tmp_path / "radar.db"
    with Store(path) as store:
        seed(store, 5)

    async def queued_only(store, registry, jobs, **kwargs):
        store.queue_run(jobs, note=kwargs["note"], plan_id=kwargs["plan_id"])
        return SimpleNamespace(ok=[True], failed=[], summary=lambda: "queued test")

    monkeypatch.setattr(batch, "run_batch", queued_only)
    assert cli.main(["batch", "--db", str(path), "--due", "--note", "scheduled 2026-11-02"]) == 0
    with Store(path) as store:
        plan = store.conn.execute("SELECT * FROM collection_plan").fetchone()
        assert len(json.loads(plan["items_json"])) == 5 and plan["run_id"]
        assert store.conn.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 2


def test_backup_byte_budget_trims_to_two_verified_copies(tmp_path):
    home = source(tmp_path / "home")
    dest = tmp_path / "backups"
    first = recovery.backup(home, dest)
    snapshot_bytes = sum(p.stat().st_size for p in first.rglob("*") if p.is_file())
    second = recovery.backup(home, dest)
    third = recovery.backup(home, dest, max_bytes=int(snapshot_bytes * 2.5))
    assert not first.exists() and second.exists() and third.exists()
    assert recovery.verify(second) and recovery.verify(third)


def test_review_reports_overlong_collection_and_disk_headroom(tmp_path, monkeypatch):
    from radar import operations

    make_run(tmp_path)
    monkeypatch.setattr(operations.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    result = review(tmp_path, "2026-11-01", max_run_hours=0.001)
    assert "database_disk_headroom_low" in result["findings"]
    assert any(f.startswith("collection_duration_exceeded:") for f in result["findings"])
