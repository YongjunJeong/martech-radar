"""Scan the whole watchlist and persist the result.

Every scan is recorded, including the failures. A blocked or timed-out visit
is a fact about that week that change detection needs: without the failure rows,
a vendor missing from three consecutive blocked scans is indistinguishable
from a vendor that was removed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, UTC
from collections.abc import Callable, Iterable
from typing import Any

from . import config
from . import detector as det
from . import evidence as ev
from . import signals as sig
from .collector import ScanConfig
from .registry import Registry
from . import worker
from .store import Store


# Chromium contexts are cheap but the network and the CPU are not. Three at a
# time keeps a 50-target run under half an hour without the settle windows
# starving each other and turning good sites into false TIMEOUTs.
DEFAULT_CONCURRENCY = config.load().scan.concurrency

ProgressFn = Callable[[str], None]


@dataclass
class ScanOutcome:
    target_id: str
    url: str
    status: str
    duration_ms: int
    detected: int
    probable: int
    scan_id: int | None = None
    error: str | None = None


@dataclass
class BatchReport:
    run_id: int
    started_at: str
    finished_at: str
    outcomes: list[ScanOutcome] = field(default_factory=list)

    @property
    def ok(self) -> list[ScanOutcome]:
        return [o for o in self.outcomes if o.status in ev.TRUSTWORTHY_STATUSES]

    @property
    def failed(self) -> list[ScanOutcome]:
        return [o for o in self.outcomes if o.status not in ev.TRUSTWORTHY_STATUSES]

    def summary(self) -> str:
        by_status: dict[str, int] = {}
        for o in self.outcomes:
            by_status[o.status] = by_status.get(o.status, 0) + 1
        parts = ", ".join(f"{k} {v}" for k, v in sorted(by_status.items()))
        return f"run {self.run_id}: {len(self.outcomes)} scans ({parts})"


@dataclass(frozen=True)
class Job:
    """One URL of one company, as the batch sees it."""
    target_id: str
    company: str
    url: str
    tier: str | None = None


def jobs_for(store: Store, target_ids: Iterable[str] | None = None,
             industries: Iterable[str] | None = None,
             limit: int | None = None,
             tiers: Iterable[str] | None = None) -> list[Job]:
    """Expand the enabled watchlist into one job per URL.

    Reads the database rather than the YAML file: the watchlist can be
    edited from the dashboard, and a batch must scan what the list says
    now, not what a file said when it was last saved.

    `tiers` filters on the watchlist's tier labels; the value "none"
    selects targets with no tier at all, so a tiered rollout can still
    address the leftovers.
    """
    wanted = set(target_ids) if target_ids else None
    verticals = set(industries) if industries else None
    bands = {t.lower() for t in tiers} if tiers else None
    rows = [r for r in store.targets(enabled_only=True)
            if (wanted is None or r["id"] in wanted)
            and (verticals is None or r["industry"] in verticals)
            and (bands is None or (r["tier"] or "none").lower() in bands)]
    if limit:
        rows = rows[:limit]
    return [Job(target_id=r["id"], company=r["company"], url=url, tier=r["tier"])
            for r in rows for url in json.loads(r["urls_json"])]


def due_split(store: Store, jobs: list[Job],
              cadence: config.CadenceSettings | None = None,
              now: datetime | None = None) -> tuple[list[Job], list[str]]:
    """Split jobs into (due now, target ids skipped as fresh).

    Freshness is measured from the last *attempt*, whatever its status —
    a site that blocked us yesterday earned its full interval of quiet,
    not a faster retry. A target never scanned is always due.
    """
    cadence = cadence or config.load().cadence
    now = now or datetime.now(UTC)
    last: dict[str, str] = {
        row["target_id"]: row["latest"]
        for row in store.conn.execute(
            "SELECT target_id, MAX(started_at) AS latest FROM scan GROUP BY target_id")
    }
    due: list[Job] = []
    skipped: dict[str, None] = {}
    for job in jobs:
        latest = last.get(job.target_id)
        if latest is None:
            due.append(job)
            continue
        age = (now - datetime.fromisoformat(latest)).total_seconds()
        if age >= cadence.seconds_for(job.tier):
            due.append(job)
        else:
            skipped[job.target_id] = None
    return due, list(skipped)


async def run_batch(
    store: Store,
    registry: Registry,
    jobs: list[Job] | None = None,
    scan_config: ScanConfig | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    keep_evidence: bool = True,
    note: str | None = None,
    progress: ProgressFn | None = None,
) -> BatchReport:
    """Queue the work and drain it here.

    The command line and the dashboard's "scan now" put the same rows in the
    same queue; the only difference is where the worker happens to be
    running. Two code paths for one job is how they drift apart.
    """
    jobs = jobs if jobs is not None else jobs_for(store)
    say = progress or (lambda _msg: None)
    target_count = len({job.target_id for job in jobs})

    run_id = store.queue_run(jobs, note=note)
    started_at = datetime.now(UTC).isoformat()
    say(f"run {run_id}: {target_count} targets, {len(jobs)} urls, "
        f"concurrency {concurrency}")

    done = 0

    def relay(line: str) -> None:
        nonlocal done
        done += 1
        say(f"  [{done}/{len(jobs)}]{line}")

    await worker.drain_queue(
        store, registry, scan_config=scan_config, run_id=run_id,
        concurrency=concurrency, keep_evidence=keep_evidence, progress=relay)
    worker.finish_runs(store, registry)

    if config.load().notify.webhook:
        from . import notify
        notify.notify_new_signals(store, progress=say)

    outcomes = [
        ScanOutcome(
            target_id=row["target_id"], url=row["url"],
            status=(row["status"] or ev.STATUS_BROWSER_ERROR),
            duration_ms=row["duration_ms"] or 0,
            detected=row["detected"] or 0, probable=row["probable"] or 0,
            scan_id=row["scan_id"], error=row["error"],
        )
        for row in store.conn.execute(
            "SELECT j.target_id, j.url, j.scan_id, j.error, s.status, s.duration_ms, "
            "  (SELECT COUNT(*) FROM detection d WHERE d.scan_id = s.id "
            "     AND d.verdict = 'DETECTED') AS detected, "
            "  (SELECT COUNT(*) FROM detection d WHERE d.scan_id = s.id "
            "     AND d.verdict = 'PROBABLE') AS probable "
            "FROM job j LEFT JOIN scan s ON s.id = j.scan_id "
            "WHERE j.run_id = ? ORDER BY j.id", (run_id,))
    ]
    return BatchReport(
        run_id=run_id,
        started_at=started_at,
        finished_at=datetime.now(UTC).isoformat(),
        outcomes=outcomes,
    )


def _renormalise_storage(stored: dict) -> bool:
    """Re-apply the current storage-name rules to a stored blob.

    Returns True if anything changed, so the caller only rewrites what needs it.
    """
    evidence = stored.get("evidence") or {}
    changed = False
    storage = evidence.get("storage") or {}
    for key, values in list(storage.items()):
        cleaned = ev.normalise_storage_names(values)
        if cleaned != values:
            storage[key] = cleaned
            changed = True
    cleaned = ev.normalise_storage_names(evidence.get("set_cookie_names"))
    if evidence.get("set_cookie_names") is not None and cleaned != evidence["set_cookie_names"]:
        evidence["set_cookie_names"] = cleaned
        changed = True
    return changed


def _detected_pairs(store: Store, scan_ids: list[int]) -> set[tuple[int, str]]:
    """(scan_id, fingerprint_id) for every DETECTED row in these scans."""
    if not scan_ids:
        return set()
    marks = ",".join("?" * len(scan_ids))
    return {
        (r["scan_id"], r["fingerprint_id"]) for r in store.conn.execute(
            f"SELECT scan_id, fingerprint_id FROM detection "
            f"WHERE verdict = 'DETECTED' AND scan_id IN ({marks})", scan_ids)
    }


def redetect(store: Store, registry: Registry, run_id: int | None = None,
             progress: ProgressFn | None = None) -> dict[str, Any]:
    """Re-classify stored evidence against the current fingerprints.

    This is the payoff for keeping the evidence: a fingerprint added today
    rewrites the verdicts on every scan ever taken, and no site is visited.
    """
    say = progress or (lambda _msg: None)
    scan_ids = store.scan_ids(run_id)
    stats: dict[str, Any] = {"scans": 0, "rewritten": 0, "skipped_no_evidence": 0,
                             "detections": 0, "restatused": 0, "scrubbed": 0}
    # What a fingerprint change actually did is the question worth answering
    # after every redetect — so compare the DETECTED set before and after.
    before = _detected_pairs(store, scan_ids)

    for scan_id in scan_ids:
        stored = store.load_evidence(scan_id)
        stats["scans"] += 1
        if stored is None:
            stats["skipped_no_evidence"] += 1
            continue

        # Status rules can change too. A scan recorded as OK before THIN
        # existed must be re-graded, or the history keeps a maintenance page
        # as a real observation forever.
        # Privacy rules apply retroactively: evidence collected under a
        # looser rule is rewritten, not left as-is.
        if _renormalise_storage(stored):
            store.rewrite_evidence(scan_id, stored)
            stats["scrubbed"] += 1

        scan = stored.get("scan", {})
        if scan.get("status") == ev.STATUS_OK and ev.is_browser_error_page(
                scan.get("final_url"), scan.get("page_host")):
            scan["status"] = ev.STATUS_NAV_FAILED
            store.restatus_scan(scan_id, ev.STATUS_NAV_FAILED, "browser_error_page", stored)
            stats["restatused"] += 1
        if scan.get("status") == ev.STATUS_OK:
            # Block rules grow as new walls turn up; a challenge page stored
            # as OK before its marker existed must not stay a look.
            marker = ev.detect_block(scan.get("http_status"), scan.get("title", ""), "",
                                     scan.get("page_host"))
            if marker:
                scan["status"] = ev.STATUS_BLOCKED
                store.restatus_scan(scan_id, ev.STATUS_BLOCKED, marker, stored)
                stats["restatused"] += 1
        if scan.get("status") == ev.STATUS_OK:
            thin = ev.detect_thin(stored.get("counts") or {}, scan.get("title", ""))
            if thin:
                scan["status"] = ev.STATUS_THIN
                store.restatus_scan(scan_id, ev.STATUS_THIN, f"thin:{thin}", stored)
                stats["restatused"] += 1

        report = det.detect(stored, registry)
        stats["detections"] += store.record_detections(scan_id, report)
        stats["rewritten"] += 1

    after = _detected_pairs(store, scan_ids)
    stats["gained"] = sorted(after - before)
    stats["lost"] = sorted(before - after)
    stats["fingerprints_hash"] = registry.content_hash
    # New verdicts, new Gold: rebuild rather than leaving the summaries
    # describing detections that no longer exist.
    sig.refresh_summaries(store)

    say(
        f"re-detected {stats['rewritten']}/{stats['scans']} scans "
        f"→ {stats['detections']} detections"
        + (f", {len(stats['gained'])} verdicts gained, {len(stats['lost'])} lost"
           if stats["gained"] or stats["lost"] else ", no verdict changed")
        + (f", {stats['scrubbed']} scrubbed" if stats["scrubbed"] else "")
        + (f", {stats['restatused']} re-graded as {ev.STATUS_THIN}"
           if stats["restatused"] else "")
        + (f" ({stats['skipped_no_evidence']} had no stored evidence)"
           if stats["skipped_no_evidence"] else "")
    )
    return stats
