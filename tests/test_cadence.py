"""Tier-based scan cadence: who is due, and who earned their quiet."""

from __future__ import annotations

from datetime import datetime, timedelta, UTC

import pytest

from radar import batch as bat
from radar import config as cfg
from radar.store import Store
from radar.targets import Target, TargetSet


def watchlist():
    def t(i, tier):
        return Target(id=i, company=i, industry="fashion",
                      urls=(f"https://{i}.example/",), tier=tier)
    return TargetSet(
        targets=(t("hot", "P1"), t("warm", "P2"), t("cold", None)),
        industries={"fashion": "패션"}, source="test")


def result(url):
    return {
        "schema_version": 1,
        "scan": {"url": url, "final_url": url, "page_host": "x", "status": "OK",
                 "http_status": 200, "title": "t",
                 "started_at": "2026-09-04T00:00:00+00:00", "duration_ms": 5},
        "evidence": {}, "counts": {}, "warnings": [],
    }


CADENCE = cfg.CadenceSettings(default="7d", tiers={"P1": "1d", "P2": "7d"})


def test_parse_duration_accepts_days_and_hours_only():
    assert cfg.parse_duration("12h") == 12 * 3600
    assert cfg.parse_duration("7d") == 7 * 86400
    for bad in ("7", "1w", "0d", "-1d", "d", ""):
        with pytest.raises(cfg.ConfigError):
            cfg.parse_duration(bad)


def test_cadence_section_round_trips_through_load(tmp_path):
    path = tmp_path / "radar.toml"
    path.write_text('[cadence]\ndefault = "3d"\nP1 = "12h"\n')
    loaded = cfg.load(path).cadence
    assert loaded.seconds_for("P1") == 12 * 3600
    assert loaded.seconds_for("P9") == 3 * 86400   # unlisted tier -> default
    assert loaded.seconds_for(None) == 3 * 86400   # untiered -> default
    path.write_text('[cadence]\nP1 = "fortnight"\n')
    import os
    os.utime(path, ns=(os.stat(path).st_mtime_ns + 2_000_000_000,) * 2)
    with pytest.raises(cfg.ConfigError):
        cfg.load(path)


def test_jobs_for_filters_by_tier_and_names_the_untiered(tmp_path):
    with Store(tmp_path / "radar.db") as s:
        s.import_watchlist(watchlist())
        assert {j.target_id for j in bat.jobs_for(s, tiers=["P1"])} == {"hot"}
        assert {j.target_id for j in bat.jobs_for(s, tiers=["none"])} == {"cold"}
        assert {j.target_id for j in bat.jobs_for(s, tiers=["P1", "P2"])} == {"hot", "warm"}
        assert {j.target_id for j in bat.jobs_for(s)} == {"hot", "warm", "cold"}


def test_due_split_by_tier_interval(tmp_path):
    with Store(tmp_path / "radar.db") as s:
        s.import_watchlist(watchlist())
        run = s.start_run(3)
        for target in ("hot", "warm", "cold"):
            s.record_scan(run, target, result(f"https://{target}.example/"), False)
        scanned_at = datetime.fromisoformat("2026-09-04T00:00:00+00:00")

        jobs = bat.jobs_for(s)
        # two days later: P1 (1d) due again, P2/default (7d) still fresh
        due, fresh = bat.due_split(s, jobs, CADENCE, now=scanned_at + timedelta(days=2))
        assert {j.target_id for j in due} == {"hot"}
        assert set(fresh) == {"warm", "cold"}
        # eight days later: everyone is due
        due, fresh = bat.due_split(s, jobs, CADENCE, now=scanned_at + timedelta(days=8))
        assert {j.target_id for j in due} == {"hot", "warm", "cold"} and not fresh


def test_never_scanned_is_always_due(tmp_path):
    with Store(tmp_path / "radar.db") as s:
        s.import_watchlist(watchlist())
        due, fresh = bat.due_split(s, bat.jobs_for(s), CADENCE,
                                   now=datetime.now(UTC))
        assert {j.target_id for j in due} == {"hot", "warm", "cold"} and not fresh


def test_a_blocked_attempt_still_counts_as_an_attempt(tmp_path):
    """A site that turned us away yesterday must not be retried sooner."""
    with Store(tmp_path / "radar.db") as s:
        s.import_watchlist(watchlist())
        run = s.start_run(1)
        blocked = result("https://hot.example/")
        blocked["scan"]["status"] = "BLOCKED"
        s.record_scan(run, "hot", blocked, False)
        scanned_at = datetime.fromisoformat("2026-09-04T00:00:00+00:00")
        due, fresh = bat.due_split(
            s, bat.jobs_for(s, tiers=["P1"]), CADENCE,
            now=scanned_at + timedelta(hours=12))
        assert not due and fresh == ["hot"]
