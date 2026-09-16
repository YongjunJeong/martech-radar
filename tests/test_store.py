"""Persistence and the retroactive re-classification it exists for."""

from __future__ import annotations

import json

import pytest

from radar import detector as det
from radar import evidence as ev
from radar import registry as reg
from radar.batch import redetect
from radar.store import Store, StoreError
from radar.targets import Target, TargetSet


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "radar.db") as s:
        yield s


@pytest.fixture(scope="module")
def registry():
    return reg.load()


def watchlist(*targets: Target) -> TargetSet:
    return TargetSet(targets=tuple(targets), industries={"fashion": "패션"}, source="test")


SHOP = Target(id="shop", company="가나다몰", industry="fashion",
                 urls=("https://shop.example/",))


def result(status: str = ev.STATUS_OK, url: str = "https://shop.example/", **evidence) -> dict:
    return {
        "schema_version": 1,
        "scan": {"url": url, "final_url": url, "page_host": "shop.example",
                 "status": status, "http_status": 200, "title": "Shop",
                 "started_at": "2026-08-22T00:00:00+00:00", "duration_ms": 1234},
        "evidence": evidence,
        "counts": {"requests": 1},
        "warnings": [],
    }


# --- targets ---------------------------------------------------------------


def test_importing_twice_updates_rather_than_duplicates(store):
    store.import_watchlist(watchlist(SHOP))
    store.import_watchlist(watchlist(Target(
        id="shop", company="가나다몰 주식회사", industry="fashion",
        urls=("https://shop.example/", "https://shop.example/category/001"))))
    rows = store.targets()
    assert len(rows) == 1
    assert rows[0]["company"] == "가나다몰 주식회사"
    assert len(json.loads(rows[0]["urls_json"])) == 2


def test_a_target_dropped_from_the_watchlist_keeps_its_history(store):
    store.import_watchlist(watchlist(SHOP))
    run = store.start_run(1)
    store.record_scan(run, "shop", result())

    store.import_watchlist(watchlist(), replace=True)   # gone from the file

    rows = store.targets()
    assert len(rows) == 1, "history must survive removal from the list"
    assert rows[0]["enabled"] == 0, "and it must stop being scanned"
    assert len(store.scans_for("shop")) == 1


def test_an_import_without_replace_leaves_others_alone(store):
    store.import_watchlist(watchlist(SHOP))
    store.import_watchlist(watchlist(Target(
        id="other", company="다른몰", industry="fashion", urls=("https://o.test/",))))
    assert {r["id"] for r in store.targets()} == {"shop", "other"}
    assert all(r["enabled"] for r in store.targets())


def test_industries_come_across_with_the_targets(store):
    store.import_watchlist(TargetSet(
        targets=(SHOP,), industries={"fashion": "패션"}, source="test"))
    assert [r["code"] for r in store.industries()] == ["fashion"]
    assert store.industries()[0]["label"] == "패션"


# --- evidence round trip ---------------------------------------------------


def test_evidence_survives_the_round_trip(store):
    run = store.start_run(1)
    original = result(network={"hosts": ["sdk.iad-03.braze.com"]},
                      runtime={"window_keys": ["braze"]})
    scan_id = store.record_scan(run, "shop", original)
    restored = store.load_evidence(scan_id)
    assert restored["evidence"] == original["evidence"]
    assert restored["scan"]["status"] == ev.STATUS_OK


def test_evidence_can_be_declined(store):
    run = store.start_run(1)
    scan_id = store.record_scan(run, "shop", result(network={"hosts": ["x.test"]}),
                                keep_evidence=False)
    assert store.load_evidence(scan_id) is None


def test_stored_evidence_is_compressed(store):
    run = store.start_run(1)
    big = result(runtime={"window_keys": [f"key_{i}" for i in range(5000)]})
    scan_id = store.record_scan(run, "shop", big)
    raw = len(json.dumps(big).encode())
    stored = len(store.conn.execute(
        "SELECT evidence_gz FROM scan WHERE id=?", (scan_id,)).fetchone()[0])
    assert stored < raw / 3


# --- the point of keeping evidence -----------------------------------------


def test_a_new_fingerprint_reclassifies_history_without_rescanning(store, registry, tmp_path):
    run = store.start_run(1)
    scan_id = store.record_scan(run, "shop", result(
        network={"hosts": ["cdn.brand-new-vendor.test"]}))
    store.record_detections(scan_id, det.detect(store.load_evidence(scan_id), registry))
    assert not [d for d in store.detections_for(scan_id)
                if d["fingerprint_id"] == "brand_new_vendor"]

    (tmp_path / "new.yaml").write_text(
        "category: engagement\nfingerprints:\n  - id: brand_new_vendor\n"
        "    name: Brand New Vendor\n    signals:\n      hosts: [brand-new-vendor.test]\n",
        encoding="utf-8")
    stats = redetect(store, reg.load(tmp_path))

    assert stats["rewritten"] == 1
    hit = [d for d in store.detections_for(scan_id)
           if d["fingerprint_id"] == "brand_new_vendor"]
    assert hit and hit[0]["verdict"] == det.VERDICT_DETECTED


