"""Watchlist health: which targets have stopped being reachable.

A NAV_FAILED week is routine; the same target failing for three
consecutive attempts is a dead or moved domain wearing the same YAML
entry. Left alone it quietly shrinks coverage — the target still counts
in the watchlist, but no evidence has arrived for a month. This report is
the manual triage the 2026-08 batch cleanup was, made repeatable.

robots-skipped attempts are ignored entirely: honouring a site's robots
file is policy working as intended, not breakage, and it neither extends
nor breaks a failure streak.
"""

from __future__ import annotations

from typing import Any

from . import evidence as ev
from .store import Store

#: Statuses that mean "we tried and could not get a real look".
FAILURE_STATUSES = frozenset({
    "NAV_FAILED", "TIMEOUT", "BLOCKED", "BROWSER_ERROR", "THIN",
})


def report(store: Store, streak: int = 3) -> list[dict[str, Any]]:
    """Targets whose last `streak`+ attempts on a URL all failed.

    Streaks are per URL: a company whose home page died but whose product
    page still answers is a YAML fix, not a dead company. `last_good_at`
    separates "was fine until June" from "never worked at all".
    """
    targets = {r["id"]: r for r in store.targets(enabled_only=True)}
    history: dict[tuple[str, str], list[Any]] = {}
    rows = store.conn.execute(
        "SELECT target_id, url, status, started_at FROM scan "
        "ORDER BY started_at DESC, id DESC")
    for row in rows:
        if row["target_id"] in targets:
            history.setdefault((row["target_id"], row["url"]), []).append(row)

    flagged: list[dict[str, Any]] = []
    for (target_id, url), attempts in history.items():
        run: list[Any] = []
        for attempt in attempts:                       # newest first
            if attempt["status"] == ev.STATUS_SKIPPED_BY_ROBOTS:
                continue
            if attempt["status"] in FAILURE_STATUSES:
                run.append(attempt)
            else:
                break
        if len(run) < streak:
            continue
        target = targets[target_id]
        statuses = [a["status"] for a in run]
        last_good = next(
            (a["started_at"] for a in attempts
             if a["status"] in ev.TRUSTWORTHY_STATUSES), None)
        flagged.append({
            "target_id": target_id,
            "company": target["company"],
            "tier": target["tier"],
            "note": target["note"],
            "url": url,
            "streak": len(run),
            "dominant": max(set(statuses), key=statuses.count),
            "statuses": statuses[:6],
            "last_failed_at": run[0]["started_at"],
            "last_good_at": last_good,
        })
    flagged.sort(key=lambda r: (-r["streak"], r["target_id"]))
    return flagged
