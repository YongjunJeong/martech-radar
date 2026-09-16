"""The registry's blind spot, made visible from stored evidence."""

from __future__ import annotations

from radar import mining
from radar import registry as reg
from radar.store import Store


def result(url: str, page_host: str, **evidence) -> dict:
    return {
        "schema_version": 1,
        "scan": {"url": url, "final_url": url, "page_host": page_host,
                 "status": "OK", "http_status": 200, "title": "t",
                 "started_at": "2026-09-04T00:00:00+00:00", "duration_ms": 5},
        "evidence": evidence, "counts": {}, "warnings": [],
    }


def seed(store, target_id, hosts, csp=()):
    run = store.start_run(1)
    store.record_scan(run, target_id, result(
        f"https://www.{target_id}.co.kr/", f"www.{target_id}.co.kr",
        network={"hosts": list(hosts)}, csp_hosts=list(csp)))


def test_registrable_domain_understands_kr_slds():
    assert mining.registrable_domain("sdk.vendor.co.kr") == "vendor.co.kr"
    assert mining.registrable_domain("cdn.a.b.vendor.io") == "vendor.io"
    assert mining.registrable_domain("wcs.naver.net") == "naver.net"
    assert mining.registrable_domain("api.veritrans.co.jp") == "veritrans.co.jp"
    assert mining.registrable_domain("bat.bing.com") == "bing.com"


def test_own_known_and_noise_hosts_are_excluded(tmp_path):
    registry = reg.load()
    with Store(tmp_path / "radar.db") as s:
        for shop in ("shopa", "shopb"):
            seed(s, shop, [
                f"img.{shop}.co.kr",            # own
                "useinsider.com",               # fingerprinted
                "fonts.googleapis.com",         # noise
                "sdk.newvendor.co.kr",          # the one that should surface
            ])
        rows = mining.unknown_hosts(s, registry)
    domains = [r["domain"] for r in rows]
    assert domains == ["newvendor.co.kr"]
    assert rows[0]["targets"] == 2
    assert rows[0]["hosts"] == ["sdk.newvendor.co.kr"]


def test_min_targets_hides_one_offs(tmp_path):
    registry = reg.load()
    with Store(tmp_path / "radar.db") as s:
        seed(s, "shopa", ["once.example.com"])
        seed(s, "shopb", ["twice.example.io"])
        seed(s, "shopc", ["twice.example.io"])
        rows = mining.unknown_hosts(s, registry, min_targets=2)
    assert [r["domain"] for r in rows] == ["example.io"]


def test_csp_only_sightings_never_count_as_presence(tmp_path):
    registry = reg.load()
    with Store(tmp_path / "radar.db") as s:
        seed(s, "shopa", [], csp=["sdk.declared-only.com"])
        seed(s, "shopb", [], csp=["sdk.declared-only.com"])
        seed(s, "shopc", ["sdk.mixed.com"], csp=["sdk.mixed.com"])
        seed(s, "shopd", ["sdk.mixed.com"])
        rows = mining.unknown_hosts(s, registry, min_targets=2)
    assert [r["domain"] for r in rows] == ["mixed.com"]  # declared-only absent


def test_only_the_latest_scan_speaks(tmp_path):
    """A vendor removed from the site must drop out of the report too."""
    registry = reg.load()
    with Store(tmp_path / "radar.db") as s:
        for shop in ("shopa", "shopb"):
            seed(s, shop, ["sdk.oldvendor.com"])
        for shop in ("shopa", "shopb"):
            seed(s, shop, ["sdk.newvendor.com"])   # later scan, same URL
        rows = mining.unknown_hosts(s, registry)
    assert [r["domain"] for r in rows] == ["newvendor.com"]


def seed_with_anchors(store, target_id, anchor_paths, urls=None):
    from radar.targets import Target, TargetSet
    store.import_watchlist(TargetSet(
        targets=(Target(id=target_id, company=target_id, industry="fashion",
                        urls=tuple(urls or (f"https://www.{target_id}.co.kr/",))),),
        industries={"fashion": "패션"}, source="test"), replace=False)
    run = store.start_run(1)
    payload = result(f"https://www.{target_id}.co.kr/", f"www.{target_id}.co.kr")
    payload["evidence"]["dom"] = {"anchor_paths": list(anchor_paths)}
    store.record_scan(run, target_id, payload, True)


def test_suggest_urls_picks_product_like_paths(tmp_path):
    with Store(tmp_path / "radar.db") as s:
        seed_with_anchors(s, "shopa", ["/event/sale", "/goods/1234", "/about"])
        seed_with_anchors(s, "shopb", ["/about", "/careers"])
        rows = mining.suggest_urls(s)
    assert [r["target_id"] for r in rows] == ["shopa"]
    assert rows[0]["urls"] == ["https://www.shopa.co.kr/event/sale"]


def test_suggest_urls_leaves_multi_url_targets_alone(tmp_path):
    with Store(tmp_path / "radar.db") as s:
        seed_with_anchors(s, "shopa", ["/goods/1"],
                          urls=("https://www.shopa.co.kr/",
                                "https://www.shopa.co.kr/goods/9"))
        assert mining.suggest_urls(s) == []


def test_probable_only_reports_single_signal_vendors(tmp_path):
    with Store(tmp_path / "radar.db") as s:
        seed_with_anchors(s, "shopa", [])
        run = s.start_run(1)
        payload = result("https://www.shopa.co.kr/x", "www.shopa.co.kr")
        scan_id = s.record_scan(run, "shopa", payload, True)
        s.record_detections(scan_id, {"detections": [
            {"id": "half_seen", "name": "Half Seen", "category": "engagement",
             "verdict": "PROBABLE", "score": 1.0, "direct_layers": ["storage"],
             "indirect_layers": [], "indirect_only": False, "match_count": 1,
             "matches": []},
            {"id": "solid", "name": "Solid", "category": "analytics",
             "verdict": "DETECTED", "score": 4.0, "direct_layers": ["network"],
             "indirect_layers": [], "indirect_only": False, "match_count": 2,
             "matches": []},
        ]})
        rows = mining.probable_only(s)
    assert [r["fingerprint_id"] for r in rows] == ["half_seen"]
    assert rows[0]["count"] == 1 and rows[0]["layers"] == ["storage"]


def test_a_vendor_detected_on_any_url_is_not_probable_only(tmp_path):
    """DETECTED on the product page beats PROBABLE on the home page."""
    with Store(tmp_path / "radar.db") as s:
        seed_with_anchors(s, "shopa", [])
        run = s.start_run(1)
        for url, verdict, score in (("https://www.shopa.co.kr/", "PROBABLE", 1.0),
                                    ("https://www.shopa.co.kr/p", "DETECTED", 4.0)):
            payload = result(url, "www.shopa.co.kr")
            scan_id = s.record_scan(run, "shopa", payload, True)
            s.record_detections(scan_id, {"detections": [
                {"id": "vendor", "name": "V", "category": "engagement",
                 "verdict": verdict, "score": score, "direct_layers": ["network"],
                 "indirect_layers": [], "indirect_only": False, "match_count": 1,
                 "matches": []}]})
        assert mining.probable_only(s) == []
