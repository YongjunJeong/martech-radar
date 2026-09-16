"""Compact factual briefs and the weekly digest.

Briefs summarize stored observations and their evidence for the CLI and
dashboard. Unobservable companies produce an explicit refusal instead of
unsupported claims.
"""

from __future__ import annotations

import json
from datetime import datetime, UTC
from typing import Any

from . import config as cfg
from . import signals as sig
from .store import Store
from .strings import translator

CATEGORY_LABEL = {
    "engagement": "engagement", "experimentation": "A/B·실험",
    "analytics": "애널리틱스", "attribution": "어트리뷰션",
    "advertising": "광고", "platform": "플랫폼·인프라",
}
CATEGORY_ORDER = ["engagement", "experimentation", "analytics",
                  "attribution", "advertising", "platform"]


def _fmt_date(value: str | None) -> str:
    return (value or "")[:10]


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
    engagement = [r for r in direct if r["category"] == sig.ENGAGEMENT]
    # An engagement platform seen until recently is not "never had one".
    # Saying greenfield here would put a falsehood in front of a prospect
    # who, a few weeks ago, demonstrably ran Braze.
    gone = sorted((c for c in changes.values()
                   if c.category == sig.ENGAGEMENT
                   and c.state in (sig.STATE_REMOVAL_CANDIDATE, sig.STATE_REMOVED)),
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
    lines += ["", t("brief.engagement")]
    if engagement:
        for row in engagement:
            change = changes.get(row["fingerprint_id"])
            state = (f" — {change.state}, {change.run_streak}/{len(judged)}") if change else ""
            lines.append(f"- **{row['name']}** (점수 {row['score']}, "
                         f"{row['direct_layers']}){state}")
    elif gone:
        for change in gone:
            lines.append(t("brief.engagement.gone", name=change.name,
                           state=change.state, since=change.since_run,
                           streak=change.run_streak, judged=len(comparable)))
    else:
        lines.append(t("brief.engagement.none", judged=len(comparable)))

    lines += ["", t("brief.stack")]
    grouped: dict[str, list[str]] = {}
    for row in direct:
        grouped.setdefault(row["category"], []).append(row["name"])
    for category in CATEGORY_ORDER:
        names = grouped.get(category)
        if names:
            lines.append(f"- {t('category.' + category)} ({len(names)}): "
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
    if engagement:
        pass
    elif gone:
        for change in gone:
            lines.append(t("brief.fact.engagement_gone", name=change.name,
                           since=change.since_run, streak=change.run_streak))
    else:
        lines.append(t("brief.fact.no_engagement", judged=len(comparable)))
    lines.append(t("brief.fact.advertising", n=maturity.advertising))
    lines.append(t("brief.fact.analytics", n=maturity.analytics))
    if maturity.attribution:
        lines.append(t("brief.fact.attribution", n=maturity.attribution))
    if maturity.experimentation:
        lines.append(t("brief.fact.experimentation", n=maturity.experimentation))
    else:
        lines.append(t("brief.fact.no_experimentation"))

    return "\n".join(lines), True


def weekly_digest(store: Store, language: str | None = None) -> str:
    t = translator(language or cfg.load().ui.language)
    runs = [dict(r) for r in store.runs(limit=2)]
    coverage = store.coverage()
    found = sig.all_signals(store, language=language)
    quiet = sig.quiet_greenfield(store, language)
    unjudged = [c for c in coverage.values() if not c["judged"]]

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
        watchlist_size = len(store.targets())
        covered = store.conn.execute(
            "SELECT COUNT(DISTINCT target_id) AS n FROM scan WHERE run_id = ?",
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

    changed = [s for s in found if s.kind != sig.KIND_GREENFIELD]
    lines += [t("digest.changes")]
    if changed:
        for signal in changed:
            lines.append(f"- **[{signal.kind}]** {signal.company} ({signal.industry}) — "
                         f"{signal.headline}")
    else:
        lines.append(t("digest.no_changes"))

    greenfield = [s for s in found if s.kind == sig.KIND_GREENFIELD]
    lines += ["", t("digest.greenfield", n=len(greenfield))]
    for signal in sorted(greenfield, key=lambda s: -(s.maturity.score if s.maturity else 0)):
        score = signal.maturity.score if signal.maturity else 0
        lines.append(f"- {signal.company} ({signal.industry}) — "
                     + t("signals.quiet.score", score=score) + f", {signal.detail}")

    if quiet:
        lines += ["", t("digest.quiet"),
                  "- " + ", ".join(f"{q['company']}({q['maturity']['score']})" for q in quiet)]
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
    return {
        "signals": len(found),
        "changes": sum(1 for s in found if s.kind != sig.KIND_GREENFIELD),
        "greenfield": sum(1 for s in found if s.kind == sig.KIND_GREENFIELD),
        "unjudged": sum(1 for c in store.coverage().values() if not c["judged"]),
    }
