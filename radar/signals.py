"""Change detection and the sales signals built on it.

Two rules decide almost everything here.

A run in which a target was never successfully scanned is *skipped*, not
counted as absence. A bot wall is not a churn event, and the whole value of
this system evaporates the first time a rep is sent to a "they dropped
their platform" meeting because the site returned 403 that week.

Only direct evidence counts. Indirect references can vary with the script-body
budget, so they cannot establish a vendor change.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from collections.abc import Iterable

from . import config as cfg
from . import evidence as ev
from . import hosts as hst
from .store import Store
from .strings import company_name, translator

# Confirmation is measured in observed time, not run count: tiers scan at
# different cadences, and two daily P1 runs agreeing is two days of
# evidence, not two weeks. An absence is REMOVED once at least
# REMOVAL_CONFIRM_RUNS judged runs agree AND the agreement spans at least
# REMOVAL_CONFIRM_DAYS. Weekly cadence keeps the old behaviour exactly
# (second absent run lands 7 days after the first).
REMOVAL_CONFIRM_RUNS = 2
REMOVAL_CONFIRM_DAYS = 7.0

ENGAGEMENT = "engagement"

# A departure and an arrival this close in time still read as one move —
# wide enough that a swap observed daily does not shatter into a removal
# plus an unrelated arrival.
MIGRATION_WINDOW_DAYS = 10.0


def _days_between(earlier: str | None, later: str | None) -> float:
    """Absolute distance in days; unparsable input reads as "too far"."""
    try:
        a = datetime.fromisoformat(earlier)      # type: ignore[arg-type]
        b = datetime.fromisoformat(later)        # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float("inf")
    return abs((b - a).total_seconds()) / 86400.0

STATE_BASELINE = "BASELINE"            # our only usable observation, not an event
STATE_STABLE = "STABLE"
STATE_NEW = "NEW"
STATE_REAPPEARED = "REAPPEARED"
STATE_REMOVAL_CANDIDATE = "REMOVAL_CANDIDATE"
STATE_REMOVED = "REMOVED"

KIND_HOME_MIGRATION = "HOME_MIGRATION"          # we were replaced, and by whom
KIND_HOME_REMOVAL = "HOME_REMOVAL"              # we are gone, replacement unknown
KIND_COMPETITOR_MIGRATION = "COMPETITOR_MIGRATION"
KIND_COMPETITOR_REMOVAL = "COMPETITOR_REMOVAL"
KIND_GREENFIELD = "GREENFIELD"
KIND_COMPETITOR_NEW = "COMPETITOR_NEW"
KIND_HOME_NEW = "HOME_NEW"
KIND_HOST_CHANGED = "HOST_CHANGED"                # we looked at a different site, not a changed stack

# Lower number sorts first, and the order is the order a rep should read
# them in. Losing an account outranks everything, and losing it to a named
# replacement outranks losing it to nothing — you know who to talk about.
PRIORITY = {
    KIND_HOME_MIGRATION: 1,
    KIND_HOME_REMOVAL: 2,
    KIND_COMPETITOR_MIGRATION: 3,
    KIND_COMPETITOR_REMOVAL: 4,
    KIND_GREENFIELD: 5,
    KIND_COMPETITOR_NEW: 6,
    KIND_HOME_NEW: 7,
    KIND_HOST_CHANGED: 8,
}

#: The `HOME_*` kinds only ever fire when `[vendor] home` is set.
HOME_KINDS = frozenset({KIND_HOME_MIGRATION, KIND_HOME_REMOVAL, KIND_HOME_NEW})


@dataclass(frozen=True)
class RunObservation:
    run_id: int
    scanned_at: str
    judged: bool
    vendors: frozenset[str]
    urls_scanned: int
    urls_ok: int
    #: Landing hosts of the trustworthy scans — where we actually looked.
    hosts: frozenset[str] = frozenset()


@dataclass(frozen=True)
class HostShift:
    """The most recent judged run that landed on a different site than the
    judged run before it. History from before it is not comparable."""
    run_id: int
    since_at: str
    previous: str
    current: str

    def as_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "since_at": self.since_at,
                "previous": self.previous, "current": self.current}


@dataclass(frozen=True)
class SiteInfo:
    latest_run: int
    hosts: tuple[str, ...]
    shift: HostShift | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"latest_run": self.latest_run, "hosts": list(self.hosts),
                "shift": self.shift.as_dict() if self.shift else None}

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> SiteInfo | None:
        if not payload:
            return None
        shift = payload.get("shift")
        return cls(latest_run=payload["latest_run"], hosts=tuple(payload.get("hosts", ())),
                   shift=HostShift(**shift) if shift else None)


def lands_off_domain(target_urls: list[str], site: SiteInfo | None) -> bool:
    """The landing moved at some point and now sits on nobody's watched domain.

    A site that has landed on some other domain from the very first run is
    taken at its word — the company's official site simply lives there
    (old.example → new.example). Only a *change* of landing is evidence that we
    may be looking at someone else.
    """
    return bool(site and site.shift) and hst.off_domain(target_urls, site.hosts)


def host_shift(observations: list[RunObservation]) -> HostShift | None:
    judged = [o for o in observations if o.judged and o.hosts]
    for index in range(len(judged) - 1, 0, -1):
        earlier, later = judged[index - 1], judged[index]
        before, after = hst.sites(earlier.hosts), hst.sites(later.hosts)
        if before and after and before.isdisjoint(after):
            return HostShift(run_id=later.run_id, since_at=later.scanned_at,
                             previous=sorted(before)[0], current=sorted(after)[0])
    return None


def site_info(observations: list[RunObservation]) -> SiteInfo | None:
    judged = [o for o in observations if o.judged]
    if not judged:
        return None
    return SiteInfo(latest_run=judged[-1].run_id, hosts=tuple(sorted(judged[-1].hosts)),
                    shift=host_shift(observations))


@dataclass(frozen=True)
class Change:
    target_id: str
    fingerprint_id: str
    name: str
    category: str
    state: str
    since_run: int | None
    run_streak: int
    judged_runs: int
    #: When the current state began — what the time-based windows compare.
    since_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "fingerprint_id": self.fingerprint_id,
            "name": self.name,
            "category": self.category,
            "state": self.state,
            "since_run": self.since_run,
            "run_streak": self.run_streak,
            "judged_runs": self.judged_runs,
            "since_at": self.since_at,
        }


CHANGE_FIELDS = ("target_id", "fingerprint_id", "name", "category",
                 "state", "since_run", "run_streak", "judged_runs", "since_at")


def change_from_dict(payload: dict[str, Any]) -> Change:
    # .get, not [] — summaries written before since_at existed still load.
    return Change(**{k: payload.get(k) for k in CHANGE_FIELDS})


@dataclass(frozen=True)
class Maturity:
    """How much MarTech an account already runs.

    A greenfield lead is only interesting if the company is actually doing
    digital marketing. Fifteen vendors and seven retargeting pixels with no
    engagement platform is a company buying the same user repeatedly; two
    vendors is a brochure site.
    """
    total: int
    analytics: int
    advertising: int
    attribution: int
    experimentation: int

    @property
    def score(self) -> int:
        return (self.analytics + self.advertising
                + 2 * self.attribution + 2 * self.experimentation)

    def as_dict(self) -> dict[str, int]:
        return {
            "total": self.total, "analytics": self.analytics,
            "advertising": self.advertising, "attribution": self.attribution,
            "experimentation": self.experimentation, "score": self.score,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, int]) -> Maturity:
        return cls(total=payload["total"], analytics=payload["analytics"],
                   advertising=payload["advertising"],
                   attribution=payload["attribution"],
                   experimentation=payload["experimentation"])


@dataclass(frozen=True)
class Signal:
    kind: str
    target_id: str
    company: str
    industry: str
    headline: str
    detail: str
    vendors: tuple[str, ...] = ()
    maturity: Maturity | None = None
    changes: tuple[Change, ...] = field(default=())

    @property
    def priority(self) -> int:
        return PRIORITY[self.kind]

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "priority": self.priority,
            "target_id": self.target_id,
            "company": self.company,
            "industry": self.industry,
            "headline": self.headline,
            "detail": self.detail,
            "vendors": list(self.vendors),
            "maturity": self.maturity.as_dict() if self.maturity else None,
            "changes": [c.as_dict() for c in self.changes],
        }


# -- timeline ----------------------------------------------------------------


def timeline(store: Store, target_id: str) -> tuple[list[RunObservation], dict[str, tuple[str, str]]]:
    """Per-run history for one target, plus a vendor id -> (name, category) map.

    A run contributes one observation per target: the union of DETECTED
    vendors across every URL that produced a trustworthy scan. Runs in which
    the target was never scanned do not appear at all, which is what makes a
    filtered batch (`--only one_company`) harmless to everyone else's history.
    """
    rows = store.conn.execute(
        "SELECT s.run_id, s.id AS scan_id, s.status, s.started_at, s.page_host, "
        "       d.fingerprint_id, d.name, d.category, d.verdict, d.indirect_only "
        "FROM scan s LEFT JOIN detection d ON d.scan_id = s.id "
        "WHERE s.target_id = ? ORDER BY s.run_id, s.id",
        (target_id,),
    )

    per_run: dict[int, dict[str, Any]] = {}
    labels: dict[str, tuple[str, str]] = {}
    for row in rows:
        _observe(per_run, labels, row)
    return _observations(per_run), labels


def _observe(per_run: dict[int, dict[str, Any]],
             labels: dict[str, tuple[str, str]], row: Any) -> None:
    entry = per_run.setdefault(row["run_id"], {
        "scanned_at": row["started_at"],
        "vendors": set(),
        "urls": set(),
        "ok_urls": set(),
        "hosts": set(),
    })
    entry["urls"].add(row["scan_id"])
    if row["status"] not in ev.TRUSTWORTHY_STATUSES:
        return
    entry["ok_urls"].add(row["scan_id"])
    if row["page_host"]:
        entry["hosts"].add(row["page_host"])
    if row["fingerprint_id"] is None:
        return
    # Verdict and directness are the gate; everything else is context.
    if row["verdict"] == "DETECTED" and not row["indirect_only"]:
        entry["vendors"].add(row["fingerprint_id"])
        labels[row["fingerprint_id"]] = (row["name"], row["category"])


def _observations(per_run: dict[int, dict[str, Any]]) -> list[RunObservation]:
    return [
        RunObservation(
            run_id=run_id,
            scanned_at=entry["scanned_at"],
            judged=bool(entry["ok_urls"]),
            vendors=frozenset(entry["vendors"]),
            urls_scanned=len(entry["urls"]),
            urls_ok=len(entry["ok_urls"]),
            hosts=frozenset(entry["hosts"]),
        )
        for run_id, entry in sorted(per_run.items())
    ]


def timelines(store: Store) -> dict[str, tuple[list[RunObservation], dict[str, tuple[str, str]]]]:
    """`timeline()` for every target in one pass over the history.

    The sweep endpoints (signals page, digest, company list) ask the same
    question about every company; per-target queries made that one scan
    +detection read per company per sweep, twice when a page runs two
    sweeps. Grouping one joined read costs what a single company costs.
    """
    rows = store.conn.execute(
        "SELECT s.target_id, s.run_id, s.id AS scan_id, s.status, s.started_at, s.page_host, "
        "       d.fingerprint_id, d.name, d.category, d.verdict, d.indirect_only "
        "FROM scan s LEFT JOIN detection d ON d.scan_id = s.id "
        "ORDER BY s.target_id, s.run_id, s.id",
    )
    grouped: dict[str, dict[int, dict[str, Any]]] = {}
    labels_by_target: dict[str, dict[str, tuple[str, str]]] = {}
    for row in rows:
        per_run = grouped.setdefault(row["target_id"], {})
        labels = labels_by_target.setdefault(row["target_id"], {})
        _observe(per_run, labels, row)
    return {
        target_id: (_observations(per_run), labels_by_target[target_id])
        for target_id, per_run in grouped.items()
    }


def _trailing_streak(runs: list[RunObservation], vendor: str, present: bool) -> int:
    streak = 0
    for observation in reversed(runs):
        if (vendor in observation.vendors) is present:
            streak += 1
        else:
            break
    return streak


def changes_for(store: Store, target_id: str,
                timeline_data: tuple[list[RunObservation], dict[str, tuple[str, str]]] | None = None,
                ) -> list[Change]:
    observations, labels = timeline_data if timeline_data is not None else timeline(store, target_id)
    judged = [o for o in observations if o.judged]
    if not judged:
        return []
    shift = host_shift(observations)
    if shift is not None:
        # A different site is a different history. Vendors seen on the old
        # landing page are not "removed" from the new one; they were never
        # on it. The first run on the new site is a baseline again.
        judged = [o for o in judged if o.run_id >= shift.run_id]

    latest = judged[-1]
    ever_seen = sorted({v for o in judged for v in o.vendors})
    changes: list[Change] = []

    for vendor in ever_seen:
        name, category = labels.get(vendor, (vendor, "unknown"))
        present_now = vendor in latest.vendors
        streak = _trailing_streak(judged, vendor, present_now)
        before = judged[:-streak]
        since_run = judged[-streak].run_id
        since_at = judged[-streak].scanned_at

        if present_now:
            if not before:
                # Present in every judged run we have. With only one such run
                # that is a baseline, not a discovery — a target scanned once
                # and blocked ever since must not look like a fresh install.
                state = STATE_BASELINE if len(judged) == 1 else STATE_STABLE
            elif any(vendor in o.vendors for o in before):
                state = STATE_REAPPEARED
            else:
                state = STATE_NEW
        else:
            observed_days = _days_between(since_at, latest.scanned_at)
            state = (STATE_REMOVED
                     if streak >= REMOVAL_CONFIRM_RUNS
                     and observed_days >= REMOVAL_CONFIRM_DAYS
                     else STATE_REMOVAL_CANDIDATE)

        changes.append(Change(
            target_id=target_id,
            fingerprint_id=vendor,
            name=name,
            category=category,
            state=state,
            since_run=since_run,
            run_streak=streak,
            judged_runs=len(judged),
            since_at=since_at,
        ))
    return changes


def has_judged_run(store: Store, target_id: str) -> bool:
    """Whether we have ever got a trustworthy look at this company.

    Not the same as "we found something". A site scanned successfully with
    zero vendors detected has an empty change list, and gating on that list
    is how a real greenfield account falls out of every view at once.
    """
    return any(o.judged for o in timeline(store, target_id)[0])


def _judged_in(timeline_data) -> bool:
    return any(o.judged for o in timeline_data[0])


def maturity_of(store: Store, target_id: str,
                stack: list[dict[str, Any]] | None = None) -> Maturity:
    """Maturity from a company's stack.

    `stack` lets a caller that already holds `stacks_of()` skip the
    per-company table read; without it the store is asked directly.
    """
    counts: dict[str, int] = {}
    for row in (store.stack_of(target_id) if stack is None else stack):
        if row["indirect_only"]:
            continue
        counts[row["category"]] = counts.get(row["category"], 0) + 1
    return Maturity(
        total=sum(counts.values()),
        analytics=counts.get("analytics", 0),
        advertising=counts.get("advertising", 0),
        attribution=counts.get("attribution", 0),
        experimentation=counts.get("experimentation", 0),
    )


# -- materialised summaries --------------------------------------------------


@dataclass(frozen=True)
class CompanySummary:
    target_id: str
    judged: bool
    scans: int
    good_scans: int
    last_status: str | None
    last_scan_at: str | None
    last_good_at: str | None
    stack: list[dict[str, Any]]
    changes: list[Change]
    maturity: Maturity
    site: SiteInfo | None = None


#: Bumped when the *derivation* changes (states, windows, serialisation) —
#: the data digest alone cannot see a code change.
SUMMARY_VERSION = 3


def _summary_state(store: Store) -> str:
    return f"{store.data_state()}|s{SUMMARY_VERSION}"


def refresh_summaries(store: Store) -> int:
    """Recompute every company's Gold row from the current Silver layer.

    Called when a batch drains and after a redetect; `summaries()` also
    calls it whenever the stored key no longer matches the data, so a
    reader can trust what it gets without trusting when it was written.
    """
    state = _summary_state(store)
    coverage = store.coverage()
    stacks = store.stacks_of()
    lines = timelines(store)
    rows: list[dict[str, Any]] = []
    for target_id, cover in coverage.items():
        stack = stacks.get(target_id, [])
        timeline_data = lines.get(target_id, ([], {}))
        changes = (changes_for(store, target_id, timeline_data)
                   if cover["judged"] else [])
        maturity = maturity_of(store, target_id, stack)
        rows.append({
            "target_id": target_id,
            "judged": cover["judged"],
            "scans": cover["scans"],
            "good_scans": cover["good_scans"],
            "last_status": cover["last_status"],
            "last_scan_at": cover["last_scan_at"],
            "last_good_at": cover["last_good_at"],
            "stack_json": json.dumps(stack, ensure_ascii=False),
            "changes_json": json.dumps([c.as_dict() for c in changes],
                                       ensure_ascii=False),
            "maturity_json": json.dumps(maturity.as_dict()),
            "site_json": json.dumps(
                site.as_dict() if (site := site_info(timeline_data[0])) else None),
        })
    store.replace_summaries(rows, state)
    # This handle may have served the old rows already; forget them.
    store._summary_cache = None
    return len(rows)


def summaries(store: Store) -> dict[str, CompanySummary]:
    """The stored summaries, rebuilt first if the data has moved on.

    Cached on the store handle: a web request opens one Store and renders
    several views from it, and they should share one table read.
    """
    state = _summary_state(store)
    cached = getattr(store, "_summary_cache", None)
    if cached is not None and cached[0] == state:
        return cached[1]
    if store.summary_state() != state:
        refresh_summaries(store)
    loaded = {
        row["target_id"]: CompanySummary(
            target_id=row["target_id"],
            judged=bool(row["judged"]),
            scans=row["scans"],
            good_scans=row["good_scans"],
            last_status=row["last_status"],
            last_scan_at=row["last_scan_at"],
            last_good_at=row["last_good_at"],
            stack=json.loads(row["stack_json"]),
            changes=[change_from_dict(c) for c in json.loads(row["changes_json"])],
            maturity=Maturity.from_dict(json.loads(row["maturity_json"])),
            site=SiteInfo.from_dict(json.loads(row["site_json"] or "null")),
        )
        for row in store.summary_rows()
    }
    store._summary_cache = (state, loaded)
    return loaded


# -- signals -----------------------------------------------------------------

# Below this, an account is not doing enough digital marketing for "no
# engagement platform" to mean anything worth a call.
GREENFIELD_MIN_MATURITY = 6


def _pair_migrations(going: list[Change], arriving: list[Change]
                     ) -> tuple[list[tuple[Change, Change]], set[Change]]:
    """Match departures to arrivals that happened around the same time.

    Greedy and closest-first: each vendor takes part in at most one move, so
    a company that swaps two platforms at once produces two migrations
    rather than four half-events.
    """
    candidates = sorted(
        ((_days_between(g.since_at, a.since_at), g, a)
         for g in going for a in arriving
         if _days_between(g.since_at, a.since_at) <= MIGRATION_WINDOW_DAYS),
        key=lambda triple: (triple[0], triple[1].name, triple[2].name),
    )
    pairs: list[tuple[Change, Change]] = []
    used: set[Change] = set()
    for _distance, gone, arrived in candidates:
        if gone in used or arrived in used:
            continue
        used.add(gone)
        used.add(arrived)
        pairs.append((gone, arrived))
    return pairs, used


def signals_for(store: Store, target_row: Any,
                language: str | None = None,
                stack: list[dict[str, Any]] | None = None,
                timeline_data: tuple[list[RunObservation], dict[str, tuple[str, str]]] | None = None,
                summary: CompanySummary | None = None,
                ) -> list[Signal]:
    settings = cfg.load()
    language = language or settings.ui.language
    t = translator(language)
    target_id = target_row["id"]
    company = company_name(target_row, language)
    industry = target_row["industry"]
    if summary is not None:
        if not summary.judged:
            return []
        changes = summary.changes
        stack = summary.stack if stack is None else stack
        site = summary.site
    else:
        if timeline_data is None:
            timeline_data = timeline(store, target_id)
        if not _judged_in(timeline_data):
            return []
        changes = changes_for(store, target_id, timeline_data)
        site = site_info(timeline_data[0])

    found: list[Signal] = []
    off = lands_off_domain(json.loads(target_row["urls_json"]), site)
    if site and site.shift and site.shift.run_id == site.latest_run:
        found.append(Signal(
            kind=KIND_HOST_CHANGED,
            target_id=target_id, company=company, industry=industry,
            headline=t("signal.host_changed.headline",
                       previous=site.shift.previous, current=site.shift.current),
            detail=t("signal.host_changed.detail", run=site.shift.run_id)
                   + (t("signal.host_changed.off_domain") if off else ""),
        ))
    engagement_now = [
        c for c in changes
        if c.category == ENGAGEMENT
        and c.state in (STATE_STABLE, STATE_NEW, STATE_REAPPEARED, STATE_BASELINE)
    ]

    home = (settings.vendor.home or "").strip()
    going = [c for c in changes
             if c.state in (STATE_REMOVAL_CANDIDATE, STATE_REMOVED)
             and c.category == ENGAGEMENT]
    arriving = [c for c in changes
                if c.state in (STATE_NEW, STATE_REAPPEARED) and c.category == ENGAGEMENT]

    # A departure and an arrival close together in the same category is one
    # event, not two. Reporting both halves separately buries the fact that
    # matters — that the account swapped, and to what.
    pairs, paired = _pair_migrations(going, arriving)
    for left, joined in pairs:
        is_home = bool(home) and left.fingerprint_id == home
        confirmed = left.state == STATE_REMOVED
        found.append(Signal(
            kind=KIND_HOME_MIGRATION if is_home else KIND_COMPETITOR_MIGRATION,
            target_id=target_id, company=company, industry=industry,
            headline=t("signal.migration.headline", gone=left.name, arrived=joined.name)
                     + ("" if confirmed else t("signal.migration.unconfirmed")),
            detail=t("signal.migration.detail", gone=left.name, gone_since=left.since_run,
                     arrived=joined.name, arrived_since=joined.since_run),
            vendors=(left.name, joined.name), changes=(left, joined),
        ))

    for change in going:
        if change in paired:
            continue
        confirmed = change.state == STATE_REMOVED
        if home and change.fingerprint_id == home:
            found.append(Signal(
                kind=KIND_HOME_REMOVAL,
                target_id=target_id, company=company, industry=industry,
                headline=t("signal.home_removal.headline",
                           vendor=change.name, streak=change.run_streak)
                         + ("" if confirmed else t("signal.home_removal.unconfirmed")),
                detail=t("signal.home_removal.detail", since=change.since_run)
                       + t("signal.home_removal.confirmed_note" if confirmed
                           else "signal.home_removal.candidate_note"),
                vendors=(change.name,), changes=(change,),
            ))
        else:
            found.append(Signal(
                kind=KIND_COMPETITOR_REMOVAL,
                target_id=target_id, company=company, industry=industry,
                headline=t("signal.competitor_removal.headline",
                           name=change.name, streak=change.run_streak)
                         + ("" if confirmed else t("signal.competitor_removal.unconfirmed")),
                detail=t("signal.competitor_removal.detail", since=change.since_run),
                vendors=(change.name,), changes=(change,),
            ))

    for change in arriving:
        if change in paired:
            continue
        is_home = bool(home) and change.fingerprint_id == home
        found.append(Signal(
            kind=KIND_HOME_NEW if is_home else KIND_COMPETITOR_NEW,
            target_id=target_id, company=company, industry=industry,
            headline=t("signal.arrival.headline", name=change.name)
                     + (t("signal.arrival.reappeared")
                        if change.state == STATE_REAPPEARED else ""),
            detail=t("signal.arrival.detail", since=change.since_run),
            vendors=(change.name,), changes=(change,),
        ))

    if not engagement_now and not pending_removal(changes) and not off:
        maturity = maturity_of(store, target_id, stack)
        if maturity.score >= GREENFIELD_MIN_MATURITY:
            found.append(Signal(
                kind=KIND_GREENFIELD,
                target_id=target_id, company=company, industry=industry,
                headline=t("signal.greenfield.headline", total=maturity.total),
                detail=(t("signal.greenfield.detail",
                          advertising=maturity.advertising, analytics=maturity.analytics)
                        + (t("signal.greenfield.attribution", n=maturity.attribution)
                           if maturity.attribution else "")
                        + (t("signal.greenfield.experimentation", n=maturity.experimentation)
                           if maturity.experimentation else "")
                        + "."),
                maturity=maturity,
            ))

    return found


def pending_removal(changes: Iterable[Change]) -> list[Change]:
    """Engagement platforms seen until recently and absent once.

    Not gone yet, not present either. Calling such an account greenfield
    would turn one missed tag load into a "no platform" pitch to a company
    that ran Braze last week; it stays out of both greenfield buckets until
    the removal is confirmed or the platform reappears.
    """
    return [c for c in changes
            if c.category == ENGAGEMENT and c.state == STATE_REMOVAL_CANDIDATE]


def quiet_greenfield(store: Store, language: str | None = None
                     ) -> list[dict[str, Any]]:
    """Companies with no engagement platform that fell below the maturity bar.

    Reported rather than dropped: a threshold that hides accounts silently
    reads as "we checked and there was nothing", which is how a watchlist
    quietly stops covering half of itself.
    """
    language = language or cfg.load().ui.language
    quiet: list[dict[str, Any]] = []
    summary_map = summaries(store)
    for target_row in store.targets():
        target_id = target_row["id"]
        summary = summary_map.get(target_id)
        if summary is None or not summary.judged:
            continue
        if any(c.category == ENGAGEMENT
               and c.state in (STATE_STABLE, STATE_NEW, STATE_REAPPEARED, STATE_BASELINE)
               for c in summary.changes):
            continue
        if pending_removal(summary.changes):
            continue
        if lands_off_domain(json.loads(target_row["urls_json"]), summary.site):
            continue
        maturity = summary.maturity
        if maturity.score < GREENFIELD_MIN_MATURITY:
            quiet.append({
                "target_id": target_id,
                "company": company_name(target_row, language),
                "industry": target_row["industry"],
                "maturity": maturity.as_dict(),
            })
    # Highest score first: ad-tech cookie-sync endpoints fire conditionally,
    # so an account sitting on the threshold can drop a point one week and
    # regain it the next. Near-misses belong at the top of the list, not
    # buried in id order.
    quiet.sort(key=lambda q: -q["maturity"]["score"])
    return quiet


def all_signals(store: Store, kinds: Iterable[str] | None = None,
                language: str | None = None) -> list[Signal]:
    wanted = set(kinds) if kinds else None
    language = language or cfg.load().ui.language
    found: list[Signal] = []
    summary_map = summaries(store)
    for target_row in store.targets():
        summary = summary_map.get(target_row["id"])
        if summary is None:
            continue
        for signal in signals_for(store, target_row, language, summary=summary):
            if wanted is None or signal.kind in wanted:
                found.append(signal)
    found.sort(key=lambda s: (s.priority, s.industry, s.company))
    return found
