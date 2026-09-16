"""Create fictional observations offline; never reads an existing workspace."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path

from radar import detector, registry
from radar.store import Store
from radar.targets import Target, TargetSet


def seed(destination: Path) -> Path:
    # exist_ok=False deliberately refuses to overwrite any existing workspace.
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "radar.toml").write_text(
        '# Synthetic portfolio demo; no webhook or preferred vendor.\n'
        '[vendor]\nhome = ""\n[notify]\nwebhook = ""\n', encoding="utf-8")
    targets = tuple(
        Target(id=key, company=f"[DEMO] {name}", industry="retail",
               urls=(f"https://{key}.example/",), note="Synthetic observations; not scanned.")
        for key, name in (("shop_a", "Sample Store A"), ("shop_b", "Sample Store B"),
                          ("walled", "Blocked Store"))
    )
    rules = registry.load(registry.shipped_dir())
    database = destination / "data" / "radar.db"
    with Store(database) as store:
        store.import_watchlist(TargetSet(targets=targets, industries={"retail": "[DEMO] Retail"},
                                         source="synthetic-demo"))
        for week in range(3):
            stamp = (datetime.now(UTC) - timedelta(days=14 - week * 7)).isoformat()
            run = store.start_run(len(targets), note="SYNTHETIC DEMO — no sites visited")
            for target in targets:
                blocked = target.id == "walled"
                hosts = [] if blocked else [
                    "connect.facebook.net", "static.criteo.net", "cdn.taboola.com",
                    "widgets.outbrain.com", "www.googletagmanager.com",
                    "www.google-analytics.com", "static.hotjar.com", "api.amplitude.com",
                ]
                if target.id == "shop_b":
                    hosts.append("braze.com" if week == 0 else "moengage.com")
                result = {
                    "schema_version": 1,
                    "scan": {"url": target.urls[0], "final_url": target.urls[0],
                             "page_host": f"{target.id}.example",
                             "status": "BLOCKED" if blocked else "OK",
                             "http_status": 403 if blocked else 200,
                             "title": target.company, "started_at": stamp,
                             "duration_ms": 0},
                    "evidence": {"network": {"hosts": hosts}},
                    "counts": {"requests": len(hosts)},
                    "warnings": ["Synthetic fixture; not a real scan."],
                }
                scan = store.record_scan(run, target.id, result)
                store.record_detections(scan, detector.detect(result, rules))
            store.finish_run(run, fingerprint_count=len(rules),
                             fingerprints_hash=rules.content_hash)
            store.conn.execute("UPDATE run SET started_at=?, finished_at=? WHERE id=?",
                               (stamp, stamp, run))
            store.conn.commit()
    return database


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="A new, nonexistent directory")
    args = parser.parse_args()
    try:
        print(f"Synthetic demo created: {seed(args.destination)}")
    except FileExistsError:
        parser.exit(1, "Destination exists; choose a new directory. Nothing overwritten.\n")
