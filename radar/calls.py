"""Evidence-backed review queue for changes across all MarTech categories."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import brief as brf
from . import config as cfg
from . import signals as sig
from .notify import signal_key
from .store import Store
from .strings import company_name, translator

TIERS = ("site", "changes", "leaving", "arrivals")
_TIER_OF = {
    sig.KIND_HOST_CHANGED: "site", sig.KIND_MIGRATION: "changes",
    sig.KIND_REMOVAL: "leaving", sig.KIND_NEW: "arrivals",
}


def _fmt(date: str | None) -> str:
    if not date:
        return ""
    try:
        stamp = datetime.fromisoformat(date)
        settings = cfg.load()
        zone = settings.analysis.timezone if settings.analysis.since else settings.scan.timezone
        return stamp.astimezone(ZoneInfo(zone)).date().isoformat() if stamp.tzinfo else date[:10]
    except ValueError:
        return date[:10]


def call_list(store: Store, language: str | None = None,
              arrivals_limit: int | None = None,
              ) -> list[dict[str, Any]]:
    settings = cfg.load()
    language = language or settings.ui.language
    t = translator(language)
    summaries = sig.summaries(store)
    acks = {sig.normalize_key(k): v for k, v in store.signal_acks().items()}
    targets = {r["id"]: r for r in store.targets()}

    entries: dict[str, list[dict[str, Any]]] = {tier: [] for tier in TIERS}
    found = sig.all_signals(store, language=language)
    identifiers_by_target = store.identifiers_many({s.target_id for s in found})
    for signal in found:
        tier = _TIER_OF.get(signal.kind)
        target = targets.get(signal.target_id)
        summary = summaries.get(signal.target_id)
        if tier is None or target is None or not target["enabled"] or summary is None:
            continue
        key = signal_key(signal)
        ack = acks.get(key)
        if ack and ack["state"] == "DISMISSED":
            continue
        maturity = summary.maturity
        direct = [r for r in summary.stack if not r["indirect_only"]]
        gone = sig.pending_removal(summary.changes)
        judged_runs = max((c.judged_runs for c in summary.changes), default=summary.good_scans)
        evidence = [t("calls.observed", date=_fmt(summary.last_good_at), n=judged_runs),
                    signal.headline + (" — " + signal.detail if signal.detail else "")]
        confirm_at = None
        for change in signal.changes:
            if change.state == sig.STATE_REMOVAL_CANDIDATE and change.since_at:
                confirm_at = (datetime.fromisoformat(change.since_at)
                              + timedelta(days=sig.REMOVAL_CONFIRM_DAYS)).date().isoformat()
                evidence.append(t("calls.confirm_at", date=confirm_at))
        new = [c.name for c in summary.changes
               if c.state in (sig.STATE_NEW, sig.STATE_REAPPEARED)]
        lost = [c.name for c in summary.changes
                if c.state in (sig.STATE_REMOVAL_CANDIDATE, sig.STATE_REMOVED)]
        if new:
            evidence.append(t("calls.changes.new", names=", ".join(sorted(new))))
        if lost:
            evidence.append(t("calls.changes.gone", names=", ".join(sorted(lost))))
        identifiers = identifiers_by_target.get(signal.target_id, [])
        if identifiers:
            evidence.append(t("calls.identifiers", n=len(identifiers)))
        if summary.site and summary.site.shift:
            evidence.append(t("calls.host_shift", previous=summary.site.shift.previous,
                              current=summary.site.shift.current))

        reason = signal.headline

        entries[tier].append({
            "target_id": signal.target_id,
            "company": company_name(target, language),
            "industry": signal.industry,
            "tier": tier,
            "kind": signal.kind,
            "score": maturity.score,
            "maturity": maturity.as_dict(),
            "reason": reason,
            "evidence": evidence,
            "facts": brf.citable_facts(t, direct, gone, maturity, judged_runs),
            "confirm_at": confirm_at,
            "key": key,
            "ack": ack["state"] if ack else "",
            "vendors": list(signal.vendors),
            "urls": json.loads(target["urls_json"]),
        })

    # Combine arrivals for the same target into one review entry.
    merged: dict[str, dict[str, Any]] = {}
    for entry in entries["arrivals"]:
        kept = merged.get(entry["target_id"])
        if kept is None:
            merged[entry["target_id"]] = entry
            continue
        vendors = sorted(set(kept["vendors"]) | set(entry["vendors"]))
        kept["vendors"] = vendors
        kept["reason"] = t("calls.reason.arrivals", vendors=", ".join(vendors),
                           total=kept["maturity"]["total"])
        kept["evidence"] = kept["evidence"][:1] + [
            line for line in (kept["evidence"][1:] + entry["evidence"][1:])
            if line not in kept["evidence"][:1]]
        seen: set[str] = set()
        kept["evidence"] = [x for x in kept["evidence"] if not (x in seen or seen.add(x))]
    entries["arrivals"] = list(merged.values())

    for tier in TIERS:
        entries[tier].sort(key=lambda e: (e["industry"], e["company"], e["vendors"]))
    if arrivals_limit is not None:
        entries["arrivals"] = entries["arrivals"][:arrivals_limit]
    return [{"tier": tier, "title": t(f"calls.tier.{tier}"),
             "note": t(f"calls.tier.{tier}.note"), "entries": entries[tier]}
            for tier in TIERS if entries[tier]]


def render(tiers: list[dict[str, Any]], language: str | None = None) -> str:
    t = translator(language or cfg.load().ui.language)
    lines = [f"# {t('calls.title')} — {datetime.now().date().isoformat()}", ""]
    if not tiers:
        lines.append(t("calls.empty"))
    for tier in tiers:
        lines += [f"## {tier['title']}", tier["note"], ""]
        for n, e in enumerate(tier["entries"], 1):
            lines.append(f"{n}. **{e['company']}** ({e['industry']}) — {e['reason']}")
            for line in e["evidence"]:
                lines.append(f"   - {line}")
            lines.append(f"   - {t('calls.facts')}: " + " / ".join(
                f.lstrip("- ") for f in e["facts"]))
            if e["ack"]:
                lines.append(f"   - {t('ack.acked')}")
        lines.append("")
    return "\n".join(lines)


__all__ = ["TIERS", "call_list", "render"]
