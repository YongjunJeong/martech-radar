"""Cross-process budgets, bounded schedules, and incremental summary work."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Lock

import pytest

from radar import batch, config, signals
from radar.store import Store
from radar.targets import Target, TargetSet
from radar import calls


def seed(s, n=12, same_origin=False):
    targets = tuple(
        Target(
            f"c{i}",
            f"C{i}",
            "retail",
            (f"https://{'shared' if same_origin else 'c' + str(i)}.test/{i}",),
        )
        for i in range(n)
    )
    s.import_watchlist(TargetSet(targets, {"retail": "Retail"}, "test"))


def record(s, target="c0", when="2026-10-01T00:00:00+00:00"):
    run = s.start_run(1)
    return s.record_scan(
        run,
        target,
        {
            "scan": {
                "url": f"https://{target}.test/0",
                "page_host": f"{target}.test",
                "status": "OK",
                "started_at": when,
            },
            "evidence": {},
        },
        False,
    )


def test_incremental_refresh_only_reads_changed_target(tmp_path):
    with Store(tmp_path / "r.db") as s:
        seed(s, 2)
        record(s, "c0")
        record(s, "c1")
        signals.refresh_summaries(s)
        unchanged = dict(
            s.conn.execute("SELECT * FROM company_summary WHERE target_id='c1'").fetchone()
        )
        record(s, "c0", "2026-10-02T00:00:00+00:00")
        statements = []
        s.conn.set_trace_callback(statements.append)
        assert signals.refresh_summaries(s) == 1
        s.conn.set_trace_callback(None)
        timeline = [q for q in statements if "LEFT JOIN detection" in q]
        assert len(timeline) == 1 and "WHERE s.target_id IN ('c0')" in timeline[0]
        assert (
            dict(s.conn.execute("SELECT * FROM company_summary WHERE target_id='c1'").fetchone())
            == unchanged
        )
        assert signals.refresh_summaries(s) == 0


def test_summary_refresh_is_coalesced_across_connections(tmp_path, monkeypatch):
    path = tmp_path / "r.db"
    with Store(path) as s:
        seed(s)
        record(s)
    barrier = Barrier(4)
    guard = Lock()
    counts = []
    original = signals._refresh_locked

    def measured(s):
        result = original(s)
        with guard:
            counts.append(result)
        return result

    monkeypatch.setattr(signals, "_refresh_locked", measured)

    def reader(_):
        with Store(path) as s:
            barrier.wait()
            return signals.summaries(s)["c0"].scans

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(reader, range(4))) == [1] * 4
    assert sum(n > 0 for n in counts) == 1


def test_revision_detects_same_count_updates_and_has_no_history_scan(tmp_path):
    with Store(tmp_path / "r.db") as s:
        seed(s)
        scan = record(s)
        before = s.data_state()
        s.conn.execute("UPDATE scan SET status='BLOCKED' WHERE id=?", (scan,))
        s.conn.commit()
        assert s.data_state() != before
        queries = []
        s.conn.set_trace_callback(queries.append)
        s.data_state()
        assert not any("FROM scan" in q or "FROM detection" in q for q in queries)


def test_scan_transaction_is_not_visible_until_complete_and_rolls_back(tmp_path):
    path = tmp_path / "r.db"
    with Store(path) as s, Store(path) as reader:
        seed(s)
        with s.transaction():
            record(s)
            assert reader.conn.execute("SELECT COUNT(*) FROM scan").fetchone()[0] == 0
        assert reader.conn.execute("SELECT COUNT(*) FROM scan").fetchone()[0] == 1
        with pytest.raises(RuntimeError), s.transaction():
            record(s)
            raise RuntimeError("rollback")
        assert reader.conn.execute("SELECT COUNT(*) FROM scan").fetchone()[0] == 1


def test_global_limit_across_workers(tmp_path, monkeypatch):
    monkeypatch.setattr(
        config, "load", lambda: config.Config(scan=config.ScanSettings(concurrency=3))
    )
    path = tmp_path / "r.db"
    with Store(path) as s:
        seed(s)
        s.queue_run(batch.jobs_for(s))
    barrier = Barrier(8)

    def claim(i):
        with Store(path) as s:
            barrier.wait()
            return s.claim_job(str(i))

    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = [j for j in pool.map(claim, range(8)) if j]
    assert len(claimed) == 3 and len({j["id"] for j in claimed}) == 3


def test_domain_is_serialized_and_cooldown_is_shared(tmp_path, monkeypatch):
    monkeypatch.setattr(
        config, "load", lambda: config.Config(scan=config.ScanSettings(per_site_delay_ms=60000))
    )
    path = tmp_path / "r.db"
    with Store(path) as s, Store(path) as other:
        seed(s, 3, same_origin=True)
        s.queue_run(batch.jobs_for(s))
        job = s.claim_job("first")
        assert other.claim_job("second") is None
        s.fail_job(job["id"], "test")
        assert other.claim_job("second") is None
        s.conn.execute("UPDATE origin_cooldown SET ready_at='2000-01-01'")
        s.conn.commit()
        assert other.claim_job("second")


def test_weekly_spread_is_stable_and_every_target_runs_once(tmp_path):
    cadence = config.CadenceSettings(spread_weekly=True, daily_limit=150)
    start = datetime(2026, 10, 17, 3, tzinfo=UTC)
    seen = []
    counts = []
    with Store(tmp_path / "r.db") as s:
        seed(s, 782)
        jobs = batch.jobs_for(s)
        for day in range(7):
            now = start + timedelta(days=day)
            due, _ = batch.due_split(s, jobs, cadence, now)
            assert batch.due_split(s, list(reversed(jobs)), cadence, now)[0] == due
            counts.append(len(due))
            for job in due:
                run = s.start_run(1)
                s.record_scan(
                    run,
                    job.target_id,
                    {"scan": {"url": job.url, "status": "BLOCKED", "started_at": now.isoformat()}},
                    False,
                )
                seen.append(job.target_id)
            assert batch.due_split(s, jobs, cadence, now)[0] == []
        assert len(seen) == len(set(seen)) == 782
        assert max(counts) <= 150 and min(counts) > 50
        assert batch.due_split(s, jobs, cadence, start + timedelta(days=7))[0]


def test_backlog_survives_cap_and_new_url_gets_its_own_schedule(tmp_path):
    cadence = config.CadenceSettings(spread_weekly=True, daily_limit=2)
    now = datetime(2026, 10, 17, tzinfo=UTC)
    with Store(tmp_path / "r.db") as s:
        seed(s, 10)
        jobs = batch.jobs_for(s)
        batch.due_split(s, jobs, cadence, now)
        s.conn.execute("UPDATE scan_schedule SET next_due='2026-10-01'")
        s.conn.commit()
        first, _ = batch.due_split(s, jobs, cadence, now)
        assert len(first) == 2
        for j in first:
            r = s.start_run(1)
            s.record_scan(
                r,
                j.target_id,
                {"scan": {"url": j.url, "status": "TIMEOUT", "started_at": now.isoformat()}},
                False,
            )
        assert not batch.due_split(s, jobs, cadence, now)[0]
        next_day, _ = batch.due_split(s, jobs, cadence, now + timedelta(days=1))
        assert len(next_day) == 2 and not set(next_day) & set(first)
        extra = batch.Job("c0", "C0", "https://c0.test/product")
        batch.due_split(s, [extra], cadence, now)
        assert (
            s.conn.execute('SELECT COUNT(*) FROM scan_schedule WHERE target_id="c0"').fetchone()[0]
            == 2
        )


def test_review_queue_does_not_read_history_once_per_signal(tmp_path, monkeypatch):
    with Store(tmp_path / "r.db") as s:
        seed(s)
        for j in batch.jobs_for(s):
            record(s, j.target_id)
            sid = record(s, j.target_id, "2026-10-02T00:00:00+00:00")
            s.record_detections(
                sid,
                {
                    "detections": [
                        {
                            "id": str(i),
                            "name": str(i),
                            "category": "analytics",
                            "verdict": "DETECTED",
                            "score": 90,
                            "direct_layers": ["network"],
                            "indirect_layers": [],
                            "indirect_only": False,
                            "matches": [],
                            "match_count": 1,
                        }
                        for i in range(3)
                    ]
                },
            )
        signals.refresh_summaries(s)
        original = s.latest_scans
        seen = []

        def latest(*args, **kwargs):
            seen.append(1)
            return original(*args, **kwargs)

        monkeypatch.setattr(s, "latest_scans", latest)
        assert calls.call_list(s)
        assert len(seen) == 1


def test_concurrent_enqueuers_do_not_duplicate_active_visits(tmp_path):
    path = tmp_path / "r.db"
    with Store(path) as s:
        seed(s, 5)
        jobs = batch.jobs_for(s)
    barrier = Barrier(2)

    def enqueue(_):
        with Store(path) as s:
            barrier.wait()
            return s.queue_run(jobs)

    with ThreadPoolExecutor(max_workers=2) as pool:
        runs = list(pool.map(enqueue, range(2)))
    with Store(path) as s:
        assert s.conn.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 5
        assert len(runs) == 2
        assert (
            s.conn.execute("SELECT COUNT(*) FROM run WHERE finished_at IS NOT NULL").fetchone()[0]
            == 1
        )


def test_crawl_delay_cooldown_survives_worker_change(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "load", lambda: config.Config())
    with Store(tmp_path / "r.db") as s:
        seed(s, 2, same_origin=True)
        s.remember_robots("https://shared.test", "User-agent: *\nCrawl-delay: 120", "test")
        s.queue_run(batch.jobs_for(s))
        job = s.claim_job("first")
        s.fail_job(job["id"], "test")
        ready = datetime.fromisoformat(
            s.conn.execute("SELECT ready_at FROM origin_cooldown").fetchone()[0]
        )
        assert (ready - datetime.now(UTC)).total_seconds() > 110
