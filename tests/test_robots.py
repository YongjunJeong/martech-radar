"""robots.txt and politeness.

The point of this module is that the tool gets *less* access, deliberately.
So the tests check that a disallowed page is genuinely not fetched, and —
just as importantly — that the skip can never be mistaken for a finding.
"""

from __future__ import annotations

import asyncio
import time


from radar import detector as det
from radar import evidence as ev
from radar import registry as reg
from radar import signals as sig
from radar.collector import Collector, ScanConfig
from radar.robots import RobotsCache
from radar.store import Store

FAST = dict(settle_ms=1000, interaction_ms=800, nav_timeout_ms=8000,
            hard_timeout_ms=30_000, per_site_delay_ms=0)


def scan(url: str, **overrides) -> dict:
    config = ScanConfig(**{**FAST, **overrides})

    async def run():
        async with Collector(config) as collector:
            return await collector.scan(url)

    return asyncio.run(run())


# --- the decision itself ----------------------------------------------------


def test_a_disallowed_page_is_not_visited(site):
    result = scan(f"{site}/forbidden.html")
    assert result["scan"]["status"] == ev.STATUS_SKIPPED_BY_ROBOTS
    assert "robots.txt" in (result["scan"]["block_marker"] or "")
    # Nothing was collected, because nothing was fetched.
    assert not result["evidence"]
    assert not result["counts"]


def test_an_allowed_page_is_visited_normally(site):
    result = scan(f"{site}/dynamic_sdk.html")
    assert result["scan"]["status"] == ev.STATUS_OK
    assert "FakeVendor" in result["evidence"]["runtime"]["window_keys"]


def test_the_check_can_be_turned_off_deliberately(site):
    result = scan(f"{site}/forbidden.html", respect_robots=False)
    assert result["scan"]["status"] == ev.STATUS_OK
    # ...and the decision is recorded in the result either way.
    assert result["scan"]["config"]["respect_robots"] is False


# --- and it must never look like a finding ---------------------------------


def test_a_robots_skip_is_not_a_trustworthy_observation():
    assert ev.STATUS_SKIPPED_BY_ROBOTS not in ev.TRUSTWORTHY_STATUSES


def test_a_robots_skip_cannot_remove_a_vendor(tmp_path):
    registry = reg.load()
    with Store(tmp_path / "radar.db") as store:
        def record(evidence, status=ev.STATUS_OK):
            run = store.start_run(1)
            result = {"schema_version": 1,
                      "scan": {"url": "https://acme.test/", "page_host": "acme.test",
                               "status": status, "started_at": "2026-08-22T00:00:00+00:00"},
                      "evidence": evidence, "counts": {}, "warnings": []}
            scan_id = store.record_scan(run, "acme", result)
            store.record_detections(scan_id, det.detect(result, registry))

        record({"network": {"hosts": ["sdk.iad-03.braze.com"]}})
        record({}, status=ev.STATUS_SKIPPED_BY_ROBOTS)

        states = {c.fingerprint_id: c.state for c in sig.changes_for(store, "acme")}
        assert states["braze"] == sig.STATE_BASELINE, \
            "a site asking us not to look is not a site that dropped a vendor"


# --- politeness -------------------------------------------------------------


def test_the_same_site_is_not_hit_twice_in_a_row_without_a_gap(site):
    config = ScanConfig(**{**FAST, "per_site_delay_ms": 700})

    async def run():
        async with Collector(config) as collector:
            started = time.monotonic()
            await collector.scan(f"{site}/plain.html")
            await collector.scan(f"{site}/plain.html")
            return time.monotonic() - started

    assert asyncio.run(run()) >= 0.7


def test_robots_is_fetched_once_per_site(site):
    calls: list[str] = []

    async def counting_fetch(url):
        calls.append(url)
        return 200, "User-agent: *\nDisallow: /nope\n"

    async def run():
        cache = RobotsCache()
        for path in ("/a", "/b", "/c"):
            await cache.check(f"{site}{path}", counting_fetch)

    asyncio.run(run())
    assert len(calls) == 1, "asking for the same robots.txt repeatedly is the rudeness"


def test_an_unreachable_robots_file_is_not_read_as_a_prohibition():
    async def broken(url):
        raise ConnectionError("nope")

    async def run():
        return await RobotsCache().check("https://x.test/a", broken)

    decision = asyncio.run(run())
    assert decision.allowed is True
    assert "unreachable" in decision.reason


def test_a_protected_robots_file_is_treated_as_absent():
    async def forbidden(url):
        return 403, None

    decision = asyncio.run(RobotsCache().check("https://x.test/a", forbidden))
    assert decision.allowed is True


# --- remembering what a site said ------------------------------------------


def test_a_site_that_stops_serving_robots_is_still_honoured(tmp_path):
    """The instability that made this necessary.

    The same site answers with its rules one week and a 403 the next. A tool
    that forgets flips between honouring and ignoring them — rude, and wrong
    for a system whose whole job is comparing one week to another.
    """
    store = Store(tmp_path / "radar.db")
    cache = RobotsCache(memory=store)

    async def serves(url):
        return 200, "User-agent: *\nDisallow: /private\n"

    async def refuses(url):
        return 403, None

    first = asyncio.run(cache.check("https://x.test/private/a", serves))
    assert first.allowed is False

    # A fresh process, and the site is now refusing to hand over the file.
    forgetful = RobotsCache(memory=store)
    second = asyncio.run(forgetful.check("https://x.test/private/a", refuses))
    assert second.allowed is False
    assert "remembered" in second.reason
    store.close()


def test_without_memory_an_unreadable_file_still_allows(tmp_path):
    async def refuses(url):
        return 403, None

    decision = asyncio.run(RobotsCache().check("https://y.test/a", refuses))
    assert decision.allowed is True


def test_a_successful_fetch_replaces_what_we_remembered(tmp_path):
    store = Store(tmp_path / "radar.db")

    async def strict(url):
        return 200, "User-agent: *\nDisallow: /\n"

    async def relaxed(url):
        return 200, "User-agent: *\nDisallow: /admin\n"

    assert asyncio.run(RobotsCache(memory=store).check("https://z.test/x", strict)).allowed is False
    assert asyncio.run(RobotsCache(memory=store).check("https://z.test/x", relaxed)).allowed is True
    store.close()
