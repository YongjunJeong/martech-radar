"""The materialised Gold layer: correct, invalidated by data, never stale."""

from __future__ import annotations

from radar import evidence as ev
from radar import signals as sig
from radar.store import Store
from radar.targets import Target, TargetSet
from radar import worker


def result(url: str, status: str = ev.STATUS_OK) -> dict:
    return {
        "schema_version": 1,
        "scan": {"url": url, "final_url": url, "page_host": "x", "status": status,
                 "http_status": 200, "title": "t",
                 "started_at": "2026-09-04T00:00:00+00:00", "duration_ms": 5},
        "evidence": {}, "counts": {}, "warnings": [],
    }


def detection(fp: str, category: str = "engagement", score: int = 90) -> dict:
    return {"id": fp, "name": fp.title(), "category": category, "verdict": "DETECTED",
            "score": score, "direct_layers": ["network"], "indirect_layers": [],
            "indirect_only": False, "match_count": 1, "matches": []}


def watchlist(*ids: str) -> TargetSet:
    return TargetSet(
        targets=tuple(Target(id=i, company=i, industry="fashion",
                             urls=(f"https://{i}.example/",)) for i in ids),
        industries={"fashion": "패션"}, source="test")


def seed(store: Store, *ids: str, runs: int = 2) -> None:
    store.import_watchlist(watchlist(*ids))
    for _ in range(runs):
        run = store.start_run(len(ids))
        for i in ids:
            scan_id = store.record_scan(run, i, result(f"https://{i}.example/"), False)
            store.record_detections(scan_id, {"detections": [detection("braze")]})


def test_summaries_match_live_computation(tmp_path):
    with Store(tmp_path / "radar.db") as s:
        seed(s, "shop", "mart")
        summary = sig.summaries(s)["shop"]
        assert summary.stack == s.stack_of("shop")
        assert summary.changes == sig.changes_for(s, "shop")
        assert summary.maturity == sig.maturity_of(s, "shop")
        cover = s.coverage()["shop"]
        assert summary.judged == cover["judged"]
        assert summary.good_scans == cover["good_scans"]


def test_signals_from_summary_match_signals_for_live(tmp_path):
    with Store(tmp_path / "radar.db") as s:
        seed(s, "shop")
        target = s.target("shop")
        live = sig.signals_for(s, target)
        via_summary = [x for x in sig.all_signals(s) if x.target_id == "shop"]
        assert [x.as_dict() for x in live] == [x.as_dict() for x in via_summary]


def test_a_new_scan_invalidates_the_summaries(tmp_path):
    with Store(tmp_path / "radar.db") as s:
        seed(s, "shop")
        assert sig.summaries(s)["shop"].stack           # braze present
        run = s.start_run(1)
        scan_id = s.record_scan(run, "shop", result("https://shop.example/"), False)
        s.record_detections(scan_id, {"detections": []})  # vendor gone
        summary = sig.summaries(s)["shop"]              # no explicit refresh
        assert not summary.stack


def test_a_blocked_scan_only_still_shows_the_last_good_stack(tmp_path):
    with Store(tmp_path / "radar.db") as s:
        seed(s, "shop")
        run = s.start_run(1)
        s.record_scan(run, "shop", result("https://shop.example/",
                                          status=ev.STATUS_BLOCKED), False)
        summary = sig.summaries(s)["shop"]
        assert summary.stack                            # fell back, not erased
        assert summary.last_status == ev.STATUS_BLOCKED


def test_finish_runs_refreshes_summaries(tmp_path):
    from types import SimpleNamespace
    with Store(tmp_path / "radar.db") as s:
        seed(s, "shop")
        run_id = s.queue_run([SimpleNamespace(target_id="shop",
                                              url="https://shop.example/")])
        job = s.claim_job("tester", run_id)
        scan_id = s.record_scan(run_id, "shop", result("https://shop.example/"), False)
        s.finish_job(job["id"], scan_id)
        worker.finish_runs(s)                           # run drained -> closed
        assert s.summary_state() == f"{s.data_state()}|s{sig.SUMMARY_VERSION}"
