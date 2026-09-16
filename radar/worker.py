"""The thing that actually visits websites.

Split out from the dashboard on purpose. Whoever runs the dashboard and
whoever runs the browser do not have to be the same machine: a worker on an
office laptop reaches sites that turn away a datacentre address, and it does
so by being an ordinary visitor rather than by pretending to be one.

A worker claims one job at a time, scans it, writes the result back, and
asks for another. Nothing is retried inside a run — a site that refused this
week is a fact the history needs, and retrying turns one refusal into
several.
"""

from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import dataclass
from collections.abc import Callable
from datetime import UTC, datetime

from . import detector as det
from . import signals as sig
from . import tasks
from .collector import COLLECTOR_VERSION, Collector, ScanConfig
from .registry import Registry
from .store import Store

ProgressFn = Callable[[str], None]


def default_worker_name() -> str:
    """Something a human can recognise in the job table."""
    try:
        return socket.gethostname()
    except Exception:  # noqa: BLE001
        return "worker"


@dataclass
class WorkerReport:
    claimed: int = 0
    done: int = 0
    failed: int = 0

    def summary(self) -> str:
        return f"{self.done} scanned, {self.failed} failed"


async def drain_queue(
    store: Store,
    registry: Registry,
    scan_config: ScanConfig | None = None,
    worker: str | None = None,
    run_id: int | None = None,
    concurrency: int = 1,
    keep_evidence: bool = True,
    progress: ProgressFn | None = None,
    idle_exit: bool = True,
) -> WorkerReport:
    """Claim and scan until the queue is empty."""
    say = progress or (lambda _msg: None)
    name = worker or default_worker_name()
    report = WorkerReport()
    write_lock = asyncio.Lock()

    base = scan_config or ScanConfig()

    async with Collector(base) as collector:
        # Give the collector the database as robots memory, so a site whose
        # rules we have already read stays honoured on a day its server
        # will not serve the file.
        collector.remember_robots_in(store)

        async def consume(slot: int) -> None:
            while True:
                async with write_lock:
                    job = store.claim_job(f"{name}#{slot}", run_id)
                if job is None:
                    if idle_exit:
                        return
                    await asyncio.sleep(2)
                    continue

                report.claimed += 1
                try:
                    # A company may be marked as an exception, with a reason
                    # recorded next to it. That decision travels with the job.
                    collector.config.respect_robots = (
                        base.respect_robots and job["robots_policy"] != "override")
                    result = await collector.scan(job["url"])
                    report_ = det.detect(result, registry)
                    async with write_lock:
                        scan_id = store.record_scan(
                            job["run_id"], job["target_id"], result, keep_evidence)
                        store.record_detections(scan_id, report_)
                        partial = store.flag_partial_scan(scan_id)
                        store.finish_job(job["id"], scan_id)
                    if partial:
                        say(f"  {job['target_id']:20} {'PARTIAL':18} {partial}")
                    report.done += 1
                    if not partial:
                        say(f"  {job['target_id']:20} "
                            f"{result['scan']['status']:18} {job['url']}")
                except Exception as exc:  # noqa: BLE001 — one bad job, not a dead worker
                    async with write_lock:
                        store.fail_job(job["id"], f"{type(exc).__name__}: {exc}")
                    report.failed += 1
                    say(f"  {job['target_id']:20} {'WORKER_ERROR':18} {exc}")

        async def chores() -> None:
            """Queued tasks, the heartbeat, and closing drained runs.

            One slot, beside the scan slots. A task runs on its own
            connection in a thread, but under the write lock, so the scan
            loop's connection is idle while it works — no two writers.
            """
            while True:
                async with write_lock:
                    task = store.claim_task(name)
                if task is None:
                    if idle_exit:
                        return
                    async with write_lock:
                        store.set_meta("worker_seen", json.dumps(
                            {"name": name, "at": datetime.now(UTC).isoformat()}))
                        closed = finish_runs(store, registry)
                    if closed:
                        say(f"  closed run(s) {closed}")
                    await asyncio.sleep(2)
                    continue
                say(f"  task #{task['id']} {task['kind']} — running")
                async with write_lock:
                    await asyncio.to_thread(tasks.run_task, store.path, task, registry)
                    row = store.task(task["id"])
                state = row["state"] if row else "?"
                say(f"  task #{task['id']} {task['kind']} — {state}"
                    + (f": {row['error']}" if row and row["error"] else ""))

        await asyncio.gather(*(consume(i) for i in range(max(1, concurrency))), chores())

    return report


def finish_runs(store: Store, registry: Registry | None = None) -> list[int]:
    """Close out runs whose queue has drained.

    A run is only finished when every job it created has stopped moving —
    which is not the same as "the worker went home".
    """
    closed: list[int] = []
    for row in store.open_runs():
        progress = store.run_progress(row["run_id"])
        if progress["complete"]:
            fields = {"collector_version": COLLECTOR_VERSION,
                      "detector_version": det.DETECTOR_VERSION}
            if registry is not None:
                fields["fingerprint_files"] = ",".join(registry.source_files)
                fields["fingerprint_count"] = len(registry)
                fields["fingerprints_hash"] = registry.content_hash
            store.finish_run(row["run_id"], **fields)
            closed.append(row["run_id"])
    if closed:
        # The batch just changed what every company page would say; pay the
        # summary rebuild here, once, instead of on someone's first request.
        sig.refresh_summaries(store)
    return closed


__all__ = ["drain_queue", "finish_runs", "default_worker_name", "WorkerReport"]
