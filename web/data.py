"""View models.

Everything the templates render is assembled here as plain dicts, so the
templates stay dumb and the numbers stay testable without a browser.

Every page still recomputes from SQLite on request — no cache to
invalidate, and a fresh `radar batch` shows up immediately — but the reads
are batched: one `stacks_of()` per request, shared by every row, instead of
per-company scans of the whole history. Watchlists in the hundreds stay
interactive that way.
"""

from __future__ import annotations

import json
from typing import Any

from radar import signals as sig
from radar.strings import company_name
from radar.registry import Registry
from radar.store import Store

CATEGORY_ORDER = [
    "engagement", "experimentation", "analytics",
    "attribution", "advertising", "platform",
]

# What a state means for the reader, in the order we want to see them.
STATE_ORDER = [
    sig.STATE_REMOVED, sig.STATE_REMOVAL_CANDIDATE, sig.STATE_NEW,
    sig.STATE_REAPPEARED, sig.STATE_STABLE, sig.STATE_BASELINE,
]


def _category_key(name: str) -> int:
    return CATEGORY_ORDER.index(name) if name in CATEGORY_ORDER else len(CATEGORY_ORDER)


def group_by_category(rows: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Group while keeping CATEGORY_ORDER.

    Jinja's own `groupby` re-sorts alphabetically, which buried engagement —
    the one category the whole tool exists for — under "advertising".
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["category"], []).append(row)
    return sorted(grouped.items(), key=lambda kv: _category_key(kv[0]))


def company_rows(store: Store, industries: dict[str, str],
                 language: str = "ko",
                 ) -> list[dict[str, Any]]:
    """One row per company with everything the list views need.

    Everything comes off the materialised summaries — one table read for
    the whole page, refreshed by `summaries()` only when the data moved.
    """
    summary_map = sig.summaries(store)
    rows: list[dict[str, Any]] = []

    for target in store.targets():
        target_id = target["id"]
        cover = summary_map.get(target_id)
        stack = list(cover.stack) if cover else []
        direct = [r for r in stack if not r["indirect_only"]]
        states = {c.fingerprint_id: c.state for c in cover.changes} if cover else {}
        engagement = [r for r in direct if r["category"] == sig.ENGAGEMENT]
        site = cover.site if cover else None
        urls = json.loads(target["urls_json"])
        off_domain = sig.lands_off_domain(urls, site)

        counts: dict[str, int] = {}
        for row in direct:
            counts[row["category"]] = counts.get(row["category"], 0) + 1

        rows.append({
            "id": target_id,
            "company": company_name(target, language),
            "company_en": target["company_en"],
            "industry": target["industry"],
            "industry_label": industries.get(target["industry"], target["industry"]),
            "tier": target["tier"],
            "note": target["note"],
            "urls": urls,
            "enabled": bool(target["enabled"]),
            "judged": bool(cover and cover.judged),
            "off_domain": off_domain,
            "landing": list(site.hosts) if site else [],
            "host_shift": (site.shift.as_dict()
                           if site and site.shift and site.shift.run_id == site.latest_run
                           else None),
            "last_status": cover.last_status if cover else None,
            "last_scan_at": cover.last_scan_at if cover else None,
            "good_scans": cover.good_scans if cover else 0,
            "vendor_count": len(direct),
            "counts": counts,
            "engagement": [
                {"id": r["fingerprint_id"], "name": r["name"],
                 "state": states.get(r["fingerprint_id"], "")}
                for r in engagement
            ],
            "engagement_pending": [
                {"id": c.fingerprint_id, "name": c.name}
                for c in sig.pending_removal(cover.changes if cover else [])
            ],
            "maturity": (cover.maturity.as_dict()
                         if cover and cover.judged else None),
            "moved": sorted(
                (
                    {"id": fid, "state": state}
                    for fid, state in states.items()
                    if state in (sig.STATE_NEW, sig.STATE_REAPPEARED,
                                 sig.STATE_REMOVAL_CANDIDATE, sig.STATE_REMOVED)
                ),
                key=lambda m: STATE_ORDER.index(m["state"]),
            ),
        })
    rows.sort(key=lambda r: (r["industry_label"], r["company"]))
    return rows


