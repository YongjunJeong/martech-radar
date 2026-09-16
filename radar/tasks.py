"""Maintenance work that can be asked for from the dashboard.

Each task is a plain function over an open store. The dashboard queues a
row; a worker claims it and calls `run_task` with a *fresh* connection of
its own, so the scan loop's connection is never touched from another
thread. The same functions back `radar task <kind>` for anyone at a
terminal, so there is one implementation per chore, not one per entry
point.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from . import batch as bat
from . import brief as brf
from . import config as cfg
from . import hygiene
from . import notify as ntf
from . import workspace
from .registry import Registry
from .store import Store

Say = Callable[[str], None]

KINDS = ("digest", "redetect", "notify", "hygiene", "backup")


def backup_snapshot(store: Store, folder: Path | None = None, keep: int = 14) -> dict[str, Any]:
    """Snapshot the database into `folder`, keeping the newest `keep`."""
    folder = folder or workspace.path("backups")
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d-%H%M")
    dest = store.backup(folder / f"radar-{stamp}.db")
    snapshots = sorted(folder.glob("radar-*.db"))
    pruned = snapshots[:-keep] if keep > 0 and len(snapshots) > keep else []
    for old in pruned:
        old.unlink()
    return {"path": str(dest), "size_mb": dest.stat().st_size / 1_000_000,
            "kept": len(snapshots) - len(pruned), "pruned": len(pruned)}


def _digest(store: Store, registry: Registry, params: dict, say: Say) -> str:
    language = params.get("language") or cfg.load().ui.language
    text = brf.weekly_digest(store, language)
    folder = workspace.path("data", "logs")
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"digest-{datetime.now().astimezone():%Y-%m-%d}.md"
    out.write_text(text + "\n", encoding="utf-8")
    say(f"written to {out}")
    return text


def _redetect(store: Store, registry: Registry, params: dict, say: Say) -> str:
    stats = bat.redetect(store, registry, progress=say)
    return (f"re-detected {stats['scans']} scans → {stats.get('detections', '?')} detections, "
            f"{len(stats.get('gained', []))} verdicts gained, {len(stats.get('lost', []))} lost")


def _notify(store: Store, registry: Registry, params: dict, say: Say) -> str:
    report = ntf.notify_new_signals(
        store, language=params.get("language"),
        dry_run=bool(params.get("dry_run")), progress=say)
    if report.get("error"):
        raise RuntimeError(f"notify failed: {report['error']}")
    if params.get("dry_run"):
        return f"{report['pending']} signal(s) pending — nothing sent (dry run)"
    return f"sent {report['sent']} of {report['pending']} pending signal(s)"


def _hygiene(store: Store, registry: Registry, params: dict, say: Say) -> str:
    streak = int(params.get("streak") or 3)
    rows = hygiene.report(store, streak=streak)
    if not rows:
        return f"no target has {streak}+ consecutive failed attempts — watchlist looks healthy"
    lines = [f"{'streak':>6}  {'status':13} {'target':22} {'tier':4} last good        url / note"]
    for r in rows:
        good = (r["last_good_at"] or "never")[:10]
        note = f"  [{r['note']}]" if r["note"] else ""
        lines.append(f"{r['streak']:>6}  {r['dominant']:13} {r['target_id']:22} "
                     f"{r['tier'] or '-':4} {good:16} {r['url']}{note}")
    lines.append(f"\n{len(rows)} URL(s) flagged — fix the domain, disable the target, "
                 "or record why it stays")
    return "\n".join(lines)


def _backup(store: Store, registry: Registry, params: dict, say: Say) -> str:
    keep = int(params.get("keep") or 14)
    result = backup_snapshot(store, keep=keep)
    return (f"backup written: {result['path']} ({result['size_mb']:.1f} MB) — "
            f"{result['kept']} kept, {result['pruned']} pruned")


_HANDLERS: dict[str, Callable[[Store, Registry, dict, Say], str]] = {
    "digest": _digest,
    "redetect": _redetect,
    "notify": _notify,
    "hygiene": _hygiene,
    "backup": _backup,
}


def run(kind: str, store: Store, registry: Registry,
        params: dict[str, Any] | None = None, say: Say | None = None) -> str:
    """Run one task now, on this connection. Returns its text output."""
    if kind not in _HANDLERS:
        raise ValueError(f"unknown task kind {kind!r}")
    return _HANDLERS[kind](store, registry, params or {}, say or (lambda _m: None))


def run_task(db_path: Path | str | None, task: dict[str, Any], registry: Registry) -> None:
    """Execute a claimed task on a connection of its own and record the outcome.

    Meant for a worker thread: the store that claimed the task stays with
    the event loop; this one lives and dies inside the call.
    """
    progress: list[str] = []
    with Store(db_path) as store:
        try:
            output = run(task["kind"], store, registry, task.get("params") or
                         json.loads(task.get("params_json") or "{}"), progress.append)
        except Exception as exc:  # noqa: BLE001 — recorded on the row, not raised into the loop
            store.fail_task(task["id"], f"{type(exc).__name__}: {exc}", "\n".join(progress))
            return
        head = "\n".join(progress)
        store.finish_task(task["id"], (head + "\n\n" if head else "") + output)


__all__ = ["KINDS", "run", "run_task", "backup_snapshot"]
