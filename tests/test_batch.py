"""Batch orchestration against locally served pages.

The batch is the part that runs unattended, so what matters is that a bad
site cannot take the run down with it and that failures are written down
rather than dropped.
"""

from __future__ import annotations

import asyncio

import pytest

from radar import evidence as ev
from radar import registry as reg
from radar.batch import run_batch
from radar.collector import ScanConfig
from radar.store import Store
from radar.targets import Target, TargetSet

FAST = ScanConfig(settle_ms=1200, interaction_ms=800,
                  nav_timeout_ms=8000, hard_timeout_ms=30_000)


def watchlist(*targets: Target) -> TargetSet:
    return TargetSet(targets=tuple(targets),
                     industries={"fashion": "패션", "beauty": "뷰티"},
                     source="test")


@pytest.fixture(scope="module")
def registry():
    return reg.load()


def batch(watch, store, registry, **kwargs):
    """Import the watchlist, then scan what the database now holds."""
    store.import_watchlist(watch)
    return asyncio.run(run_batch(store, registry, scan_config=FAST, **kwargs))


def test_a_run_records_every_url_and_survives_a_dead_host(tmp_path, site, registry):
    watch = watchlist(
        Target(id="good", company="정상몰", industry="fashion",
               urls=(f"{site}/dynamic_sdk.html", f"{site}/csp_page.html")),
        # Nothing listens here; the batch must record it and carry on.
        Target(id="dead", company="죽은몰", industry="beauty",
               urls=("http://127.0.0.1:9/",)),
    )
    with Store(tmp_path / "radar.db") as store:
        report = batch(watch, store, registry, concurrency=2)

        assert len(report.outcomes) == 3
        assert len(report.failed) == 1
        assert report.failed[0].target_id == "dead"

        rows = store.conn.execute("SELECT target_id, status FROM scan").fetchall()
        assert len(rows) == 3, "the failed scan must be stored, not dropped"
        statuses = {r["target_id"]: r["status"] for r in rows}
        assert statuses["dead"] != ev.STATUS_OK

        run = store.runs()[0]
        assert run["finished_at"] is not None
        assert run["fingerprint_count"] == len(registry)


def test_disabled_targets_are_not_visited(tmp_path, site, registry):
    watch = watchlist(
        Target(id="live", company="살아있는몰", industry="fashion",
               urls=(f"{site}/plain.html",)),
        Target(id="paused", company="멈춘몰", industry="beauty", enabled=False,
               urls=(f"{site}/dynamic_sdk.html",)),
    )
    with Store(tmp_path / "radar.db") as store:
        report = batch(watch, store, registry, concurrency=2)
        assert {o.target_id for o in report.outcomes} == {"live"}
        # ...but the paused company is still on file.
        assert {r["id"] for r in store.targets()} == {"live", "paused"}


def test_detections_land_with_the_scan(tmp_path, site, registry):
    watch = watchlist(Target(id="csp", company="정책몰", industry="fashion",
                             urls=(f"{site}/csp_page.html",)))
    with Store(tmp_path / "radar.db") as store:
        batch(watch, store, registry, concurrency=1)
        scan_id = store.scan_ids()[0]
        found = {d["fingerprint_id"]: d["verdict"] for d in store.detections_for(scan_id)}
        # The fixture's CSP names these and loads none of them.
        assert found.get("insider") == "PROBABLE"
        assert found.get("braze") == "PROBABLE"


def test_two_identical_runs_produce_identical_verdicts(tmp_path, site, registry):
    """Idempotency on a page we control.

    Real sites rotate ad tech between visits, so this can only be asserted
    against a static fixture — which is exactly why the removal rules
    have to tolerate flapping on live sites.
    """
    watch = watchlist(Target(id="stable", company="안정몰", industry="fashion",
                             urls=(f"{site}/dynamic_sdk.html",)))
    with Store(tmp_path / "radar.db") as store:
        batch(watch, store, registry, concurrency=1)
        batch(watch, store, registry, concurrency=1)

        first, second = store.scan_ids()
        def verdicts(scan_id):
            return {d["fingerprint_id"]: d["verdict"] for d in store.detections_for(scan_id)}
        assert verdicts(first) == verdicts(second)
        assert len(store.runs()) == 2