def test_redetect_is_idempotent(store, registry):
    run = store.start_run(1)
    scan_id = store.record_scan(run, "shop", result(
        network={"hosts": ["sdk.iad-03.braze.com"]},
        storage={"local_storage_keys": ["ab.storage.deviceId.abc"]}))

    def snapshot():
        redetect(store, registry)
        return [tuple(dict(r).items()) for r in store.detections_for(scan_id)]

    first, second, third = snapshot(), snapshot(), snapshot()
    assert first == second == third
    assert any(dict(r)["fingerprint_id"] == "braze" for r in store.detections_for(scan_id))


def test_scans_without_evidence_are_reported_not_silently_skipped(store, registry):
    run = store.start_run(1)
    store.record_scan(run, "shop", result(network={"hosts": ["x.test"]}),
                      keep_evidence=False)
    stats = redetect(store, registry)
    assert stats["skipped_no_evidence"] == 1
    assert stats["rewritten"] == 0


# --- aggregation ------------------------------------------------------------


def test_latest_scan_wins_per_url_not_per_target(store, registry):
    run = store.start_run(1)
    home = "https://shop.example/"
    pdp = "https://shop.example/category/001"
    a = store.record_scan(run, "shop", result(url=home, network={"hosts": ["hotjar.com"]}))
    b = store.record_scan(run, "shop", result(url=pdp,
                                                 network={"hosts": ["sdk.iad-03.braze.com"]}))
    for scan_id in (a, b):
        store.record_detections(scan_id, det.detect(store.load_evidence(scan_id), registry))

    stack = {row["fingerprint_id"] for row in store.stack_of("shop")}
    assert {"hotjar", "braze"} <= stack, "a vendor seen only on the PDP still counts"


def test_a_blocked_rescan_does_not_erase_a_good_stack(store, registry):
    run = store.start_run(1)
    good = store.record_scan(run, "shop", result(
        network={"hosts": ["sdk.iad-03.braze.com"]}))
    store.record_detections(good, det.detect(store.load_evidence(good), registry))

    later = store.start_run(1)
    store.record_scan(later, "shop", result(status=ev.STATUS_BLOCKED))

    stack = {row["fingerprint_id"] for row in store.stack_of("shop")}
    assert "braze" in stack


def test_vendor_counts_count_companies_not_scans(store, registry):
    run = store.start_run(2)
    for target_id, url in (("shop", "https://shop.example/"),
                           ("shop", "https://shop.example/category/001"),
                           ("other_shop", "https://other.example/")):
        scan_id = store.record_scan(run, target_id, result(
            url=url, network={"hosts": ["sdk.iad-03.braze.com"]}))
        store.record_detections(scan_id, det.detect(store.load_evidence(scan_id), registry))

    braze = next(v for v in store.vendor_counts() if v["fingerprint_id"] == "braze")
    assert braze["targets"] == 2


def test_schema_version_mismatch_refuses_to_open(store, tmp_path):
    store.conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    store.conn.commit()
    with pytest.raises(RuntimeError, match="schema v99"):
        Store(store.path)


def test_coverage_separates_never_judged_from_nothing_found(store):
    run = store.start_run(2)
    store.record_scan(run, "seen", result())
    store.record_scan(run, "walled", result(status=ev.STATUS_BLOCKED))
    report = store.coverage()
    assert report["seen"]["judged"] is True
    assert report["walled"]["judged"] is False
    assert report["walled"]["last_status"] == ev.STATUS_BLOCKED
    assert report["walled"]["good_scans"] == 0


def test_a_scan_can_be_regraded_under_a_newer_status_rule(store, registry):
    run = store.start_run(1)
    thin = result(network={"hosts": []})
    thin["scan"]["title"] = "시스템 점검 안내"
    thin["counts"] = {"requests": 3, "scripts": 0, "window_keys": 0,
                      "cookie_names": 0, "csp_hosts": 0, "identifiers": 0,
                      "third_party_domains": 0}
    scan_id = store.record_scan(run, "shop", thin)
    assert store.scans_for("shop")[0]["status"] == ev.STATUS_OK

    stats = redetect(store, registry)

    assert stats["restatused"] == 1
    row = store.scans_for("shop")[0]
    assert row["status"] == ev.STATUS_THIN
    assert "thin:" in row["block_marker"]
    # The blob must agree with the row, or the next redetect flips it back.
    assert store.load_evidence(scan_id)["scan"]["status"] == ev.STATUS_THIN


