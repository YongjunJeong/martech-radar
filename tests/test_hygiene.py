"""Dead-domain detection: streaks of failure, per URL, robots exempt."""

from __future__ import annotations

from radar import hygiene
from radar.store import Store
from radar.targets import Target, TargetSet


def result(url: str, status: str = "OK", at: str = "2026-09-01T00:00:00+00:00") -> dict:
    return {
        "schema_version": 1,
        "scan": {"url": url, "final_url": url, "page_host": "x", "status": status,
                 "http_status": 200, "title": "t", "started_at": at,
                 "duration_ms": 5},
        "evidence": {}, "counts": {}, "warnings": [],
    }


def seed(tmp_path, statuses, note=None):
    s = Store(tmp_path / "radar.db")
    s.import_watchlist(TargetSet(
        targets=(Target(id="shop", company="가나다몰", industry="fashion",
                        urls=("https://shop.example/",), note=note),),
        industries={"fashion": "패션"}, source="test"))
    for i, status in enumerate(statuses):
        run = s.start_run(1)
        s.record_scan(run, "shop", result("https://shop.example/", status,
                                          at=f"2026-09-0{i+1}T00:00:00+00:00"), False)
    return s


def test_three_straight_failures_are_flagged_with_last_good(tmp_path):
    with seed(tmp_path, ["OK", "NAV_FAILED", "NAV_FAILED", "NAV_FAILED"]) as s:
        rows = hygiene.report(s)
    assert len(rows) == 1
    row = rows[0]
    assert row["streak"] == 3 and row["dominant"] == "NAV_FAILED"
    assert row["last_good_at"].startswith("2026-09-01")


def test_two_failures_are_not_yet_a_pattern(tmp_path):
    with seed(tmp_path, ["OK", "NAV_FAILED", "NAV_FAILED"]) as s:
        assert hygiene.report(s) == []


def test_a_recovery_resets_the_streak(tmp_path):
    with seed(tmp_path, ["NAV_FAILED", "NAV_FAILED", "NAV_FAILED", "OK"]) as s:
        assert hygiene.report(s) == []


def test_robots_skips_neither_break_nor_extend_a_streak(tmp_path):
    with seed(tmp_path, ["NAV_FAILED", "SKIPPED_BY_ROBOTS", "NAV_FAILED",
                         "NAV_FAILED"]) as s:
        rows = hygiene.report(s)
    assert rows and rows[0]["streak"] == 3


def test_disabled_targets_are_not_reported(tmp_path):
    with seed(tmp_path, ["NAV_FAILED"] * 3) as s:
        s.set_target_enabled("shop", False)
        assert hygiene.report(s) == []