def industry_rows(companies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in companies:
        entry = grouped.setdefault(row["industry"], {
            "industry": row["industry"],
            "label": row["industry_label"],
            "companies": 0, "judged": 0, "unjudged": 0,
            "with_engagement": 0, "pending": 0, "greenfield": 0,
            "vendors": {},
        })
        entry["companies"] += 1
        if not row["judged"]:
            entry["unjudged"] += 1
            continue
        entry["judged"] += 1
        if row["off_domain"]:
            entry["pending"] += 1
        elif row["engagement"]:
            entry["with_engagement"] += 1
            for vendor in row["engagement"]:
                entry["vendors"][vendor["name"]] = entry["vendors"].get(vendor["name"], 0) + 1
        elif row["engagement_pending"]:
            entry["pending"] += 1
        else:
            entry["greenfield"] += 1

    rows = list(grouped.values())
    for row in rows:
        row["vendors"] = sorted(row["vendors"].items(), key=lambda kv: (-kv[1], kv[0]))
    rows.sort(key=lambda r: (-r["companies"], r["label"]))
    return rows


def vendor_rows(store: Store, companies: list[dict[str, Any]],
                registry: Registry | None = None,
                ) -> list[dict[str, Any]]:
    """Vendor share across companies, with the never-seen ones kept visible."""
    by_company: dict[str, list[dict[str, Any]]] = {}
    meta: dict[str, dict[str, str]] = {}
    judged = [c for c in companies if c["judged"]]
    summary_map = sig.summaries(store)

    for company in judged:
        summary = summary_map.get(company["id"])
        for row in (summary.stack if summary else []):
            if row["indirect_only"]:
                continue
            meta.setdefault(row["fingerprint_id"],
                            {"name": row["name"], "category": row["category"]})
            by_company.setdefault(row["fingerprint_id"], []).append({
                "id": company["id"], "company": company["company"],
                "industry_label": company["industry_label"],
                "score": row["score"], "layers": row["direct_layers"],
            })

    rows = [
        {
            "id": fingerprint_id,
            "name": info["name"],
            "category": info["category"],
            "count": len(by_company[fingerprint_id]),
            "share": len(by_company[fingerprint_id]) / len(judged) if judged else 0.0,
            "companies": sorted(by_company[fingerprint_id], key=lambda c: c["company"]),
        }
        for fingerprint_id, info in meta.items()
    ]

    if registry is not None:
        # A fingerprint that never matches is worth seeing: it is either a
        # vendor with no Korean footprint or a pattern that is quietly broken.
        seen = set(meta)
        for fp in registry:
            if fp.id not in seen:
                rows.append({"id": fp.id, "name": fp.name, "category": fp.category,
                             "count": 0, "share": 0.0, "companies": []})

    rows.sort(key=lambda r: (_category_key(r["category"]), -r["count"], r["name"]))
    return rows


def company_detail(store: Store, industries: dict[str, str], target_id: str,
                   language: str = "ko") -> dict[str, Any] | None:
    target = store.target(target_id)
    if target is None:
        return None

    timeline_data = sig.timeline(store, target_id)
    observations, _labels = timeline_data
    changes = {c.fingerprint_id: c
               for c in sig.changes_for(store, target_id, timeline_data)}
    coverage = store.coverage(target_id).get(target_id)
    detected = store.stack_of(target_id)

    stack: list[dict[str, Any]] = []
    for row in store.stack_of(target_id, ("DETECTED", "PROBABLE")):
        change = changes.get(row["fingerprint_id"])
        stack.append({
            "id": row["fingerprint_id"],
            "name": row["name"],
            "category": row["category"],
            "verdict": row["verdict"],
            "score": row["score"],
            "direct_layers": [layer for layer in (row["direct_layers"] or "").split(",") if layer],
            "indirect_layers": [layer for layer in (row["indirect_layers"] or "").split(",") if layer],
            "indirect_only": bool(row["indirect_only"]),
            "seen_on": row["seen_on"],
            "state": change.state if change else "",
            "run_streak": change.run_streak if change else 0,
            "judged_runs": change.judged_runs if change else 0,
            "matches": json.loads(row["matches_json"] or "[]"),
        })
    stack.sort(key=lambda r: (_category_key(r["category"]), -r["score"], r["name"]))
    site = sig.site_info(observations)
    urls = json.loads(target["urls_json"])

    return {
        "off_domain": sig.lands_off_domain(urls, site),
        "landing": list(site.hosts) if site else [],
        "host_shift": (site.shift.as_dict()
                       if site and site.shift and site.shift.run_id == site.latest_run
                       else None),
        "stack_groups": group_by_category(stack),
        "id": target_id,
        "company": company_name(target, language),
        "company_en": target["company_en"],
        "industry": target["industry"],
        "industry_label": industries.get(target["industry"], target["industry"]),
        "tier": target["tier"],
        "note": target["note"],
        "urls": urls,
        "enabled": bool(target["enabled"]),
        "judged": bool(coverage and coverage["judged"]),
        "last_status": coverage["last_status"] if coverage else None,
        "stack": stack,
        "timeline": [
            {"run_id": o.run_id, "scanned_at": o.scanned_at, "judged": o.judged,
             "urls_ok": o.urls_ok, "urls_scanned": o.urls_scanned,
             "vendor_count": len(o.vendors), "vendors": sorted(o.vendors),
             "hosts": sorted(o.hosts)}
            for o in reversed(observations)
        ],
        "scans": [dict(r) for r in store.scans_for(target_id, limit=12)],
        "identifiers": store.identifiers_of(target_id),
        "signals": [s.as_dict() for s in sig.signals_for(
            store, target, stack=detected, timeline_data=timeline_data)],
        "maturity": sig.maturity_of(store, target_id, detected).as_dict(),
    }


def overview(store: Store, companies: list[dict[str, Any]]) -> dict[str, Any]:
    found = sig.all_signals(store)
    by_kind: dict[str, int] = {}
    for signal in found:
        by_kind[signal.kind] = by_kind.get(signal.kind, 0) + 1
    runs = [dict(r) for r in store.runs(limit=8)]
    judged = [c for c in companies if c["judged"]]
    return {
        "runs": runs,
        "latest_run": runs[0] if runs else None,
        "company_count": len(companies),
        "judged_count": len(judged),
        "unjudged": [c for c in companies if not c["judged"]],
        "with_engagement": [c for c in judged if c["engagement"] and not c["off_domain"]],
        "pending": [c for c in judged if c["off_domain"]
                    or (not c["engagement"] and c["engagement_pending"])],
        "greenfield": [c for c in judged if not c["off_domain"]
                       and not c["engagement"] and not c["engagement_pending"]],
        "signal_counts": by_kind,
        "signal_total": len(found),
    }


def try_pattern(field: str, pattern: str, db_path: str | None = None,
                limit: int = 40) -> dict[str, Any]:
    """Run one candidate pattern against every stored scan.

    Writing a fingerprint blind is how unanchored ones get shipped. This
    answers the only question that matters before adding one — what does it
    already match, and is any of it something else's.
    """
    from radar.detector import _layered_values, _matches_value
    from radar.store import Store

    if field not in registry_fields():
        return {"error": f"unknown field {field}", "hits": [], "scanned": 0}
    try:
        signal = compile_signal(field, pattern)
    except Exception as exc:  # noqa: BLE001 — a bad regex is user input, not a crash
        return {"error": str(exc), "hits": [], "scanned": 0}

    hits: list[dict[str, Any]] = []
    scanned = 0
    with Store(db_path) as store:
        for scan_id in store.scan_ids():
            stored = store.load_evidence(scan_id)
            if stored is None:
                continue
            scanned += 1
            pools = _layered_values(stored.get("evidence") or {})
            for layer, values in pools.get(signal.field, {}).items():
                for value in values:
                    if _matches_value(signal, value):
                        hits.append({
                            "host": stored.get("scan", {}).get("page_host", "?"),
                            "layer": layer, "value": value[:120],
                        })
                        break
            if len(hits) >= limit:
                break
    seen: set[tuple[str, str]] = set()
    unique = []
    for hit in hits:
        key = (hit["host"], hit["layer"])
        if key not in seen:
            seen.add(key)
            unique.append(hit)
    return {"error": None, "hits": unique, "scanned": scanned}


def registry_fields() -> tuple[str, ...]:
    from radar.registry import FIELDS
    return FIELDS


def compile_signal(field: str, pattern: str):
    from radar.registry import _compile, STRENGTH_STRONG
    return _compile(field, pattern, STRENGTH_STRONG, "pattern test")