def test_a_corrupt_database_reads_as_a_sentence_not_a_traceback(tmp_path):
    # Cron reads this file. A traceback in a log nobody watches is a bug.
    bad = tmp_path / "corrupt.db"
    bad.write_bytes(b"this is not a database" * 20)
    with pytest.raises(StoreError, match="not a usable database"):
        Store(bad)


def test_redetect_reapplies_current_privacy_rules_to_old_evidence(store, registry):
    run = store.start_run(1)
    raw = result(storage={"local_storage_keys":
                          ["ab.storage.deviceId.b9a58994-a795-4f4d-b145-1023fd6a1e11"]})
    scan_id = store.record_scan(run, "shop", raw)
    assert "b9a58994" in json.dumps(store.load_evidence(scan_id))

    stats = redetect(store, registry)

    assert stats["scrubbed"] == 1
    cleaned = json.dumps(store.load_evidence(scan_id))
    assert "b9a58994" not in cleaned
    assert "ab.storage.deviceId.{uuid}" in cleaned


def test_a_watchlist_survives_a_round_trip_through_yaml(store, tmp_path):
    """The database is the working copy; YAML is how it travels."""
    from radar import targets as tgt

    store.import_watchlist(TargetSet(
        targets=(
            Target(id="a", company="가몰", company_en="A Mall", industry="fashion",
                   tier="enterprise", note="주의: 봇월", urls=("https://a.test/", "https://a.test/x")),
            Target(id="b", company="나몰", industry="fashion", enabled=False,
                   urls=("https://b.test/",)),
        ),
        industries={"fashion": "패션"}, source="test"))

    exported = tmp_path / "out.yaml"
    exported.write_text(tgt.to_yaml(store.industries(), store.targets()), encoding="utf-8")
    reloaded = tgt.load(exported)

    assert {t.id for t in reloaded} == {"a", "b"}
    first = reloaded.get("a")
    assert first.company == "가몰" and first.company_en == "A Mall"
    assert first.tier == "enterprise" and first.note == "주의: 봇월"
    assert first.urls == ("https://a.test/", "https://a.test/x")
    assert reloaded.get("b").enabled is False
    assert reloaded.industry_label("fashion") == "패션"


def test_pausing_a_target_takes_it_out_of_the_next_batch(store, registry):
    from radar.batch import jobs_for

    store.import_watchlist(watchlist(SHOP))
    assert [j.target_id for j in jobs_for(store)] == ["shop"]

    assert store.set_target_enabled("shop", False) is True
    assert jobs_for(store) == []
    assert store.set_target_enabled("nobody", False) is False


def test_a_target_can_be_added_without_a_file(store):
    store.upsert_target(id="new", company="새몰", industry="fashion",
                        urls=["https://new.test/"])
    row = store.target("new")
    assert row["company"] == "새몰"
    assert json.loads(row["urls_json"]) == ["https://new.test/"]


def _record_with_counts(store, registry, requests, evidence, target="acme"):
    run = store.start_run(1)
    payload = {"schema_version": 1,
               "scan": {"url": f"https://{target}.test/", "page_host": f"{target}.test",
                        "status": ev.STATUS_OK, "started_at": "2026-08-22T00:00:00+00:00"},
               "evidence": evidence, "counts": {"requests": requests}, "warnings": []}
    scan_id = store.record_scan(run, target, payload)
    store.record_detections(scan_id, det.detect(payload, registry))
    return scan_id


BUSY_EVIDENCE = {"network": {"hosts": ["sdk.iad-03.braze.com", "static.hotjar.com",
                                       "connect.facebook.net", "static.criteo.net"]}}
THIN_EVIDENCE = {"network": {"hosts": ["connect.facebook.net"]}}


def test_a_half_loaded_page_is_not_a_page_that_lost_its_vendors(store, registry):
    """The failure that made this necessary.

    A real site gave 245-251 requests on six visits, then 133 on the
    seventh — and two vendors duly went missing. The page was partial; the
    stack was not.
    """
    for _ in range(4):
        _record_with_counts(store, registry, 250, BUSY_EVIDENCE)
    scan_id = _record_with_counts(store, registry, 120, THIN_EVIDENCE)

    marker = store.flag_partial_scan(scan_id)

    assert marker is not None and "partial:" in marker
    assert store.scans_for("acme")[0]["status"] == ev.STATUS_PARTIAL
    assert ev.STATUS_PARTIAL not in ev.TRUSTWORTHY_STATUSES


