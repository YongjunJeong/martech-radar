"""Integration tests against locally served pages we control."""

from __future__ import annotations

import asyncio
import json


from radar.collector import Collector, ScanConfig
from radar import evidence as ev

# Short waits keep the suite quick; the real defaults are longer.
FAST = dict(settle_ms=1500, interaction_ms=1200, nav_timeout_ms=10_000, hard_timeout_ms=40_000)

_cache: dict[tuple, dict] = {}


def scan(url: str, **overrides) -> dict:
    """Scan once and memoise — several assertions share one browser session."""
    key = (url, tuple(sorted(overrides.items())))
    if key not in _cache:
        config = ScanConfig(**{**FAST, **overrides})

        async def run():
            async with Collector(config) as collector:
                return await collector.scan(url)

        _cache[key] = asyncio.run(run())
    return _cache[key]


# --- the core case: a script the parser never saw ---------------------------


def test_body_injected_sdk_is_observed_end_to_end(site):
    result = scan(f"{site}/dynamic_sdk.html")
    assert result["scan"]["status"] == ev.STATUS_OK
    e = result["evidence"]

    # 1. Network: the SDK request and the event beacon it fired.
    paths = {f"{r['host']}{r['path']}" for r in e["network"]["requests"]}
    assert any(p.endswith("/static/fake-sdk.js") for p in paths)
    assert any(p.endswith("/collect/event") for p in paths)

    # 2. Runtime: proof the SDK actually executed, not merely downloaded.
    assert "FakeVendor" in e["runtime"]["window_keys"]

    # 3. Scripts, including the one injected from <body> at runtime.
    assert any(s.endswith("/static/fake-sdk.js") for s in e["scripts"])

    # 4. Storage, names only.
    assert "fv_uid" in e["storage"]["cookie_names"]
    assert "fv.session" in e["storage"]["local_storage_keys"]
    assert "fv_temp" in e["storage"]["session_storage_keys"]

    # 5. DOM shape.
    assert "fv-widget" in e["dom"]["custom_elements"]
    assert "data-fv-campaign" in e["dom"]["data_attributes"]

    # 6. The vendor naming itself in the console.
    assert any("FakeVendor" in c["text"] for c in e["console"])

    # 7. A container ID pulled out of a query string before it was dropped.
    assert {"kind": "gtm_container", "value": "GTM-TEST123"} in e["identifiers"]


def test_hosts_named_inside_a_bundle_are_found_without_being_called(site):
    """The layer no HTML scanner reaches: endpoints declared but not yet used."""
    result = scan(f"{site}/dynamic_sdk.html")
    referenced = result["evidence"]["referenced_hosts"]
    contacted = result["evidence"]["network"]["hosts"]

    assert "collect.fakevendor-cdn.com" in referenced
    assert "reco.fakevendor-cdn.com" in referenced
    assert "collect.fakevendor-cdn.com" not in contacted


def test_a_two_hop_loader_chain_still_resolves(site):
    result = scan(f"{site}/loader_chain.html")
    e = result["evidence"]
    assert "FakeVendor" in e["runtime"]["window_keys"]
    assert any(s.endswith("/static/fake-sdk.js") for s in e["scripts"])


# --- why the scroll nudge exists --------------------------------------------


def test_scroll_triggered_sdk_needs_the_interaction_pass(site):
    with_nudge = scan(f"{site}/lazy_sdk.html", interact=True)
    without = scan(f"{site}/lazy_sdk.html", interact=False)

    assert "FakeVendor" in with_nudge["evidence"]["runtime"]["window_keys"]
    # Same page, same timeout, no scroll: a false NOT_DETECTED.
    assert "FakeVendor" not in without["evidence"]["runtime"]["window_keys"]


# --- the header layer --------------------------------------------------------


def test_csp_reveals_vendors_the_visit_never_touched(site):
    result = scan(f"{site}/csp_page.html")
    e = result["evidence"]

    for host in ("cdn.useinsider.com", "js.appboycdn.com", "collect.hidden-vendor.io"):
        assert host in e["csp_hosts"], host
        assert host not in e["network"]["hosts"], f"{host} should not have been contacted"

    assert e["headers"].get("x-powered-by") == "TestHarness/1.0"


# --- clean negatives and failures --------------------------------------------


def test_a_plain_page_is_a_quiet_but_valid_observation(site):
    result = scan(f"{site}/plain.html")
    assert result["scan"]["status"] == ev.STATUS_OK
    e = result["evidence"]
    assert "FakeVendor" not in e["runtime"]["window_keys"]
    # Baseline subtraction should leave almost nothing on an empty page.
    assert len(e["runtime"]["window_keys"]) < 30, e["runtime"]["window_keys"]
    assert e["storage"]["cookie_names"] == []


def test_an_unreachable_host_fails_loudly_and_returns(site):
    result = scan("http://127.0.0.1:1/nothing-here")
    assert result["scan"]["status"] in {ev.STATUS_NAV_FAILED, ev.STATUS_TIMEOUT}
    assert result["scan"]["status"] not in ev.TRUSTWORTHY_STATUSES
    assert result["scan"]["error"] or result["warnings"]


def test_one_bad_url_does_not_stop_the_next_scan(site):
    async def run():
        async with Collector(ScanConfig(**FAST)) as collector:
            bad = await collector.scan("http://127.0.0.1:1/dead")
            good = await collector.scan(f"{site}/plain.html")
            return bad, good

    bad, good = asyncio.run(run())
    assert bad["scan"]["status"] != ev.STATUS_OK
    assert good["scan"]["status"] == ev.STATUS_OK


# --- privacy -----------------------------------------------------------------


def test_no_values_leak_into_the_result(site):
    result = scan(f"{site}/dynamic_sdk.html")
    dumped = json.dumps(result, ensure_ascii=False)

    assert "fv_uid=abc123" not in dumped   # cookie value
    assert "uid=abc123" not in dumped      # query string
    assert "?" not in json.dumps(result["evidence"]["network"]["requests"])
    # Storage keys are present, their contents are not.
    assert "fv.session" in dumped
