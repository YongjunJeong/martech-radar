"""Account IDs: collected long ago, now visible per company."""

from __future__ import annotations

from radar.store import Store
from radar.targets import Target, TargetSet


def result(url: str, identifiers: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "scan": {"url": url, "final_url": url, "page_host": "x", "status": "OK",
                 "http_status": 200, "title": "t",
                 "started_at": "2026-09-04T00:00:00+00:00", "duration_ms": 5},
        "evidence": {"identifiers": identifiers}, "counts": {}, "warnings": [],
    }


def test_identifiers_deduped_across_urls_and_sorted(tmp_path):
    with Store(tmp_path / "radar.db") as s:
        s.import_watchlist(TargetSet(
            targets=(Target(id="shop", company="가나다몰", industry="fashion",
                            urls=("https://shop.example/", "https://shop.example/p")),),
            industries={"fashion": "패션"}, source="test"))
        run = s.start_run(1)
        s.record_scan(run, "shop", result("https://shop.example/", [
            {"kind": "gtm_container", "value": "GTM-AAA"},
            {"kind": "criteo_partner", "value": "129538"}]), True)
        s.record_scan(run, "shop", result("https://shop.example/p", [
            {"kind": "gtm_container", "value": "GTM-AAA"},       # duplicate
            {"kind": "ga4_measurement", "value": "G-XYZ"}]), True)
        rows = s.identifiers_of("shop")
    assert [(r["kind"], r["value"]) for r in rows] == [
        ("criteo_partner", "129538"),
        ("ga4_measurement", "G-XYZ"),
        ("gtm_container", "GTM-AAA"),
    ]


def test_evidence_free_scans_yield_no_identifiers(tmp_path):
    with Store(tmp_path / "radar.db") as s:
        s.import_watchlist(TargetSet(
            targets=(Target(id="shop", company="몰", industry="fashion",
                            urls=("https://shop.example/",)),),
            industries={"fashion": "패션"}, source="test"))
        run = s.start_run(1)
        s.record_scan(run, "shop", result("https://shop.example/", []), False)
        assert s.identifiers_of("shop") == []
