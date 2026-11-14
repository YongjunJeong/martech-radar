"""Read-only heartbeat probe for the single always-on collection worker."""

from contextlib import closing
from datetime import UTC, datetime
import json
import sqlite3

from radar.workspace import path


def main():
    with closing(
        sqlite3.connect(path("data/radar.db").as_uri() + "?mode=ro", uri=True, timeout=2)
    ) as db:
        row = db.execute("SELECT value FROM meta WHERE key='worker_seen'").fetchone()
    if not row:
        raise SystemExit("Worker heartbeat missing")
    seen = datetime.fromisoformat(json.loads(row[0])["at"])
    age = (datetime.now(UTC) - seen).total_seconds()
    if not 0 <= age <= 120:
        raise SystemExit("Worker heartbeat stale")


if __name__ == "__main__":
    main()
