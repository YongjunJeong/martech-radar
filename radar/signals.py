"""Evidence-based change detection across all MarTech categories.

Two rules decide almost everything here.

A run in which a target was never successfully scanned is *skipped*, not
counted as absence. A bot wall is not a churn event, and the whole value of
this system evaporates the first time a rep is sent to a "they dropped
their platform" meeting because the site returned 403 that week.

Only direct evidence counts. Two live runs fifteen minutes apart agreed on
all 329 DETECTED rows and disagreed on five `indirect_only` rows, because
the script-body budget decides which third-party bundle gets read. Building
signals on that would manufacture a weekly stream of ghosts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from functools import cached_property
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

KIND_MIGRATION = "STACK_CHANGE"
KIND_REMOVAL = "NOT_SEEN"
KIND_NEW = "NEW_DETECTION"
KIND_HOST_CHANGED = "HOST_CHANGED"

# Technical aliases for existing Python callers. All vendors now share kinds.
KIND_HOME_MIGRATION = KIND_COMPETITOR_MIGRATION = KIND_MIGRATION
KIND_HOME_REMOVAL = KIND_COMPETITOR_REMOVAL = KIND_REMOVAL
KIND_HOME_NEW = KIND_COMPETITOR_NEW = KIND_NEW
KIND_GREENFIELD = "GREENFIELD"  # retired: never emitted
HOME_KINDS = frozenset()
PRIORITY = {KIND_HOST_CHANGED: 1, KIND_MIGRATION: 2, KIND_REMOVAL: 3, KIND_NEW: 4}

LEGACY_KINDS = {
    "HOME_MIGRATION": KIND_MIGRATION, "COMPETITOR_MIGRATION": KIND_MIGRATION,
    "HOME_REMOVAL": KIND_REMOVAL, "COMPETITOR_REMOVAL": KIND_REMOVAL,
    "HOME_NEW": KIND_NEW, "COMPETITOR_NEW": KIND_NEW,
}


def normalize_key(key: str) -> str:
    kind, sep, rest = key.partition(":")
    return LEGACY_KINDS.get(kind, kind) + sep + rest


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
    (nhqv.com → nhsec.com). Only a *change* of landing is evidence that we
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
    """Observed tool counts; not a judgement of marketing maturity."""
    total: int
    analytics: int
    advertising: int
    attribution: int
    experimentation: int

    @property
    def score(self) -> int:
        return self.total

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
    cutoff = cfg.load().analysis.cutoff
    rows = store.conn.execute(
        "SELECT s.run_id, s.id AS scan_id, s.status, s.started_at, s.page_host, "
        "       d.fingerprint_id, d.name, d.category, d.verdict, d.indirect_only "
        "FROM scan s LEFT JOIN detection d ON d.scan_id = s.id "
        "WHERE s.target_id = ? AND (? = '' OR julianday(s.started_at) >= julianday(?)) "
        "ORDER BY julianday(s.started_at), s.id", (target_id, cutoff, cutoff),
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


def timelines(store: Store, target_ids=None) -> dict[str, tuple[list[RunObservation], dict[str, tuple[str, str]]]]:
    """`timeline()` for every target in one pass over the history.

    The sweep endpoints (signals page, digest, company list) ask the same
    question about every company; per-target queries made that one scan
    +detection read per company per sweep, twice when a page runs two
    sweeps. Grouping one joined read costs what a single company costs.
    """
    ids = list(target_ids) if target_ids is not None else None
    if ids == []:
        return {}
    where = ["s.target_id IN (" + ",".join("?" for _ in ids) + ")"] if ids else []
    params = list(ids or ())
    if cutoff := cfg.load().analysis.cutoff:
        where.append("julianday(s.started_at) >= julianday(?)")
        params.append(cutoff)
    clause = "WHERE " + " AND ".join(where) + " " if where else ""
    rows = store.conn.execute(
        "SELECT s.target_id, s.run_id, s.id AS scan_id, s.status, s.started_at, s.page_host, "
        "       d.fingerprint_id, d.name, d.category, d.verdict, d.indirect_only "
        "FROM scan s LEFT JOIN detection d ON d.scan_id = s.id "
        f"{clause}ORDER BY s.target_id, julianday(s.started_at), s.id", params,
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
    would incorrectly hide a successfully observed site.
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
    observations_json: str = field(default="{}", repr=False)

    @cached_property
    def observations(self) -> dict[str, Any]:
        # Detail/legacy pages need at most one company's facts. Avoid decoding
        # every company's extra payload just to open one company.
        return json.loads(self.observations_json)


#: Bumped when the *derivation* changes (states, windows, serialisation) —
#: the data digest alone cannot see a code change.
SUMMARY_VERSION = 8


def observation_summary(observations: list[RunObservation], changes: list[Change],
                        site: SiteInfo | None) -> dict[str, Any]:
    """Bounded facts for sales views, derived with the same cutoff/site scope.

    Keep counts and the eight most recent judged observations, not a second
    copy of the complete history. Evidence itself stays in scan/detection.
    """
    good = [o for o in observations if o.judged
            and (not site or not site.shift or o.run_id >= site.shift.run_id)]
    vendors = {}
    for change in changes:
        present = [o for o in good if change.fingerprint_id in o.vendors]
        facts = dict(detected_count=len(present),
                     last_seen_at=present[-1].scanned_at if present else None,
                     evidence_run=present[-1].run_id if present else None)
        if change.state not in (STATE_BASELINE, STATE_STABLE):
            previous = good[-change.run_streak - 1]
            facts.update(previous_at=previous.scanned_at,
                         previous_detected=change.fingerprint_id in previous.vendors)
        vendors[change.fingerprint_id] = facts
    return dict(observed_runs=len(observations), good_runs=len(good),
                first_at=good[0].scanned_at if good else None,
                last_at=good[-1].scanned_at if good else None,
                recent=[dict(scanned_at=o.scanned_at, vendors=sorted(o.vendors))
                        for o in good[-8:]], vendors=vendors)


def _summary_state(store: Store) -> str:
    cutoff = cfg.load().analysis.cutoff
    return f"{store.data_state()}|s{SUMMARY_VERSION}" + (f"|since:{cutoff}" if cutoff else "")


def refresh_summaries(store: Store) -> int:
    """Coalesce writers using SQLite's reservation, then refresh dirty targets.

    A waiter rechecks the revision after acquiring the reservation. Readers
    keep their WAL snapshots; writers cannot publish half a refresh.
    """
    try:
        store.conn.execute("BEGIN IMMEDIATE")
        result = _refresh_locked(store)
        store.conn.commit()
        return result
    except BaseException:
        store.conn.rollback()
        raise


def _refresh_locked(store: Store) -> int:
    state = _summary_state(store)
    if store.summary_state() == state:
        return 0
    full = (store.summary_state() or "").partition("|")[2] != state.partition("|")[2]
    dirty = None if full else [r[0] for r in store.conn.execute("SELECT target_id FROM summary_dirty")]
    coverage = store.coverage(target_ids=dirty)
    stacks = store.stacks_of(target_ids=dirty)
    lines = timelines(store, target_ids=dirty)
    rows: list[dict[str, Any]] = []
    for target_id, cover in coverage.items():
        stack = stacks.get(target_id, [])
        timeline_data = lines.get(target_id, ([], {}))
        changes = (changes_for(store, target_id, timeline_data)
                   if cover["judged"] else [])
        maturity = maturity_of(store, target_id, stack)
        site = site_info(timeline_data[0])
        observations = observation_summary(timeline_data[0], changes, site)
        solution_facts = []
        for change in changes:
            present = [change.fingerprint_id in o['vendors'] for o in observations['recent']]
            transitions = sum(a != b for a, b in zip(present, present[1:], strict=False))
            solution_facts.append((change.fingerprint_id, change.category, change.state,
                                   change.run_streak, int(transitions >= 3)))
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
            "site_json": json.dumps(site.as_dict() if site else None),
            "observations_json": json.dumps(observations),
            "filter_facts": (observations['observed_runs'], observations['good_runs'],
                             int(bool(site and site.shift))),
            "solution_facts": solution_facts,
        })
    store.conn.execute("DELETE FROM summary_dirty")
    store.replace_summaries(rows, state, target_ids=dirty)
    # This handle may have served the old rows already; forget them.
    store._summary_cache = None
    return len(rows)


def summaries(store: Store, target_ids=None) -> dict[str, CompanySummary]:
    """The stored summaries, rebuilt first if the data has moved on.

    Cached on the store handle: a web request opens one Store and renders
    several views from it, and they should share one table read.
    """
    state = _summary_state(store)
    ids = None if target_ids is None else tuple(sorted(set(target_ids)))
    cached = getattr(store, "_summary_cache", None)
    if cached is not None and cached[0] == state:
        return cached[1] if ids is None else {k: cached[1][k] for k in ids if k in cached[1]}
    scoped = getattr(store, '_scoped_summary_cache', None)
    if ids is not None and scoped and scoped[:2] == (state, ids):
        return scoped[2]
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
            observations_json=row["observations_json"] or "{}",
        )
        for row in store.summary_rows(target_ids=ids)
    }
    if ids is None:
        store._summary_cache = (state, loaded)
    else:
        store._scoped_summary_cache = (state, ids, loaded)
    return loaded


# -- signals -----------------------------------------------------------------

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
         if g.category == a.category
         and _days_between(g.since_at, a.since_at) <= MIGRATION_WINDOW_DAYS),
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
    if not target_row["enabled"]:
        return []
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
    going = [c for c in changes
             if c.state in (STATE_REMOVAL_CANDIDATE, STATE_REMOVED)]
    arriving = [c for c in changes if c.state in (STATE_NEW, STATE_REAPPEARED)]

    # A departure and an arrival close together in the same category is one
    # event, not two. Reporting both halves separately buries the fact that
    # matters — that the account swapped, and to what.
    pairs, paired = _pair_migrations(going, arriving)
    for left, joined in pairs:
        confirmed = left.state == STATE_REMOVED
        found.append(Signal(
            kind=KIND_MIGRATION,
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
        found.append(Signal(
            kind=KIND_REMOVAL,
            target_id=target_id, company=company, industry=industry,
            headline=t("signal.removal.headline", name=change.name,
                       streak=change.run_streak)
                     + ("" if confirmed else t("signal.removal.unconfirmed")),
            detail=t("signal.removal.detail", since=change.since_run),
            vendors=(change.name,), changes=(change,),
        ))

    for change in arriving:
        if change in paired:
            continue
        found.append(Signal(
            kind=KIND_NEW,
            target_id=target_id, company=company, industry=industry,
            headline=t("signal.arrival.headline", name=change.name)
                     + (t("signal.arrival.reappeared")
                        if change.state == STATE_REAPPEARED else ""),
            detail=t("signal.arrival.detail", since=change.since_run),
            vendors=(change.name,), changes=(change,),
        ))

    return found


def pending_removal(changes: Iterable[Change]) -> list[Change]:
    """Tools absent in recent observations, pending confirmation."""
    return [c for c in changes if c.state == STATE_REMOVAL_CANDIDATE]


def quiet_greenfield(store: Store, language: str | None = None) -> list[dict[str, Any]]:
    """Compatibility shim: absence of a category is no longer a sales signal."""
    return []


def all_signals(store: Store, kinds: Iterable[str] | None = None,
                language: str | None = None) -> list[Signal]:
    wanted = {LEGACY_KINDS.get(k, k) for k in kinds} if kinds else None
    language = language or cfg.load().ui.language
    found: list[Signal] = []
    summary_map = summaries(store)
    for target_row in store.targets(enabled_only=True):
        summary = summary_map.get(target_row["id"])
        if summary is None:
            continue
        for signal in signals_for(store, target_row, language, summary=summary):
            if wanted is None or signal.kind in wanted:
                found.append(signal)
    found.sort(key=lambda s: (s.priority, s.industry, s.company, s.vendors))
    return found
