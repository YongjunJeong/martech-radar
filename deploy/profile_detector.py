"""Read-only detector timing against saved evidence; no website visits or writes."""

import argparse
import gzip
import json
from pathlib import Path
import sqlite3
import statistics
import time

from radar.detector import detect
from radar.registry import load


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=60)
    args = parser.parse_args()
    if not 1 <= args.limit <= 1000:
        parser.error("limit must be 1..1000")
    registry = load()
    timings, durations = [], []
    with sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True, timeout=2) as db:
        for raw, duration in db.execute(
            "SELECT evidence_gz,duration_ms FROM scan WHERE status='OK' AND evidence_gz IS NOT NULL ORDER BY id DESC LIMIT ?",
            (args.limit,),
        ):
            evidence = json.loads(gzip.decompress(raw))
            detect(evidence, registry)
            repeated = []
            for _ in range(3):
                before = time.perf_counter()
                detect(evidence, registry)
                repeated.append((time.perf_counter() - before) * 1000)
            timings.append(statistics.median(repeated))
            durations.append(duration or 0)
    print(
        json.dumps(
            {
                "sample_count": len(timings),
                "selection": "latest OK scans with saved evidence",
                "detector_median_ms": statistics.median(timings) if timings else None,
                "detector_max_ms": max(timings, default=0),
                "sum_detector_ms": sum(timings),
                "sum_recorded_browser_ms": sum(durations),
                "detector_fraction_of_sequential_browser_plus_detector": sum(timings)
                / (sum(durations) + sum(timings))
                if sum(durations) + sum(timings)
                else None,
                "note": "Not a Rust benchmark or concurrent batch wall-time estimate; excludes evidence decompression and DB I/O.",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