def test_fewer_requests_but_the_same_vendors_is_fine(store, registry):
    # A lighter page that still shows the same stack is a lighter page.
    for _ in range(4):
        _record_with_counts(store, registry, 250, BUSY_EVIDENCE)
    scan_id = _record_with_counts(store, registry, 100, BUSY_EVIDENCE)
    assert store.flag_partial_scan(scan_id) is None


def test_a_genuine_drop_needs_history_to_judge_against(store, registry):
    # With one prior visit there is no "usual" to compare to, so nothing is
    # downgraded — better to report a change we are unsure of than to hide it.
    _record_with_counts(store, registry, 250, BUSY_EVIDENCE)
    scan_id = _record_with_counts(store, registry, 20, THIN_EVIDENCE)
    assert store.flag_partial_scan(scan_id) is None


def test_a_partial_scan_cannot_remove_a_vendor(store, registry):
    from radar import signals as sig

    for _ in range(4):
        _record_with_counts(store, registry, 250, BUSY_EVIDENCE)
    scan_id = _record_with_counts(store, registry, 100, THIN_EVIDENCE)
    store.flag_partial_scan(scan_id)

    states = {c.fingerprint_id: c.state for c in sig.changes_for(store, "acme")}
    assert states["braze"] == sig.STATE_STABLE


def test_a_partial_downgrade_survives_redetect(store, registry):
    """The review finding: the row said PARTIAL, the blob still said OK.

    `redetect` judges from the blob, so a removal hidden by the downgrade
    came straight back the next time anyone re-judged the history.
    """
    for _ in range(4):
        _record_with_counts(store, registry, 250, BUSY_EVIDENCE)
    scan_id = _record_with_counts(store, registry, 100, THIN_EVIDENCE)
    assert store.flag_partial_scan(scan_id) is not None

    assert store.load_evidence(scan_id)["scan"]["status"] == ev.STATUS_PARTIAL, \
        "the blob must agree with the row"
    redetect(store, registry)
    assert store.scans_for("acme")[0]["status"] == ev.STATUS_PARTIAL
    assert store.detections_for(scan_id) == [], \
        "a partial scan must not be re-judged into a stack"


def test_redetect_reports_what_a_fingerprint_change_did(store, registry, tmp_path):
    """`redetect --diff`: the question worth answering after every re-judge."""
    run = store.start_run(1)
    scan_id = store.record_scan(run, "shop", result(
        network={"hosts": ["cdn.brand-new-vendor.test"]}))
    store.record_detections(scan_id, det.detect(store.load_evidence(scan_id), registry))

    (tmp_path / "new.yaml").write_text(
        "category: engagement\nfingerprints:\n  - id: brand_new_vendor\n"
        "    name: Brand New Vendor\n    signals:\n      hosts: [brand-new-vendor.test]\n",
        encoding="utf-8")
    stats = redetect(store, reg.load(tmp_path))

    assert stats["gained"] == [(scan_id, "brand_new_vendor")]
    assert stats["lost"] == []
    assert stats["fingerprints_hash"] == reg.load(tmp_path).content_hash


def test_the_fingerprint_hash_sees_a_one_line_edit(tmp_path):
    # `fingerprint_count` cannot tell "75 vendors, one pattern changed" from
    # "nothing changed". The hash exists so that the run history can.
    f = tmp_path / "a.yaml"
    f.write_text("category: t\nfingerprints:\n  - id: x\n    name: X\n    signals:\n      hosts: [x.test]\n")
    before = reg.load(tmp_path).content_hash
    f.write_text("category: t\nfingerprints:\n  - id: x\n    name: X\n    signals:\n      hosts: [x.test, y.test]\n")
    assert reg.load(tmp_path).content_hash != before


def test_backup_is_a_usable_copy_and_old_snapshots_are_pruned(tmp_path):
    from radar.targets import Target, TargetSet
    with Store(tmp_path / "radar.db") as s:
        s.import_watchlist(TargetSet(
            targets=(Target(id="acme", company="에이스몰", industry="fashion",
                            urls=("https://acme.test/",)),),
            industries={"fashion": "패션"}, source="test"))
        folder = tmp_path / "backups"
        for stamp in ("2026-09-01-0245", "2026-09-02-0245"):
            s.backup(folder / f"radar-{stamp}.db")
        latest = s.backup(folder / "radar-2026-09-03-0245.db")
    assert not list(folder.glob("*.part")), "no half-written snapshot may remain"
    with Store(latest) as copy:
        assert [r["id"] for r in copy.targets()] == ["acme"]

    from radar import workspace
    from radar.cli import main
    try:
        assert main(["--home", str(tmp_path), "backup", "--keep", "2"]) == 0
    finally:
        workspace.set_home(None)   # --home pins the process; do not leak it
    kept = sorted(p.name for p in folder.glob("radar-*.db"))
    assert len(kept) == 2 and "radar-2026-09-01-0245.db" not in kept
