"""SQLite history.

Two decisions worth stating up front.

The raw evidence is kept, gzipped, in the database rather than as loose
files. It costs roughly 20-40 KB per scan, and it buys the thing this whole
design is built around: a fingerprint added six months from now can be run
back over every scan ever taken. Evidence on disk beside a database is
evidence that eventually goes missing.

Detections are stored as derived rows that can be thrown away and rebuilt
(`redetect`), never as the source of truth. The source of truth is the
evidence blob and the fingerprint files, both of which are versioned.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, UTC

from . import evidence as ev
from . import workspace
from pathlib import Path
from typing import Any
from collections.abc import Iterator

SCHEMA_VERSION = 11

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS industry (
    code      TEXT PRIMARY KEY,
    label     TEXT NOT NULL,
    label_en  TEXT,
    position  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS target (
    id          TEXT PRIMARY KEY,
    company     TEXT NOT NULL,
    company_en  TEXT,
    industry    TEXT NOT NULL,
    tier        TEXT,
    note        TEXT,
    urls_json   TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    -- 'respect' (default) or 'override'. An override is a deliberate,
    -- per-company decision with a reason attached, not a global switch.
    robots_policy TEXT NOT NULL DEFAULT 'respect',
    robots_note   TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at         TEXT NOT NULL,
    finished_at        TEXT,
    collector_version  TEXT,
    detector_version   TEXT,
    fingerprint_files  TEXT,
    fingerprint_count  INTEGER,
    fingerprints_hash  TEXT,
    target_count       INTEGER,
    note               TEXT
);

CREATE TABLE IF NOT EXISTS scan (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    target_id     TEXT NOT NULL,
    url           TEXT NOT NULL,
    final_url     TEXT,
    page_host     TEXT,
    status        TEXT NOT NULL,
    http_status   INTEGER,
    title         TEXT,
    error         TEXT,
    block_marker  TEXT,
    started_at    TEXT NOT NULL,
    duration_ms   INTEGER,
    warnings_json TEXT,
    counts_json   TEXT,
    evidence_gz   BLOB
);

CREATE TABLE IF NOT EXISTS detection (
    scan_id         INTEGER NOT NULL REFERENCES scan(id) ON DELETE CASCADE,
    fingerprint_id  TEXT NOT NULL,
    name            TEXT NOT NULL,
    category        TEXT NOT NULL,
    verdict         TEXT NOT NULL,
    score           REAL NOT NULL,
    direct_layers   TEXT,
    indirect_layers TEXT,
    indirect_only   INTEGER NOT NULL DEFAULT 0,
    match_count     INTEGER NOT NULL DEFAULT 0,
    matches_json    TEXT,
    PRIMARY KEY (scan_id, fingerprint_id)
);

-- Gold, materialised. Derived entirely from scan+detection and rebuilt
-- whole; `meta.summary_state` records which data state it was built from,
-- so a reader can tell staleness apart instead of trusting it.
CREATE TABLE IF NOT EXISTS company_summary (
    target_id     TEXT PRIMARY KEY,
    judged        INTEGER NOT NULL,
    scans         INTEGER NOT NULL,
    good_scans    INTEGER NOT NULL,
    last_status   TEXT,
    last_scan_at  TEXT,
    last_good_at  TEXT,
    stack_json    TEXT NOT NULL,
    changes_json  TEXT NOT NULL,
    maturity_json TEXT NOT NULL,
    site_json     TEXT,
    computed_at   TEXT NOT NULL
);

-- One row per signal ever pushed to the webhook. The key is the signal's
-- stable identity; its presence here is what makes notifications once-only.
CREATE TABLE IF NOT EXISTS notification (
    key        TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    target_id  TEXT NOT NULL,
    sent_at    TEXT NOT NULL
);

-- A rep's verdict on one signal: ACKED (being worked) or DISMISSED. Keyed
-- by the same stable identity notifications use, so the verdict survives
-- the signal being recomputed every run.
CREATE TABLE IF NOT EXISTS signal_ack (
    key       TEXT PRIMARY KEY,
    state     TEXT NOT NULL,
    note      TEXT,
    noted_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS robots (
    origin     TEXT PRIMARY KEY,
    body       TEXT,
    note       TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    target_id   TEXT NOT NULL,
    url         TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'pending',
    claimed_by  TEXT,
    claimed_at  TEXT,
    finished_at TEXT,
    error       TEXT,
    scan_id     INTEGER,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    params_json  TEXT NOT NULL DEFAULT '{}',
    state        TEXT NOT NULL DEFAULT 'pending',
    requested_by TEXT,
    requested_at TEXT NOT NULL,
    claimed_by   TEXT,
    started_at   TEXT,
    finished_at  TEXT,
    output       TEXT,
    error        TEXT
);

CREATE INDEX IF NOT EXISTS task_by_state ON task(state, id);
CREATE INDEX IF NOT EXISTS job_by_state ON job(state, id);
CREATE INDEX IF NOT EXISTS job_by_run   ON job(run_id);
CREATE INDEX IF NOT EXISTS scan_by_target  ON scan(target_id, started_at);
CREATE INDEX IF NOT EXISTS scan_by_run     ON scan(run_id);
CREATE INDEX IF NOT EXISTS detection_by_fp ON detection(fingerprint_id, verdict);
CREATE INDEX IF NOT EXISTS detection_by_scan ON detection(scan_id);
"""


