"""The dashboard must not re-read the whole history once per company.

These are query-shape tests, not benchmarks: they pin the number of table
reads a list view is allowed, so a watchlist growing from dozens into the
hundreds degrades linearly with rows returned — not quadratically with
companies times history.
"""

from __future__ import annotations

import pytest

from radar import evidence as ev
from radar.store import Store
from radar.targets import Target, TargetSet
from web import data


N_COMPANIES = 60


def result(url: str, status: str = ev.STATUS_OK) -> dict:
    return {
        "schema_version": 1,
        "scan": {"url": url, "final_url": url, "page_host": "x", "status": status,
                 "http_status": 200, "title": "t",
                 "started_at": "2026-08-27T00:00:00+00:00", "duration_ms": 5},
        "evidence": {}, "counts": {}, "warnings": [],
    }


def detection(fp: str, category: str = "engagement", score: int = 90) -> dict:
    return {"id": fp, "name": fp.title(), "category": category, "verdict": "DETECTED",
            "score": score, "direct_layers": ["network"], "indirect_layers": [],
            "indirect_only": False, "match_count": 1, "matches": []}


@pytest.fixture
def big_store(tmp_path):
    targets = tuple(
        Target(id=f"co{i}", company=f"회사{i}", industry="fashion",
               urls=(f"https://co{i}.example/",))
        for i in range(N_COMPANIES)
    )
    with Store(tmp_path / "radar.db") as s:
        s.import_watchlist(TargetSet(targets=targets,
                                   industries={"fashion": "패션"}, source="test"))
        for _ in range(2):
            run = s.start_run(N_COMPANIES)
            for i in range(N_COMPANIES):
                scan_id = s.record_scan(run, f"co{i}",
                                        result(f"https://co{i}.example/"), False)
                s.record_detections(scan_id, {"detections": [
                    detection("braze"), detection("ga4", "analytics")]})
        yield s


HISTORY_SHAPES = ("FROM scan ORDER BY", "FROM scan WHERE",
                  "LEFT JOIN detection")
# `data_state()`'s O(1) digest also says "FROM scan"; these needles match
# only the queries that actually walk the history.


def count_history_reads(store: Store) -> list[int]:
    hits = [0]
    store.conn.set_trace_callback(
        lambda st: hits.__setitem__(
            0, hits[0] + any(shape in st for shape in HISTORY_SHAPES)))
    return hits


def count_queries(store: Store, needle: str) -> list[int]:
    hits = [0]
    def trace(statement: str) -> None:
        if needle in statement:
            hits[0] += 1
    store.conn.set_trace_callback(trace)
    return hits


def test_stacks_of_matches_stack_of(big_store):
    stacks = big_store.stacks_of()
    for target_id in ("co0", "co31"):
        assert stacks[target_id] == big_store.stack_of(target_id)


def test_company_rows_reads_no_history_when_summaries_are_fresh(big_store):
    from radar import signals as sig
    sig.summaries(big_store)                      # warm (and materialise)
    hits = count_history_reads(big_store)
    data.company_rows(big_store, {"fashion": "패션"})
    big_store.conn.set_trace_callback(None)
    assert hits[0] == 0


def test_company_rows_never_reads_detections_per_scan(big_store):
    hits = count_queries(big_store, "FROM detection WHERE scan_id = ?")
    data.company_rows(big_store, {"fashion": "패션"})
    big_store.conn.set_trace_callback(None)
    assert hits[0] == 0  # batched IN (...) read only


def test_vendor_rows_reads_no_history_when_summaries_are_fresh(big_store):
    from radar import signals as sig
    sig.summaries(big_store)
    companies = data.company_rows(big_store, {"fashion": "패션"})
    hits = count_history_reads(big_store)
    data.vendor_rows(big_store, companies)
    big_store.conn.set_trace_callback(None)
    assert hits[0] == 0


def test_all_signals_cold_rebuilds_history_once(big_store):
    """A stale summary triggers exactly one timeline join, not one per company."""
    from radar import signals as sig
    hits = count_queries(big_store, "LEFT JOIN detection")
    sig.all_signals(big_store)
    big_store.conn.set_trace_callback(None)
    assert hits[0] == 1


def test_signals_page_trio_runs_on_summaries(big_store):
    """all_signals + quiet_greenfield + company_rows share one summary
    build; after it, none of them touches the scan history at all."""
    from radar import signals as sig
    sig.summaries(big_store)                      # warm (and materialise)
    hits = count_history_reads(big_store)
    sig.all_signals(big_store)
    sig.quiet_greenfield(big_store)
    data.company_rows(big_store, {"fashion": "패션"})
    big_store.conn.set_trace_callback(None)
    assert hits[0] == 0


def test_timelines_batch_matches_per_target(big_store):
    from radar import signals as sig
    batch = sig.timelines(big_store)
    for target_id in ("co0", "co31"):
        assert batch[target_id] == sig.timeline(big_store, target_id)


def test_scan_list_queries_leave_the_evidence_blob_behind(big_store):
    for row in big_store.latest_scans()[:1]:
        columns = list(row.keys())
        assert "evidence_gz" not in columns
    for row in big_store.scans_for("co0", limit=1):
        columns = list(row.keys())
        assert "evidence_gz" not in columns


def test_config_load_is_cached_until_the_file_changes(tmp_path):
    from radar import config as cfg
    path = tmp_path / "radar.toml"
    path.write_text('[ui]\nlanguage = "ko"\n')
    first = cfg.load(path)
    assert cfg.load(path) is first          # same mtime -> cached object
    import os
    os.utime(path, ns=(os.stat(path).st_mtime_ns + 2_000_000_000,) * 2)
    assert cfg.load(path) is not first      # touched -> re-read
