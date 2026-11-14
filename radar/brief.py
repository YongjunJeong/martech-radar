"""Compact factual briefs and the weekly digest.

The split matters for cost. Assembling what we observed is deterministic
work that Python does for free; only the writing needs a model. So this
module hands the skill a short, factual brief instead of making it read and
summarise several megabytes of JSON.

Everything here is an observation with a number attached. Interpretation —
what it means, what to say about it — is deliberately left to the skill,
because a tool that editorialises quietly becomes a tool that asserts things
nobody verified.
"""

from __future__ import annotations

import json
from datetime import datetime, UTC
from typing import Any
from zoneinfo import ZoneInfo

from . import config as cfg
from . import signals as sig
from .store import Store
from .strings import translator

CATEGORY_LABEL = {
    "engagement": "engagement", "experimentation": "A/B·실험",
    "analytics": "애널리틱스", "attribution": "어트리뷰션",
    "advertising": "광고", "platform": "플랫폼·인프라",
}
CATEGORY_ORDER = sorted(CATEGORY_LABEL)


def _fmt_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        stamp = datetime.fromisoformat(value)
        settings = cfg.load()
        zone = settings.analysis.timezone if settings.analysis.since else settings.scan.timezone
        return stamp.astimezone(ZoneInfo(zone)).date().isoformat() if stamp.tzinfo else value[:10]
    except ValueError:
        return value[:10]


def company_brief(store: Store, target_id: str,
                  settings: cfg.Config | None = None,
                  language: str | None = None) -> tuple[str, bool]:
    """Return (text, usable). `usable` is False when no email may be written."""
    settings = settings if settings is not None else cfg.load()
    t = translator(language or settings.ui.language)
    target = store.target(target_id)
    if target is None:
        return t("brief.unknown_target", target_id=target_id), False

    timeline_data = sig.timeline(store, target_id)
    observations, _ = timeline_data
    judged = [o for o in observations if o.judged]
    lines: list[str] = []
    name = target["company"] + (f" ({target['company_en']})" if target["company_en"] else "")
    lines.append(f"# {name} — {target['industry']}")

    if settings.analysis.since:
        lines.append(f"분석 범위: {settings.analysis.since}부터 ({settings.analysis.timezone}). 첫 정상 관측은 기준점이며 도입일이 아닙니다."
                      if (language or settings.ui.language) == "ko" else
                      f"Analysis since {settings.analysis.since} ({settings.analysis.timezone}); first observation is a baseline, not an adoption date.")

    if not judged:
        cover = store.coverage(target_id).get(target_id, {})
        lines += [
            "",
            t("brief.never_scanned", scans=cover.get("scans", 0),
              status=cover.get("last_status") or t("status.unscanned")),
            "",
            t("brief.never_scanned.rule"),
        ]
        return "\n".join(lines), False

    latest = judged[-1]
    site = sig.site_info(observations)
    if sig.lands_off_domain(json.loads(target["urls_json"]), site):
        # The page we judged is not on any domain we were asked to watch.
        # Whatever it runs, it is not evidence about this company.
        lines += [
            "",
            t("brief.off_domain", host=", ".join(site.hosts), run=site.latest_run),
            "",
            t("brief.never_scanned.rule"),
        ]
        return "\n".join(lines), False

    # Runs before a landing-site change are not comparable; every "in all N
    # judged runs" below counts only the runs on the current site.
    comparable = ([o for o in judged if o.run_id >= site.shift.run_id]
                  if site is not None and site.shift is not None else judged)
    stack = [dict(r) for r in store.stack_of(target_id)]
    direct = [r for r in stack if not r["indirect_only"]]
    maturity = sig.maturity_of(store, target_id, stack)
    changes = {c.fingerprint_id: c
               for c in sig.changes_for(store, target_id, timeline_data)}
    gone = sorted((c for c in changes.values()
                   if c.state in (sig.STATE_REMOVAL_CANDIDATE, sig.STATE_REMOVED)),
                  key=lambda c: c.name)

    lines += [
        t("brief.urls", urls=", ".join(json.loads(target["urls_json"]))),
        t("brief.latest", date=_fmt_date(latest.scanned_at),
          run=latest.run_id, judged=len(judged)),
        t("brief.counts", total=len(direct), score=maturity.score),
    ]
    if site is not None and site.shift is not None:
        lines.append(t("brief.host_shift", run=site.shift.run_id,
                       previous=site.shift.previous, current=site.shift.current))
    lines += ["", t("brief.stack")]
    grouped: dict[str, list[str]] = {}
    for row in direct:
        grouped.setdefault(row["category"], []).append(row["name"])
    for category in sorted(grouped):
        names = grouped.get(category)
        if names:
            lines.append(f"- {(t('category.' + category) if category in CATEGORY_ORDER else category)} ({len(names)}): "
                         + ", ".join(sorted(names)))

    moved = [c for c in changes.values()
             if c.state in (sig.STATE_NEW, sig.STATE_REAPPEARED,
                            sig.STATE_REMOVAL_CANDIDATE, sig.STATE_REMOVED)]
    lines += ["", t("brief.changes")]
    if moved:
        for change in sorted(moved, key=lambda c: c.name):
            lines.append(f"- {change.name}: {change.state} "
                         f"(run {change.since_run} +{change.run_streak})")
    else:
        lines.append(t("brief.changes.none", judged=len(comparable)))

    identifiers = store.identifiers_of(target_id)
    if identifiers:
        lines += ["", t("brief.identifiers")]
        for ident in identifiers[:12]:
            lines.append(f"- {ident['kind']}: `{ident['value']}`")

    lines += ["", t("brief.facts"), "", t("brief.facts.rule")]
    lines += citable_facts(t, direct, gone, maturity, len(comparable))

    return "\n".join(lines), True