def _trusted_sql(column: str = "status") -> str:
    """`status IN (...)` built from the one list that defines trustworthiness.

    Every query that means "a real observation" goes through here, so
    widening or narrowing `TRUSTWORTHY_STATUSES` changes all of them at once
    instead of leaving five literal 'OK's quietly enforcing the old rule.
    """
    values = ", ".join(f"'{v}'" for v in sorted(ev.TRUSTWORTHY_STATUSES))
    return f"{column} IN ({values})"


class StoreError(RuntimeError):
    """Raised when the database cannot be opened or is not ours."""


def default_path() -> Path:
    return workspace.path("data", "radar.db")


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.conn = sqlite3.connect(self.path)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.execute("PRAGMA journal_mode = WAL")
            # The worker and the dashboard share this file; a write meeting
            # a write should wait its turn, not 500 a page.
            self.conn.execute("PRAGMA busy_timeout = 5000")
            self._migrate()
        except sqlite3.DatabaseError as exc:
            raise StoreError(f"{self.path} is not a usable database: {exc}") from exc

    def _migrate(self) -> None:
        self.conn.executescript(SCHEMA)
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self.conn.commit()
        else:
            self._upgrade(int(row["value"]))

    def _upgrade(self, found: int) -> None:
        """Move an older database forward, or refuse clearly.

        Migrations only ever add. Nothing here rewrites or drops a row,
        because the evidence in this file is the part that cannot be
        regenerated.
        """
        if found == SCHEMA_VERSION:
            return
        if found > SCHEMA_VERSION:
            raise StoreError(
                f"{self.path} was written by schema v{found}, "
                f"this build only understands v{SCHEMA_VERSION} — upgrade the tool")
        # `executescript` above creates anything new. Columns added to an
        # existing table need saying explicitly.
        if found < 4:
            existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(target)")}
            if "robots_policy" not in existing:
                self.conn.execute("ALTER TABLE target ADD COLUMN robots_policy TEXT "
                                  "NOT NULL DEFAULT 'respect'")
                self.conn.execute("ALTER TABLE target ADD COLUMN robots_note TEXT")
        if found < 6:
            existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(run)")}
            if "fingerprints_hash" not in existing:
                self.conn.execute("ALTER TABLE run ADD COLUMN fingerprints_hash TEXT")
        if found < 11:
            existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(company_summary)")}
            if "site_json" not in existing:
                self.conn.execute("ALTER TABLE company_summary ADD COLUMN site_json TEXT")
        self.conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'",
                          (str(SCHEMA_VERSION),))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # The tables fall into three layers, in the medallion sense, and the
    # split is the load-bearing design decision of the whole tool:
    #
    #   Bronze  scan.evidence_gz        raw, vendor-blind, never rewritten
    #           (only ever re-graded in status, and re-normalised for privacy)
    #   Silver  detection               derived from Bronze + fingerprints;
    #           thrown away and rebuilt wholesale by `redetect`
    #   Gold    stack_of / coverage /   computed from Silver; the sweeps are
    #           vendor_counts / signals  materialised in company_summary,
    #           keyed by `meta.summary_state` = a digest of the data they
    #           were computed from, and rebuilt whenever the key mismatches
    #           — so the copy exists but a stale copy cannot be *served*.
    #
    # Bronze plus the fingerprint files are the only truth. Everything above
    # is a cache of a function of them. Keep it that way: a second copy of a
    # fact is a second place for it to be wrong.

    # -- targets (watchlist; not a data layer) ---------------------------------

    def import_watchlist(self, watchlist, replace: bool = False) -> dict[str, int]:
        """Load a YAML watchlist into the database.

        The database is the watchlist — this is how one gets in, whether from
        the seed file that ships with the tool or from a file someone
        exported last month. Rows are never deleted: a company taken off the
        list keeps its history, and `enabled` records that we stopped looking
        rather than pretending we never did.

        With `replace`, targets absent from the file are disabled rather than
        removed, for the same reason.
        """
        now = _now()
        stats = {"industries": 0, "added": 0, "updated": 0, "disabled": 0}
        for position, code in enumerate(watchlist.industries):
            industry = watchlist.industry(code)
            self.conn.execute(
                "INSERT INTO industry (code, label, label_en, position) VALUES (?,?,?,?) "
                "ON CONFLICT(code) DO UPDATE SET label=excluded.label, "
                "label_en=excluded.label_en, position=excluded.position",
                (industry.code, industry.label, industry.label_en, position),
            )
            stats["industries"] += 1

        known = {r["id"] for r in self.conn.execute("SELECT id FROM target")}
        seen: set[str] = set()
        for target in watchlist:
            seen.add(target.id)
            stats["updated" if target.id in known else "added"] += 1
            self.upsert_target(
                id=target.id, company=target.company, company_en=target.company_en,
                industry=target.industry, tier=target.tier, note=target.note,
                urls=list(target.urls), enabled=target.enabled,
                robots_policy=target.robots_policy, robots_note=target.robots_note,
                _now=now)

        if replace:
            for row in self.conn.execute("SELECT id FROM target WHERE enabled = 1"):
                if row["id"] not in seen:
                    self.conn.execute("UPDATE target SET enabled = 0, last_seen = ? "
                                      "WHERE id = ?", (now, row["id"]))
                    stats["disabled"] += 1
        self.conn.commit()
        return stats

    def upsert_target(self, *, id: str, company: str, industry: str,
                      urls: list[str], company_en: str | None = None,
                      tier: str | None = None, note: str | None = None,
                      enabled: bool = True, robots_policy: str = "respect",
                      robots_note: str | None = None,
                      _now: str | None = None) -> None:
        """Create or update one watched company."""
        if robots_policy not in ("respect", "override"):
            raise ValueError(f"robots_policy must be respect or override, got {robots_policy!r}")
        if robots_policy == "override" and not (robots_note or "").strip():
            # An undocumented exception is one nobody can review later.
            raise ValueError("overriding robots.txt requires a reason in robots_note")
        now = _now or globals()["_now"]()
        self.conn.execute(
            "INSERT INTO target (id, company, company_en, industry, tier, note, "
            "urls_json, enabled, robots_policy, robots_note, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET company=excluded.company, "
            "company_en=excluded.company_en, industry=excluded.industry, "
            "tier=excluded.tier, note=excluded.note, urls_json=excluded.urls_json, "
            "enabled=excluded.enabled, robots_policy=excluded.robots_policy, "
            "robots_note=excluded.robots_note, last_seen=excluded.last_seen",
            (id, company, company_en, industry, tier, note,
             json.dumps(urls, ensure_ascii=False), int(enabled),
             robots_policy, robots_note, now, now),
        )
        if _now is None:
            self.conn.commit()

    def set_target_enabled(self, target_id: str, enabled: bool) -> bool:
        cur = self.conn.execute("UPDATE target SET enabled = ?, last_seen = ? WHERE id = ?",
                                (int(enabled), _now(), target_id))
        self.conn.commit()
        return cur.rowcount > 0

    def industries(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM industry ORDER BY position, code"))

    def upsert_industry(self, code: str, label: str, label_en: str = "",
                        position: int | None = None) -> None:
        if position is None:
            row = self.conn.execute("SELECT COALESCE(MAX(position), -1) + 1 AS p "
                                    "FROM industry").fetchone()
            position = row["p"]
        self.conn.execute(
            "INSERT INTO industry (code, label, label_en, position) VALUES (?,?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET label=excluded.label, "
            "label_en=excluded.label_en, position=excluded.position",
            (code, label, label_en, position))
        self.conn.commit()

    def targets(self, enabled_only: bool = False) -> list[sqlite3.Row]:
        clause = "WHERE enabled = 1" if enabled_only else ""
        return list(self.conn.execute(
            f"SELECT * FROM target {clause} ORDER BY industry, id"))

    def last_status_per_target(self) -> dict[str, str]:
        """The most recent scan status for each company, whatever it was."""
        latest: dict[str, str] = {}
        for row in self.conn.execute(
                "SELECT target_id, status FROM scan ORDER BY started_at DESC, id DESC"):
            latest.setdefault(row["target_id"], row["status"])
        return latest

    def target(self, target_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM target WHERE id = ?",
                                 (target_id,)).fetchone()

    # -- runs ----------------------------------------------------------------

    def start_run(self, target_count: int, note: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO run (started_at, target_count, note) VALUES (?,?,?)",
            (_now(), target_count, note),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, **fields: Any) -> None:
        allowed = {
            "collector_version", "detector_version",
            "fingerprint_files", "fingerprint_count", "fingerprints_hash",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown run fields: {sorted(unknown)}")
        assignments = ", ".join(f"{k}=?" for k in fields)
        sql = "UPDATE run SET finished_at=?" + (f", {assignments}" if fields else "") + " WHERE id=?"
        self.conn.execute(sql, (_now(), *fields.values(), run_id))
        self.conn.commit()

    def runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT r.*, (SELECT COUNT(*) FROM scan s WHERE s.run_id = r.id) AS scan_count "
            "FROM run r ORDER BY r.id DESC LIMIT ?", (limit,)
        ))

    def latest_run_id(self) -> int | None:
        row = self.conn.execute("SELECT MAX(id) AS id FROM run").fetchone()
        return row["id"] if row and row["id"] is not None else None

    # -- robots ----------------------------------------------------------------

    #: How long a site's robots.txt is reused. Long enough that we ask once a
    #: day rather than once a scan, short enough to pick up a change.
    ROBOTS_TTL_SECONDS = 24 * 3600

    def remembered_robots(self, origin: str) -> dict[str, Any] | None:
        """The last robots.txt we successfully read for this site.

        Kept because the fetch is not reliable. The same site answers with
        its rules one week and a 403 the next, and a tool that forgets would
        flip between honouring and ignoring them — which is both rude and,
        for a system built on comparing weeks, wrong.
        """
        row = self.conn.execute("SELECT * FROM robots WHERE origin = ?",
                                (origin,)).fetchone()
        if row is None:
            return None
        age = (datetime.now(UTC)
               - datetime.fromisoformat(row["fetched_at"])).total_seconds()
        return {"body": row["body"], "note": row["note"], "stale": age > self.ROBOTS_TTL_SECONDS}

    def remember_robots(self, origin: str, body: str | None, note: str) -> None:
        self.conn.execute(
            "INSERT INTO robots (origin, body, note, fetched_at) VALUES (?,?,?,?) "
            "ON CONFLICT(origin) DO UPDATE SET body=excluded.body, "
            "note=excluded.note, fetched_at=excluded.fetched_at",
            (origin, body, note, _now()))
        self.conn.commit()

    # -- jobs ------------------------------------------------------------------

    #: A worker that claimed a job and then died leaves it stuck. After this
    #: long the job goes back in the queue — long enough that a slow scan is
    #: never stolen from a worker still doing it.
    CLAIM_TIMEOUT_SECONDS = 15 * 60

    JOB_PENDING = "pending"
    JOB_CLAIMED = "claimed"
    JOB_DONE = "done"
    JOB_FAILED = "failed"

    def queue_run(self, jobs, note: str | None = None) -> int:
        """Create a run and put its work in the queue.

        Queueing rather than scanning inline is what lets the collector live
        somewhere other than the dashboard — on a laptop on an office
        network, for instance, rather than in a datacentre whose addresses
        far more sites turn away.
        """
        now = _now()
        run_id = self.start_run(target_count=len({j.target_id for j in jobs}), note=note)
        self.conn.executemany(
            "INSERT INTO job (run_id, target_id, url, created_at) VALUES (?,?,?,?)",
            [(run_id, job.target_id, job.url, now) for job in jobs])
        self.conn.commit()
        return run_id

    def claim_job(self, worker: str, run_id: int | None = None) -> sqlite3.Row | None:
        """Take the next job, atomically. Returns None when the queue is dry."""
        self._release_stale_claims()
        clause = "AND run_id = ?" if run_id is not None else ""
        params: tuple = (self.JOB_CLAIMED, worker, _now())
        params += (run_id,) if run_id is not None else ()
        row = self.conn.execute(
            "UPDATE job SET state = ?, claimed_by = ?, claimed_at = ? "
            "WHERE id = (SELECT id FROM job WHERE state = 'pending' "
            f"           {clause} ORDER BY id LIMIT 1) "
            "RETURNING *", params).fetchone()
        self.conn.commit()
        if row is None:
            return None
        # The worker needs the company's robots policy with the job, not a
        # second lookup it might forget to make.
        target = self.target(row["target_id"])
        job = dict(row)
        job["robots_policy"] = target["robots_policy"] if target else "respect"
        return job

    def _release_stale_claims(self) -> int:
        cutoff = (datetime.now(UTC)
                  - timedelta(seconds=self.CLAIM_TIMEOUT_SECONDS)).isoformat()
        cur = self.conn.execute(
            "UPDATE job SET state = 'pending', claimed_by = NULL, claimed_at = NULL "
            "WHERE state = 'claimed' AND claimed_at < ?", (cutoff,))
        return cur.rowcount

    def finish_job(self, job_id: int, scan_id: int) -> None:
        self.conn.execute(
            "UPDATE job SET state = ?, finished_at = ?, scan_id = ? WHERE id = ?",
            (self.JOB_DONE, _now(), scan_id, job_id))
        self.conn.commit()

    def fail_job(self, job_id: int, error: str) -> None:
        self.conn.execute(
            "UPDATE job SET state = ?, finished_at = ?, error = ? WHERE id = ?",
            (self.JOB_FAILED, _now(), error[:500], job_id))
        self.conn.commit()

    # -- tasks -----------------------------------------------------------------
    #
    # Maintenance work the dashboard asks for — a digest, a redetect, a
    # backup — goes through the same shape as scans: a row in a queue that a
    # worker claims. The dashboard stays a thing that reads and asks, never
    # a thing that does; that is what lets it sit on a small box.

    TASK_PENDING = "pending"
    TASK_RUNNING = "running"
    TASK_DONE = "done"
    TASK_FAILED = "failed"
    TASK_OUTPUT_LIMIT = 200_000
    #: A task still "running" after this long belongs to a worker that died.
    TASK_TIMEOUT_SECONDS = 60 * 60

    def queue_task(self, kind: str, params: dict[str, Any] | None = None,
                   requested_by: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO task (kind, params_json, requested_by, requested_at) "
            "VALUES (?,?,?,?)",
            (kind, json.dumps(params or {}, ensure_ascii=False), requested_by, _now()))
        self.conn.commit()
        return int(cur.lastrowid)

    def claim_task(self, worker: str) -> dict[str, Any] | None:
        cutoff = (datetime.now(UTC)
                  - timedelta(seconds=self.TASK_TIMEOUT_SECONDS)).isoformat()
        self.conn.execute(
            "UPDATE task SET state = 'failed', finished_at = ?, "
            "error = 'worker went away' WHERE state = 'running' AND started_at < ?",
            (_now(), cutoff))
        row = self.conn.execute(
            "UPDATE task SET state = ?, claimed_by = ?, started_at = ? "
            "WHERE id = (SELECT id FROM task WHERE state = 'pending' ORDER BY id LIMIT 1) "
            "RETURNING *", (self.TASK_RUNNING, worker, _now())).fetchone()
        self.conn.commit()
        if row is None:
            return None
        task = dict(row)
        task["params"] = json.loads(task["params_json"] or "{}")
        return task

    def finish_task(self, task_id: int, output: str) -> None:
        self.conn.execute(
            "UPDATE task SET state = ?, finished_at = ?, output = ? WHERE id = ?",
            (self.TASK_DONE, _now(), output[-self.TASK_OUTPUT_LIMIT:], task_id))
        self.conn.commit()

    def fail_task(self, task_id: int, error: str, output: str = "") -> None:
        self.conn.execute(
            "UPDATE task SET state = ?, finished_at = ?, error = ?, output = ? WHERE id = ?",
            (self.TASK_FAILED, _now(), error[:500],
             output[-self.TASK_OUTPUT_LIMIT:] or None, task_id))
        self.conn.commit()

    def task(self, task_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM task WHERE id = ?", (task_id,)).fetchone()

    def tasks(self, limit: int = 30) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT id, kind, params_json, state, requested_by, requested_at, claimed_by, "
            "started_at, finished_at, error, length(output) AS output_len "
            "FROM task ORDER BY id DESC LIMIT ?", (limit,)))

    def active_tasks(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT id, kind, state, started_at FROM task "
            "WHERE state IN ('pending', 'running') ORDER BY id"))

    # -- meta ------------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
        self.conn.commit()

    def run_progress(self, run_id: int) -> dict[str, Any]:
        counts = {r["state"]: r["n"] for r in self.conn.execute(
            "SELECT state, COUNT(*) AS n FROM job WHERE run_id = ? GROUP BY state",
            (run_id,))}
        total = sum(counts.values())
        finished = counts.get(self.JOB_DONE, 0) + counts.get(self.JOB_FAILED, 0)
        run = self.conn.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()
        return {
            "run_id": run_id,
            "note": run["note"] if run else None,
            "started_at": run["started_at"] if run else None,
            "finished_at": run["finished_at"] if run else None,
            "total": total,
            "done": counts.get(self.JOB_DONE, 0),
            "failed": counts.get(self.JOB_FAILED, 0),
            "pending": counts.get(self.JOB_PENDING, 0),
            "claimed": counts.get(self.JOB_CLAIMED, 0),
            "complete": total > 0 and finished == total,
        }

    def jobs_of(self, run_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT j.*, t.company FROM job j LEFT JOIN target t ON t.id = j.target_id "
            "WHERE j.run_id = ? ORDER BY j.id", (run_id,)))

    def active_runs(self) -> list[sqlite3.Row]:
        """Runs with work still in the queue."""
        return list(self.conn.execute(
            "SELECT DISTINCT run_id FROM job WHERE state IN ('pending','claimed') "
            "ORDER BY run_id DESC"))

    def open_runs(self) -> list[sqlite3.Row]:
        """Queued runs not yet marked finished — including drained ones.

        Distinct from `active_runs`, and the distinction is the whole point:
        a run whose jobs have all completed has nothing active left, which is
        exactly when it needs closing.
        """
        return list(self.conn.execute(
            "SELECT DISTINCT j.run_id FROM job j JOIN run r ON r.id = j.run_id "
            "WHERE r.finished_at IS NULL ORDER BY j.run_id DESC"))

    # -- scans (Bronze) ------------------------------------------------------

    def record_scan(self, run_id: int, target_id: str, result: dict,
                    keep_evidence: bool = True) -> int:
        scan = result.get("scan", {})
        blob = None
        if keep_evidence:
            payload = json.dumps(
                {"schema_version": result.get("schema_version"),
                 "scan": scan,
                 "evidence": result.get("evidence", {}),
                 "counts": result.get("counts", {}),
                 "warnings": result.get("warnings", [])},
                ensure_ascii=False,
            ).encode("utf-8")
            blob = gzip.compress(payload, compresslevel=6)

        cur = self.conn.execute(
            "INSERT INTO scan (run_id, target_id, url, final_url, page_host, status, "
            "http_status, title, error, block_marker, started_at, duration_ms, "
            "warnings_json, counts_json, evidence_gz) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id, target_id, scan.get("url"), scan.get("final_url"),
                scan.get("page_host"), scan.get("status"), scan.get("http_status"),
                scan.get("title"), scan.get("error"), scan.get("block_marker"),
                scan.get("started_at") or _now(), scan.get("duration_ms"),
                json.dumps(result.get("warnings", []), ensure_ascii=False),
                json.dumps(result.get("counts", {}), ensure_ascii=False),
                blob,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    #: A scan is treated as partial when it collected less than this share
    #: of what the same site usually gives us. Real sites vary by a percent
    #: or two between visits; this is for the ones that halve.
    PARTIAL_VOLUME_RATIO = 0.6
    #: ...and only once there is enough history to know what "usually" is.
    PARTIAL_MIN_HISTORY = 3

    def flag_partial_scan(self, scan_id: int) -> str | None:
        """Downgrade a scan that observed far less than this site normally does.

        Called after detection, because both halves matter: a page that
        fetched less but found the same vendors is fine, and only a drop in
        both is evidence that we saw part of a page rather than a changed
        one. Partial page loads must not manufacture vendor removals.
        """
        row = self.conn.execute(
            "SELECT target_id, status, counts_json FROM scan WHERE id = ?",
            (scan_id,)).fetchone()
        if row is None or row["status"] not in ev.TRUSTWORTHY_STATUSES:
            return None
        requests = (json.loads(row["counts_json"] or "{}") or {}).get("requests") or 0
        if not requests:
            return None

        history = [
            (json.loads(r["counts_json"] or "{}") or {}).get("requests") or 0
            for r in self.conn.execute(
                f"SELECT counts_json FROM scan WHERE target_id = ? AND {_trusted_sql()} "
                "AND id != ? ORDER BY id DESC LIMIT 8", (row["target_id"], scan_id))
        ]
        history = [h for h in history if h]
        if len(history) < self.PARTIAL_MIN_HISTORY:
            return None
        typical = sorted(history)[len(history) // 2]
        if requests >= typical * self.PARTIAL_VOLUME_RATIO:
            return None

        found = self.conn.execute(
            "SELECT COUNT(*) AS n FROM detection WHERE scan_id = ? AND verdict = 'DETECTED'",
            (scan_id,)).fetchone()["n"]
        previous = self.conn.execute(
            "SELECT COUNT(*) AS n FROM detection d JOIN scan s ON s.id = d.scan_id "
            f"WHERE s.target_id = ? AND {_trusted_sql('s.status')} AND s.id != ? "
            "AND d.verdict = 'DETECTED' AND s.id = "
            f"  (SELECT MAX(id) FROM scan WHERE target_id = ? AND {_trusted_sql()} AND id != ?)",
            (row["target_id"], scan_id, row["target_id"], scan_id)).fetchone()["n"]
        if previous and found >= previous:
            return None

        marker = (f"partial:{requests} requests against a usual {typical}, "
                  f"{found} vendors against {previous}")
        # Through `restatus_scan`, not a bare UPDATE: the evidence blob keeps
        # its own copy of the status and `redetect` trusts that copy. A row
        # that says PARTIAL over a blob that says OK is a removal that comes
        # back to life the next time someone re-judges the history.
        stored = self.load_evidence(scan_id)
        if stored is not None:
            stored.setdefault("scan", {})["status"] = "PARTIAL"
        self.restatus_scan(scan_id, "PARTIAL", marker, stored)
        return marker

    def load_evidence(self, scan_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT evidence_gz FROM scan WHERE id = ?", (scan_id,)
        ).fetchone()
        if row is None or row["evidence_gz"] is None:
            return None
        return json.loads(gzip.decompress(row["evidence_gz"]).decode("utf-8"))

    def rewrite_evidence(self, scan_id: int, stored: dict) -> None:
        """Replace a stored evidence blob in place."""
        payload = json.dumps(stored, ensure_ascii=False).encode("utf-8")
        self.conn.execute("UPDATE scan SET evidence_gz = ? WHERE id = ?",
                          (gzip.compress(payload, compresslevel=6), scan_id))
        self.conn.commit()

    def restatus_scan(self, scan_id: int, status: str, marker: str,
                      stored: dict | None = None) -> None:
        """Re-grade a stored scan under a newer status rule.

        The evidence blob is rewritten too, so the row and the blob never
        disagree about what this scan was.
        """
        self.conn.execute(
            "UPDATE scan SET status = ?, block_marker = ? WHERE id = ?",
            (status, marker, scan_id),
        )
        if stored is not None:
            payload = json.dumps(stored, ensure_ascii=False).encode("utf-8")
            self.conn.execute("UPDATE scan SET evidence_gz = ? WHERE id = ?",
                              (gzip.compress(payload, compresslevel=6), scan_id))
        self.conn.commit()

    #: Every scan column except the gzipped evidence. List views read scan
    #: rows by the thousand and never open the blob; dragging it along made
    #: one latest_scans() pass 20x slower than the metadata it wanted.
    _SCAN_META = ("id, run_id, target_id, url, final_url, page_host, status, "
                  "http_status, title, error, block_marker, started_at, "
                  "duration_ms, warnings_json, counts_json")

    def scans_for(self, target_id: str, limit: int = 50) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            f"SELECT {self._SCAN_META} FROM scan WHERE target_id = ? "
            f"ORDER BY started_at DESC, id DESC LIMIT ?",
            (target_id, limit),
        ))

    def scan_ids(self, run_id: int | None = None) -> list[int]:
        if run_id is None:
            rows = self.conn.execute("SELECT id FROM scan ORDER BY id")
        else:
            rows = self.conn.execute("SELECT id FROM scan WHERE run_id = ? ORDER BY id", (run_id,))
        return [r["id"] for r in rows]

    # -- detections (Silver) -------------------------------------------------

    def record_detections(self, scan_id: int, report: dict) -> int:
        """Replace this scan's detections. Derived data, so a full rewrite."""
        self.conn.execute("DELETE FROM detection WHERE scan_id = ?", (scan_id,))
        rows = [
            (
                scan_id, d["id"], d["name"], d["category"], d["verdict"], d["score"],
                ",".join(d.get("direct_layers", [])),
                ",".join(d.get("indirect_layers", [])),
                int(d.get("indirect_only", False)),
                d.get("match_count", 0),
                json.dumps(d.get("matches", []), ensure_ascii=False),
            )
            for d in report.get("detections", [])
        ]
        self.conn.executemany(
            "INSERT INTO detection (scan_id, fingerprint_id, name, category, verdict, "
            "score, direct_layers, indirect_layers, indirect_only, match_count, "
            "matches_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def detections_for(self, scan_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM detection WHERE scan_id = ? ORDER BY score DESC, name",
            (scan_id,),
        ))

    def detections_by_scan(self, scan_ids) -> dict[int, list[sqlite3.Row]]:
        """detections_for over many scans in one query, grouped by scan.

        The dashboard reads detections for every latest scan of every
        company on one page; fetching them row-set by row-set was the
        bigger half of an N+1.
        """
        ids = list(scan_ids)
        grouped: dict[int, list[sqlite3.Row]] = {i: [] for i in ids}
        if not ids:
            return grouped
        marks = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT * FROM detection WHERE scan_id IN ({marks}) "
            f"ORDER BY score DESC, name", ids)
        for row in rows:
            grouped[row["scan_id"]].append(row)
        return grouped

    def latest_scans_by_target(self, trustworthy_only: bool = True
                               ) -> dict[str, list[sqlite3.Row]]:
        """latest_scans(), grouped by target — one table read for all targets."""
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in self.latest_scans(trustworthy_only):
            grouped.setdefault(row["target_id"], []).append(row)
        return grouped

    def stacks_of(self, verdicts: tuple[str, ...] = ("DETECTED",)
                  ) -> dict[str, list[dict[str, Any]]]:
        """Every company's current stack in two queries total.

        The per-company `stack_of` is the right call for one detail page;
        list views calling it in a loop re-read the scan table once per
        company and the detection table once per scan. This is the same
        merge, batched.
        """
        by_target = self.latest_scans_by_target()
        detections = self.detections_by_scan(
            s["id"] for scans in by_target.values() for s in scans)
        return {
            target_id: self._merge_stack(scans, detections, verdicts)
            for target_id, scans in by_target.items()
        }

    @staticmethod
    def _merge_stack(scans: list[sqlite3.Row],
                     detections: dict[int, list[sqlite3.Row]],
                     verdicts: tuple[str, ...]) -> list[dict[str, Any]]:
        best: dict[str, dict[str, Any]] = {}
        for scan in scans:
            for det in detections.get(scan["id"], ()):
                if det["verdict"] not in verdicts:
                    continue
                row = dict(det)
                row["seen_on"] = scan["url"]
                current = best.get(row["fingerprint_id"])
                if current is None or row["score"] > current["score"]:
                    best[row["fingerprint_id"]] = row
        return sorted(best.values(), key=lambda r: (r["category"], -r["score"], r["name"]))

    def latest_scans(self, trustworthy_only: bool = True) -> list[sqlite3.Row]:
        """The most recent scan of each (target, url).

        Grouping by url as well as target is what makes extra URLs worth
        listing: a vendor that only loads on a product page still counts
        toward the company's stack.

        With `trustworthy_only`, a target whose last visit was blocked falls
        back to its last good visit instead of looking like an empty stack.
        """
        clause = f"WHERE {_trusted_sql()}" if trustworthy_only else ""
        rows = self.conn.execute(
            f"SELECT {self._SCAN_META} FROM scan {clause} "
            f"ORDER BY started_at DESC, id DESC"
        )
        latest: dict[tuple[str, str], sqlite3.Row] = {}
        for row in rows:
            latest.setdefault((row["target_id"], row["url"]), row)
        return list(latest.values())

    def stack_of(self, target_id: str,
                 verdicts: tuple[str, ...] = ("DETECTED",)) -> list[dict[str, Any]]:
        """A company's current stack: the union across its latest good scans.

        When two URLs disagree, the stronger verdict wins — a vendor present
        on the product page but not the home page is present.
        """
        scans = self.latest_scans_by_target().get(target_id, [])
        detections = self.detections_by_scan(s["id"] for s in scans)
        return self._merge_stack(scans, detections, verdicts)

    def coverage(self, target_id: str | None = None) -> dict[str, dict[str, Any]]:
        """Whether each target has ever been judged, and how it last went.

        An empty stack means two very different things: "we looked and the
        company runs nothing" and "we have never managed to look". Collapsing
        them turns every bot-walled site into a fake greenfield lead, so the
        difference is answered here rather than inferred from an empty list.
        """
        report: dict[str, dict[str, Any]] = {}
        clause = "WHERE target_id = ? " if target_id else ""
        rows = self.conn.execute(
            f"SELECT target_id, status, started_at, url FROM scan "
            f"{clause}ORDER BY started_at DESC, id DESC",
            (target_id,) if target_id else (),
        )
        for row in rows:
            entry = report.setdefault(row["target_id"], {
                "target_id": row["target_id"],
                "last_status": row["status"],
                "last_scan_at": row["started_at"],
                "scans": 0,
                "good_scans": 0,
                "judged": False,
                "last_good_at": None,
            })
            entry["scans"] += 1
            if row["status"] in ev.TRUSTWORTHY_STATUSES:
                entry["good_scans"] += 1
                entry["judged"] = True
                if entry["last_good_at"] is None:
                    entry["last_good_at"] = row["started_at"]
        return report

    def vendor_counts(self, verdicts: tuple[str, ...] = ("DETECTED",)) -> list[dict[str, Any]]:
        """How many distinct targets currently run each vendor."""
        per_vendor: dict[str, dict[str, Any]] = {}
        for stack in self.stacks_of(verdicts).values():
            for row in stack:
                entry = per_vendor.setdefault(row["fingerprint_id"], {
                    "fingerprint_id": row["fingerprint_id"],
                    "name": row["name"],
                    "category": row["category"],
                    "targets": 0,
                })
                entry["targets"] += 1
        return sorted(per_vendor.values(), key=lambda r: (-r["targets"], r["name"]))


    # -- company summaries (Gold, materialised) ------------------------------

    def backup(self, dest: Path | str) -> Path:
        """Copy the database with SQLite's online backup — safe while the
        worker is writing, unlike copying the file and its WAL by hand."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        out = sqlite3.connect(tmp)
        try:
            self.conn.backup(out)
        finally:
            out.close()
        tmp.replace(dest)
        return dest

    def data_state(self) -> str:
        """A digest of everything the summaries are a function of.

        Any new scan, re-grade or redetect changes at least one term, and
        that is the whole invalidation story: mismatch means rebuild.
        """
        row = self.conn.execute(
            "SELECT (SELECT COALESCE(MAX(id), 0) FROM scan) AS scan_high, "
            "       (SELECT COUNT(*) FROM scan) AS scans, "
            f"      (SELECT COALESCE(SUM({_trusted_sql()}), 0) FROM scan) AS good, "
            "       (SELECT COUNT(*) FROM detection) AS detections, "
            "       (SELECT COALESCE(MAX(rowid), 0) FROM detection) AS detection_high"
        ).fetchone()
        # `good` moves when a scan is re-graded; `detection_high` moves on
        # every redetect even when the count does not.
        return (f"{row['scan_high']}:{row['scans']}:{row['good']}:"
                f"{row['detections']}:{row['detection_high']}")

    def summary_state(self) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = 'summary_state'").fetchone()
        return row["value"] if row else None

    def replace_summaries(self, rows: list[dict[str, Any]], state: str) -> None:
        """Swap in a freshly computed summary set, atomically with its key."""
        now = _now()
        self.conn.execute("DELETE FROM company_summary")
        self.conn.executemany(
            "INSERT INTO company_summary (target_id, judged, scans, good_scans, "
            "last_status, last_scan_at, last_good_at, stack_json, changes_json, "
            "maturity_json, site_json, computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(r["target_id"], int(r["judged"]), r["scans"], r["good_scans"],
              r["last_status"], r["last_scan_at"], r["last_good_at"],
              r["stack_json"], r["changes_json"], r["maturity_json"],
              r.get("site_json"), now)
             for r in rows],
        )
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES ('summary_state', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (state,))
        self.conn.commit()

    def summary_rows(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM company_summary"))


    def identifiers_of(self, target_id: str) -> list[dict[str, Any]]:
        """Vendor account IDs seen on this company's latest good scans.

        The collector already extracts them (GTM containers, GA4 streams,
        ad-account IDs, partner slugs); this surfaces them per company —
        which *contract* a brand runs, not merely which vendor.
        """
        found: dict[tuple[str, str], dict[str, Any]] = {}
        for scan in self.latest_scans_by_target().get(target_id, []):
            stored = self.load_evidence(scan["id"])
            if stored is None:
                continue
            for ident in (stored.get("evidence") or {}).get("identifiers") or []:
                key = (ident.get("kind", ""), ident.get("value", ""))
                if key[1] and key not in found:
                    found[key] = {"kind": key[0], "value": key[1],
                                  "seen_on": scan["url"]}
        return sorted(found.values(), key=lambda r: (r["kind"], r["value"]))

    # -- signal acknowledgements ----------------------------------------------

    ACK_STATES = ("ACKED", "DISMISSED")

    def signal_acks(self) -> dict[str, sqlite3.Row]:
        return {r["key"]: r for r in self.conn.execute("SELECT * FROM signal_ack")}

    def set_signal_ack(self, key: str, state: str | None,
                       note: str | None = None) -> None:
        """Record a verdict; an empty state clears it (back to open)."""
        if not state:
            self.conn.execute("DELETE FROM signal_ack WHERE key = ?", (key,))
        else:
            if state not in self.ACK_STATES:
                raise ValueError(f"unknown ack state {state!r}")
            self.conn.execute(
                "INSERT INTO signal_ack (key, state, note, noted_at) VALUES (?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET state=excluded.state, "
                "note=excluded.note, noted_at=excluded.noted_at",
                (key, state, note, _now()))
        self.conn.commit()

    # -- notifications --------------------------------------------------------

    def notified_keys(self) -> set[str]:
        return {r["key"] for r in self.conn.execute("SELECT key FROM notification")}

    def record_notified(self, rows: list[tuple[str, str, str]], sent_at: str) -> None:
        self.conn.executemany(
            "INSERT OR IGNORE INTO notification (key, kind, target_id, sent_at) "
            "VALUES (?,?,?,?)",
            [(key, kind, target_id, sent_at) for key, kind, target_id in rows])
        self.conn.commit()


@contextmanager
def open_store(path: Path | str | None = None) -> Iterator[Store]:
    store = Store(path)
    try:
        yield store
    finally:
        store.close()
