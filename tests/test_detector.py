"""Verdict rules.

The commercially dangerous mistake is not missing a vendor — it is
promoting weak evidence to DETECTED, because change detection reads a DETECTED that
later disappears as a churn event and puts it in front of a rep. So most of
these tests assert that something is *not* DETECTED.
"""

from __future__ import annotations

import asyncio

import pytest

from radar import detector as det
from radar import evidence as ev
from radar import registry as reg
from radar.collector import Collector, ScanConfig


@pytest.fixture(scope="module")
def registry():
    return reg.load()


def make_result(status: str = ev.STATUS_OK, **evidence) -> dict:
    return {
        "schema_version": 1,
        "scan": {"url": "https://x.test/", "page_host": "x.test", "status": status},
        "evidence": evidence,
    }


def verdict(result: dict, registry, fingerprint_id: str) -> str:
    for d in det.detect(result, registry)["detections"]:
        if d["id"] == fingerprint_id:
            return d["verdict"]
    return det.VERDICT_NOT_DETECTED


# --- the gate ---------------------------------------------------------------


def test_untrustworthy_scan_is_never_judged(registry):
    for status in (ev.STATUS_BLOCKED, ev.STATUS_TIMEOUT, ev.STATUS_NAV_FAILED):
        result = make_result(status, network={"hosts": ["cdn.useinsider.com"]})
        report = det.detect(result, registry)
        assert report["judged"] is False
        assert report["detections"] == []


# --- how layers combine -----------------------------------------------------


def test_one_strong_direct_layer_is_enough(registry):
    result = make_result(network={"hosts": ["acmekr.api.useinsider.com"]})
    assert verdict(result, registry, "insider") == det.VERDICT_DETECTED


def test_one_weak_direct_layer_stays_probable(registry):
    # `dataLayer` is a GTM convention, not proof GTM is installed.
    result = make_result(runtime={"window_keys": ["dataLayer"]})
    assert verdict(result, registry, "google_tag_manager") == det.VERDICT_PROBABLE


def test_two_weak_direct_layers_reach_detected(registry):
    result = make_result(
        runtime={"window_keys": ["analytics"]},
        storage={"cookie_names": ["ajs_anonymous_id"]},
    )
    assert verdict(result, registry, "segment") == det.VERDICT_DETECTED


def test_csp_mention_alone_never_reaches_detected(registry):
    result = make_result(csp_hosts=["cdn.useinsider.com", "js.appboycdn.com"])
    report = det.detect(result, registry)
    hits = {d["id"]: d for d in report["detections"]}
    for vendor in ("insider", "braze"):
        assert hits[vendor]["verdict"] == det.VERDICT_PROBABLE
        assert hits[vendor]["indirect_only"] is True
        assert hits[vendor]["score"] == 0.0


def test_named_in_bundle_but_never_contacted_stays_probable(registry):
    result = make_result(referenced_hosts=["sdk.braze.eu"])
    hits = {d["id"]: d for d in det.detect(result, registry)["detections"]}
    assert hits["braze"]["verdict"] == det.VERDICT_PROBABLE
    assert hits["braze"]["indirect_layers"] == ["source"]


def test_indirect_evidence_cannot_top_up_a_weak_direct_hit(registry):
    # weak runtime (1.0) + strong source + strong declared must still be
    # PROBABLE: the browser never contacted the vendor.
    result = make_result(
        runtime={"window_keys": ["dataLayer"]},
        referenced_hosts=["www.googletagmanager.com"],
        csp_hosts=["www.googletagmanager.com"],
    )
    assert verdict(result, registry, "google_tag_manager") == det.VERDICT_PROBABLE


# --- matching semantics -----------------------------------------------------


def test_host_matching_is_domain_scoped_not_substring(registry):
    result = make_result(network={"hosts": ["notuseinsider.com", "useinsider.com.evil.test"]})
    assert verdict(result, registry, "insider") == det.VERDICT_NOT_DETECTED


def test_subdomains_do_match(registry):
    result = make_result(network={"hosts": ["a.b.useinsider.com"]})
    assert verdict(result, registry, "insider") == det.VERDICT_DETECTED


def test_window_matching_is_case_sensitive(registry):
    # A news site with window.insider (lower-case) is not a customer.
    assert verdict(make_result(runtime={"window_keys": ["insider"]}), registry, "insider") \
        == det.VERDICT_NOT_DETECTED
    assert verdict(make_result(runtime={"window_keys": ["Insider"]}), registry, "insider") \
        == det.VERDICT_DETECTED


def test_a_layer_scores_once_no_matter_how_many_patterns_hit(registry):
    one = make_result(network={"hosts": ["useinsider.com"]})
    many = make_result(network={
        "hosts": ["useinsider.com", "falcon.useinsider.com", "insidercdn.com"],
        "third_party_domains": ["useinsider.com"],
    })
    scores = [
        next(d["score"] for d in det.detect(r, registry)["detections"] if d["id"] == "insider")
        for r in (one, many)
    ]
    assert scores[0] == scores[1] == 2.0


def test_matches_are_recorded_so_a_verdict_can_be_explained(registry):
    result = make_result(
        network={"hosts": ["acmekr.api.useinsider.com"]},
        runtime={"window_keys": ["__INSIDER_SCRIPT_VERSION_acmekr__"]},
    )
    hit = next(d for d in det.detect(result, registry)["detections"] if d["id"] == "insider")
    assert hit["verdict"] == det.VERDICT_DETECTED
    assert {m["layer"] for m in hit["matches"]} == {"network", "runtime"}
    # The partner slug rides along in the evidence — useful, so it must survive.
    assert any("acmekr" in m["value"] for m in hit["matches"])


def test_nested_globals_are_reachable(registry):
    # adobe.target lives one level down, not on window itself.
    result = make_result(runtime={"nested_keys": {"adobe": ["target", "optIn"]}})
    assert verdict(result, registry, "adobe_target") == det.VERDICT_DETECTED


# --- end to end, real browser ----------------------------------------------


def test_csp_header_surfaces_vendors_the_visit_never_contacts(site):
    """The fixture page names three vendors in its CSP and loads none of them."""
    config = ScanConfig(settle_ms=1200, interaction_ms=1000,
                        nav_timeout_ms=10_000, hard_timeout_ms=40_000)

    async def run():
        async with Collector(config) as collector:
            return await collector.scan(f"{site}/csp_page.html")

    report = det.detect(asyncio.run(run()), reg.load())
    assert report["judged"] is True
    hits = {d["id"]: d for d in report["detections"]}
    for vendor in ("insider", "braze", "google_tag_manager"):
        assert hits[vendor]["verdict"] == det.VERDICT_PROBABLE, vendor
        assert hits[vendor]["indirect_only"] is True, vendor
