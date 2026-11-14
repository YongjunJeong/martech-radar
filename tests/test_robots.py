"""robots.txt and politeness.

The point of this module is that the tool gets *less* access, deliberately.
So the tests check that a disallowed page is genuinely not fetched, and —
just as importantly — that the skip can never be mistaken for a finding.
"""

from __future__ import annotations

import asyncio
import contextlib
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


def test_per_job_override_does_not_leak_sequentially_or_concurrently(site, tmp_path):
    from radar.batch import Job
    from radar.worker import drain_queue
    from radar.targets import Target, TargetSet

    async def run(concurrency):
        with Store(tmp_path / f'{concurrency}.db') as store:
            store.import_watchlist(TargetSet(tuple(
                Target(id=tid, company=tid, industry='test',
                       urls=(f'{site}/forbidden.html',), robots_policy=policy,
                       robots_note='test exception' if policy == 'override' else None)
                for tid, policy in [('exception', 'override'), ('normal', 'respect')]
            ), {'test': 'test'}, 'test'))
            run_id = store.queue_run([Job(tid, tid, f'{site}/forbidden.html')
                                      for tid in ('exception', 'normal')])
            config = ScanConfig(**FAST, respect_robots=True)
            await drain_queue(store, reg.load(), config, run_id=run_id,
                              concurrency=concurrency)
            rows = store.conn.execute('SELECT id,target_id,status FROM scan').fetchall()
            assert {r['target_id']: r['status'] for r in rows} == {
                'exception': ev.STATUS_OK, 'normal': ev.STATUS_SKIPPED_BY_ROBOTS}
            for row in rows:
                evidence = store.load_evidence(row['id'])
                assert evidence['scan']['config']['respect_robots'] == (row['target_id'] == 'normal')
            assert config.respect_robots is True

    asyncio.run(run(1))
    asyncio.run(run(2))


def test_concurrent_same_origin_shares_fetch_and_retains_path_rules():
    async def run():
        calls = []
        async def fetch(url):
            calls.append(url)
            await asyncio.sleep(.05)
            return 200, 'User-agent: *\nDisallow: /private\nCrawl-delay: 2\n'
        cache = RobotsCache()
        decisions = await asyncio.gather(*(cache.check('https://site.test/' + path, fetch)
                                          for path in ('public', 'private', 'other')))
        assert len(calls) == 1
        assert [d.allowed for d in decisions] == [True, False, True]
        assert all(d.crawl_delay == 2 for d in decisions)
    asyncio.run(run())


def test_robots_fetch_lock_does_not_serialize_different_origins():
    async def run():
        both_started = asyncio.Event()
        calls = []
        async def fetch(url):
            calls.append(url)
            if len(calls) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=1)
            return 200, 'User-agent: *\nDisallow: /private\n'
        cache = RobotsCache()
        result = await asyncio.gather(*(cache.check(url, fetch) for url in
                                      ('https://one.test/private', 'https://two.test/private')))
        assert all(not d.allowed for d in result)
    asyncio.run(run())


def test_cancelled_robots_fetch_releases_same_origin_lock():
    async def run():
        started = asyncio.Event()
        async def hanging(url):
            started.set()
            await asyncio.Event().wait()
        async def available(url):
            return 200, 'User-agent: *\nDisallow: /private\n'
        cache = RobotsCache()
        task = asyncio.create_task(cache.check('https://site.test/private', hanging))
        await started.wait()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        decision = await asyncio.wait_for(cache.check('https://site.test/private', available), timeout=1)
        assert not decision.allowed
    asyncio.run(run())