def citable_facts(t, direct_stack: list[dict], gone: list[sig.Change],
                  maturity: sig.Maturity, judged_runs: int) -> list[str]:
    """The sentences an email may contain, from observation alone.

    Shared by the brief and the call list so the two can never disagree
    about what is allowed to be said.
    """
    facts: list[str] = []
    grouped: dict[str, list[str]] = {}
    for row in direct_stack:
        grouped.setdefault(row["category"], []).append(row["name"])
    for category, names in sorted(grouped.items()):
        facts.append(t("brief.fact.category", category=(t("category." + category)
                                                       if category in CATEGORY_ORDER else category),
                       n=len(names), names=", ".join(sorted(names))))
    for change in gone:
        facts.append(t("brief.fact.absent", name=change.name, since=change.since_run,
                       streak=change.run_streak))
    dead = [r["name"] for r in direct_stack if "sunset" in r["name"].lower()]
    if dead:
        facts.append(t("brief.fact.experimentation_sunset", n=len(dead), names=", ".join(dead)))
    if not direct_stack:
        facts.append(t("brief.fact.no_tools"))
    return facts


def weekly_digest(store: Store, language: str | None = None) -> str:
    t = translator(language or cfg.load().ui.language)
    runs = [dict(r) for r in store.runs(limit=2)]
    coverage = store.coverage()
    found = sig.all_signals(store, language=language)
    active_ids = {r["id"] for r in store.targets(enabled_only=True)}
    unjudged = [c for tid, c in coverage.items() if tid in active_ids and not c["judged"]]

    lines = ["# " + t("digest.title",
                       date=datetime.now(UTC).date().isoformat()), ""]
    if runs:
        latest = runs[0]
        statuses: dict[str, int] = {}
        for row in store.conn.execute(
                "SELECT status, COUNT(*) AS n FROM scan WHERE run_id = ? GROUP BY status",
                (latest["id"],)):
            statuses[row["status"]] = row["n"]
        # A filtered run (`--only`) covers a handful of targets. Reporting
        # just its scan count makes a weekly digest read as though the whole
        # watchlist collapsed to one company.
        watchlist_size = len(active_ids)
        covered = store.conn.execute(
            "SELECT COUNT(DISTINCT s.target_id) AS n FROM scan s "
            "JOIN target t ON t.id=s.target_id WHERE s.run_id = ? AND t.enabled=1",
            (latest["id"],)).fetchone()["n"]
        partial = ("" if covered >= watchlist_size
                   else t("digest.partial", total=watchlist_size, covered=covered))
        lines += [
            t("digest.latest_run", run=latest["id"], when=_fmt_date(latest["started_at"]),
              scans=latest["scan_count"],
              statuses=", ".join(f"{k} {v}" for k, v in sorted(statuses.items())))
            + partial,
            "",
        ]
        if partial:
            lines += [t("digest.partial_note"), ""]

    changed = found
    lines += [t("digest.changes")]
    if changed:
        for signal in changed:
            lines.append(f"- **[{signal.kind}]** {signal.company} ({signal.industry}) — "
                         f"{signal.headline}")
    else:
        lines.append(t("digest.no_changes"))

    if unjudged:
        lines += ["", t("digest.unjudged"),
                  "- " + ", ".join(sorted(c["target_id"] for c in unjudged))]

    from . import hygiene
    flagged = hygiene.report(store)
    if flagged:
        lines += ["", t("digest.hygiene", n=len(flagged))]
        for row in flagged[:5]:
            lines.append(f"- {row['target_id']}: {row['dominant']} x{row['streak']} ({row['url']})")
        if len(flagged) > 5:
            lines.append(f"- … +{len(flagged) - 5}")
    return "\n".join(lines)


def digest_stats(store: Store) -> dict[str, Any]:
    found = sig.all_signals(store)
    coverage = store.coverage()
    return {
        "signals": len(found),
        "changes": len(found),
        "unjudged": sum(1 for r in store.targets(enabled_only=True)
                        if not coverage.get(r["id"], {}).get("judged")),
    }
