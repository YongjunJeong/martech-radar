"""Command line entry point for the evidence collector."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from pathlib import Path

from .collector import Collector, ScanConfig
from . import evidence as ev
from . import config as cfg
from . import registry as reg
from . import detector as det
from . import targets as tgt
from . import batch as bat
from . import security
from . import worker as wrk
from . import tasks as tsk
from . import workspace
from . import signals as sig
from . import brief as brf
from . import strings as st
from .store import Store, StoreError


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="radar",
        description="Korea MarTech Deal Radar — browser evidence collector",
    )
    parser.add_argument("--home", metavar="DIR",
                        help="workspace: where radar.toml, targets.yaml and data/ live "
                             "(default: $RADAR_HOME, else the current directory)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("home", help="print the workspace this command would use")

    scan = sub.add_parser("scan", help="scan a single public URL")
    scan.add_argument("url")
    scan.add_argument("-o", "--out", help="write the full JSON result to this file")
    scan.add_argument("--json", action="store_true", help="print full JSON to stdout")
    scan.add_argument("--no-interact", action="store_true",
                      help="skip the scroll/mouse nudge (faster, sees less)")
    scan.add_argument("--no-js-bodies", action="store_true",
                      help="do not read script bodies for referenced hostnames")
    scan.add_argument("--settle-ms", type=int, default=ScanConfig.settle_ms)
    scan.add_argument("--nav-timeout-ms", type=int, default=ScanConfig.nav_timeout_ms)
    scan.add_argument("--headful", action="store_true", help="show the browser window")
    scan.add_argument("--no-detect", action="store_true",
                      help="collect evidence only, skip vendor detection")
    scan.add_argument("--default-ua", action="store_true",
                      help="use Playwright's own headless user agent instead of desktop Chrome")
    detect = sub.add_parser(
        "detect", help="classify vendors in already-collected scan JSON")
    detect.add_argument("paths", nargs="+",
                        help="scan JSON files, or directories containing them")
    detect.add_argument("--json", action="store_true", help="print full JSON to stdout")
    detect.add_argument("--fingerprints", help="fingerprint directory (default: ./fingerprints)")
    detect.add_argument("--min-verdict", choices=["PROBABLE", "DETECTED"], default="PROBABLE")
    detect.add_argument("--category", action="append",
                        help="only show these categories (repeatable)")
    detect.add_argument("--why", action="store_true",
                        help="print the matched evidence behind every verdict")

    wl = sub.add_parser("watchlist", help="the watched companies, stored in the database")
    wl_sub = wl.add_subparsers(dest="watchlist_command", required=True)
    wl_import = wl_sub.add_parser("import", help="load a YAML watchlist into the database")
    wl_import.add_argument("path", nargs="?", help="default: ./targets.yaml")
    wl_import.add_argument("--db")
    wl_import.add_argument("--replace", action="store_true",
                           help="disable companies missing from the file (never deletes)")
    wl_export = wl_sub.add_parser("export", help="write the database watchlist back to YAML")
    wl_export.add_argument("-o", "--out", help="default: stdout")
    wl_export.add_argument("--db")
    wl_list = wl_sub.add_parser("list", help="what the database currently watches")
    wl_list.add_argument("--db")
    wl_list.add_argument("--json", action="store_true")

    task = sub.add_parser(
        "task", help="run a maintenance task now — what a worker runs when it is "
                     "queued from the dashboard")
    task.add_argument("kind", choices=tsk.KINDS)
    task.add_argument("--db", help="database path (default: ./data/radar.db)")
    task.add_argument("--fingerprints")
    task.add_argument("--dry-run", action="store_true", help="notify: list, send nothing")

    fps = sub.add_parser("fingerprints", help="list and validate the fingerprint registry")
    fps.add_argument("--fingerprints", help="fingerprint directory (default: ./fingerprints)")
    fps.add_argument("--json", action="store_true")

    for name, help_text in (
        ("batch", "scan the whole watchlist and store the results"),
        ("redetect", "re-classify stored evidence against current fingerprints"),
        ("targets", "list and validate the watchlist"),
        ("stack", "show a company's current stack from the database"),
        ("runs", "list stored batch runs"),
        ("signals", "sales signals from the accumulated history"),
        ("changes", "per-vendor change states for one company"),
        ("serve", "run the dashboard"),
        ("worker", "claim queued scans and run them here"),
        ("brief", "compact evidence-backed brief for one company"),
        ("digest", "weekly summary of signals and run health"),
        ("unknown-hosts", "third-party hosts no fingerprint accounts for"),
        ("notify", "push not-yet-notified signals to the configured webhook"),
        ("hygiene", "targets whose recent scans keep failing — dead or moved domains"),
        ("suggest-urls", "product-page candidates for single-URL targets, from stored evidence"),
        ("probable", "vendors stuck at PROBABLE — fingerprints worth strengthening"),
        ("backup", "snapshot the database into backups/ and prune old snapshots"),
    ):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--db", help="database path (default: ./data/radar.db)")
        if name in ("serve", "digest", "brief", "signals", "notify"):
            cmd.add_argument("--lang", choices=st.languages(),
                             help="override the language in radar.toml")
        if name == "probable":
            cmd.add_argument("--json", action="store_true")
        if name == "backup":
            cmd.add_argument("--dir", help="where snapshots go (default: <workspace>/backups)")
            cmd.add_argument("--keep", type=int, default=14,
                             help="how many snapshots to keep (default 14)")
        if name == "suggest-urls":
            cmd.add_argument("--max-per-target", type=int, default=1)
            cmd.add_argument("--json", action="store_true")
        if name == "hygiene":
            cmd.add_argument("--streak", type=int, default=3,
                             help="flag after this many consecutive failed attempts (default 3)")
            cmd.add_argument("--json", action="store_true")
        if name == "notify":
            cmd.add_argument("--dry-run", action="store_true",
                             help="list what would be sent; record and send nothing")
            cmd.add_argument("--webhook", help="override [notify] webhook from radar.toml")
        if name == "unknown-hosts":
            cmd.add_argument("--fingerprints", help="fingerprint directory")
            cmd.add_argument("--min-targets", type=int, default=2,
                             help="hide domains seen on fewer companies (default 2)")
            cmd.add_argument("--limit", type=int, default=40)
            cmd.add_argument("--include-noise", action="store_true",
                             help="also list domains on the built-in noise list")
            cmd.add_argument("--include-exchanges", action="store_true",
                             help="also list programmatic SSP/DSP sync domains")
            cmd.add_argument("--json", action="store_true")
        if name == "worker":
            cmd.add_argument("--run", type=int, help="only this run's jobs")
            cmd.add_argument("--concurrency", type=int, default=bat.DEFAULT_CONCURRENCY)
            cmd.add_argument("--name", help="how this worker appears in the queue")
            cmd.add_argument("--watch", action="store_true",
                             help="stay running and wait for new work")
            cmd.add_argument("--fingerprints")
            cmd.add_argument("--settle-ms", type=int, default=ScanConfig.settle_ms)
        if name == "serve":
            cmd.add_argument("--host", default="127.0.0.1")
            cmd.add_argument("--port", type=int, default=8848)
            cmd.add_argument("--targets", dest="targets_file")
            cmd.add_argument("--fingerprints")
        if name == "targets":
            cmd.add_argument("--targets", dest="targets_file",
                             help="watchlist file to validate (default: ./targets.yaml)")
        if name in ("batch", "redetect"):
            cmd.add_argument("--fingerprints", help="fingerprint directory")
        if name == "batch":
            cmd.add_argument("--only", action="append",
                             help="scan only these target ids (repeatable)")
            cmd.add_argument("--industry", action="append",
                             help="scan only these industries (repeatable)")
            cmd.add_argument("--tier", action="append",
                             help="scan only these watchlist tiers; \"none\" = untiered (repeatable)")
            cmd.add_argument("--due", action="store_true",
                             help="skip targets scanned more recently than their "
                                  "tier's [cadence] interval in radar.toml")
            cmd.add_argument("--limit", type=int, help="scan at most this many targets")
            cmd.add_argument("--concurrency", type=int, default=bat.DEFAULT_CONCURRENCY)
            cmd.add_argument("--settle-ms", type=int, default=ScanConfig.settle_ms)
            cmd.add_argument("--no-interact", action="store_true")
            cmd.add_argument("--no-evidence", action="store_true",
                             help="do not store raw evidence (breaks future redetect)")
            cmd.add_argument("--note", help="label this run")
        if name == "redetect":
            cmd.add_argument("--run", type=int, help="only this run id (default: all scans)")
            cmd.add_argument("--diff", action="store_true",
                             help="list every verdict the re-judging gained or lost")
        if name == "stack":
            cmd.add_argument("target_id")
            cmd.add_argument("--all", action="store_true",
                             help="include PROBABLE, not just DETECTED")
        if name == "signals":
            cmd.add_argument("--kind", action="append", choices=sorted(sig.PRIORITY),
                             help="only these signal kinds (repeatable)")
            cmd.add_argument("--industry", action="append")
        if name == "brief":
            cmd.add_argument("target_id")
        if name == "digest":
            cmd.add_argument("-o", "--out", help="write to this file as well as stdout")
        if name == "changes":
            cmd.add_argument("target_id")
            cmd.add_argument("--all", action="store_true",
                             help="include STABLE, not just what moved")
        if name in ("targets", "stack", "runs", "signals", "changes"):
            cmd.add_argument("--json", action="store_true")

    return parser


def _summary(result: dict) -> str:
    scan = result["scan"]
    lines = [
        f"  URL       {scan['url']}",
        f"  Final     {scan.get('final_url') or '-'}",
        f"  Status    {scan['status']}  (http {scan.get('http_status')})  {scan.get('duration_ms')}ms",
        f"  Title     {scan.get('title') or '-'}",
    ]
    if scan.get("error"):
        lines.append(f"  Error     {scan['error']}")
    if scan.get("block_marker"):
        lines.append(f"  Blocked   {scan['block_marker']}")

    counts = result.get("counts") or {}
    if counts:
        lines.append("")
        lines.append("  Evidence collected")
        for key, value in counts.items():
            lines.append(f"    {key:<20} {value}")

    e = result.get("evidence") or {}
    third_party = (e.get("network") or {}).get("third_party_domains") or []
    if third_party:
        lines.append("")
        lines.append(f"  Third-party domains contacted ({len(third_party)})")
        for domain in third_party[:40]:
            lines.append(f"    {domain}")
        if len(third_party) > 40:
            lines.append(f"    … and {len(third_party) - 40} more")

    csp_only = sorted(set(e.get("csp_hosts") or []) - set((e.get("network") or {}).get("hosts") or []))
    if csp_only:
        lines.append("")
        lines.append(f"  Allowed by CSP but not contacted during this visit ({len(csp_only)})")
        for host in csp_only[:30]:
            lines.append(f"    {host}")

    referenced_only = sorted(
        set(e.get("referenced_hosts") or []) - set((e.get("network") or {}).get("hosts") or [])
    )
    if referenced_only:
        lines.append("")
        lines.append(f"  Named inside script source but not contacted ({len(referenced_only)})")
        for host in referenced_only[:30]:
            lines.append(f"    {host}")

    identifiers = e.get("identifiers") or []
    if identifiers:
        lines.append("")
        lines.append("  Vendor account identifiers")
        for item in identifiers[:25]:
            lines.append(f"    {item['kind']:<18} {item['value']}")

    for warning in result.get("warnings", [])[:10]:
        lines.append(f"  ! {warning}")
    return "\n".join(lines)


_VERDICT_ORDER = {det.VERDICT_PROBABLE: 0, det.VERDICT_DETECTED: 1}


def _say(message: str) -> None:
    print(message, flush=True)


def _load_registry(path: str | None) -> reg.Registry:
    return reg.load(Path(path) if path else None)


def _detection_lines(report: dict, min_verdict: str, categories: list[str] | None,
                     why: bool) -> list[str]:
    host = report.get("page_host") or report.get("url") or "?"
    if not report.get("judged"):
        return [f"{host}: not judged — {report['reason']}"]

    floor = _VERDICT_ORDER[min_verdict]
    rows = [
        d for d in report["detections"]
        if _VERDICT_ORDER[d["verdict"]] >= floor
        and (not categories or d["category"] in categories)
    ]
    lines = [f"{host}  ({len(rows)} of {len(report['detections'])} shown)"]

    current = None
    for d in sorted(rows, key=lambda d: (d["category"], -d["score"], d["name"].lower())):
        if d["category"] != current:
            current = d["category"]
            lines.append(f"\n  [{current}]")
        if d["indirect_only"]:
            trail = "referenced only: " + ", ".join(d["indirect_layers"])
        else:
            trail = ", ".join(d["direct_layers"])
            if d["indirect_layers"]:
                trail += " (+" + ", ".join(d["indirect_layers"]) + ")"
        lines.append(f"    {d['verdict']:9} {d['score']:4.1f}  {d['name']:38} {trail}")
        if why:
            for m in d["matches"]:
                lines.append(
                    f"             {m['layer']:9} {m['strength']:6} "
                    f"{m['pattern']:34} <- {m['value']}"
                )
    return lines


def _scan_json_files(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            files.extend(sorted(path.glob("*.json")))
        else:
            files.append(path)
    return files


def _cmd_scan(args: argparse.Namespace) -> int:
    config = ScanConfig(
        interact=not args.no_interact,
        scan_js_bodies=not args.no_js_bodies,
        settle_ms=args.settle_ms,
        nav_timeout_ms=args.nav_timeout_ms,
        headless=not args.headful,
    )
    if args.default_ua:
        config.user_agent = None

    async def run() -> dict:
        async with Collector(config) as collector:
            return await collector.scan(args.url)

    result = asyncio.run(run())

    report: dict | None = None
    if not args.no_detect:
        try:
            report = det.detect(result, _load_registry(None))
        except reg.RegistryError as exc:
            print(f"fingerprint registry unusable: {exc}", file=sys.stderr)

    if args.out:
        payload = dict(result)
        if report is not None:
            payload["detection"] = report
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    if args.json:
        payload = dict(result)
        if report is not None:
            payload["detection"] = report
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(_summary(result))
        if report is not None:
            print()
            print("\n".join(_detection_lines(report, "PROBABLE", None, False)))
        if args.out:
            print(f"\n  full JSON → {args.out}")

    return 0 if result["scan"]["status"] in ev.TRUSTWORTHY_STATUSES else 1


def _cmd_detect(args: argparse.Namespace) -> int:
    try:
        registry = _load_registry(args.fingerprints)
    except reg.RegistryError as exc:
        print(f"fingerprint registry unusable: {exc}", file=sys.stderr)
        return 2

    files = _scan_json_files(args.paths)
    if not files:
        print("no scan JSON found", file=sys.stderr)
        return 2

    reports = []
    failed = 0
    for path in files:
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"{path}: unreadable ({exc})", file=sys.stderr)
            failed += 1
            continue
        reports.append(det.detect(result, registry))

    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    else:
        for report in reports:
            print("\n".join(_detection_lines(
                report, args.min_verdict, args.category, args.why)))
            print()
    return 1 if failed else 0


def _cmd_fingerprints(args: argparse.Namespace) -> int:
    try:
        registry = _load_registry(args.fingerprints)
    except reg.RegistryError as exc:
        print(f"fingerprint registry unusable: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps([
            {
                "id": fp.id, "name": fp.name, "category": fp.category,
                "signals": [
                    {"field": s.field, "pattern": s.pattern, "strength": s.strength}
                    for s in fp.signals
                ],
            }
            for fp in registry
        ], ensure_ascii=False, indent=2))
        return 0

    print(f"{len(registry)} fingerprints, {reg.signal_count(registry)} signals, "
          f"from {len(registry.source_files)} files")
    for category, count in reg.summarise(registry):
        print(f"\n  [{category}] {count}")
        for fp in registry.by_category(category):
            strong = sum(1 for s in fp.signals if s.strength == reg.STRENGTH_STRONG)
            weak = len(fp.signals) - strong
            fields = ",".join(sorted({s.field for s in fp.signals}))
            print(f"    {fp.id:32} {fp.name:38} {strong:>2}s/{weak:<2}w  {fields}")
    return 0


def _cmd_targets(args: argparse.Namespace) -> int:
    try:
        watchlist = tgt.load(args.targets_file)
    except tgt.TargetError as exc:
        print(f"watchlist unusable: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps([
            {"id": t.id, "company": t.company, "company_en": t.company_en,
             "industry": t.industry, "industry_label": watchlist.industry_label(t.industry),
             "tier": t.tier, "urls": list(t.urls), "enabled": t.enabled, "note": t.note}
            for t in watchlist
        ], ensure_ascii=False, indent=2))
        return 0

    disabled = len(watchlist) - len(watchlist.enabled())
    print(f"{len(watchlist)} targets, {sum(len(t.urls) for t in watchlist)} urls, "
          f"{len(watchlist.by_industry())} industries"
          + (f", {disabled} disabled" if disabled else ""))
    for code, group in watchlist.by_industry().items():
        print(f"\n  [{code}] {watchlist.industry_label(code)}")
        for t in group:
            flag = "" if t.enabled else "  (disabled)"
            extra = f"  +{len(t.urls) - 1} url" if len(t.urls) > 1 else ""
            print(f"    {t.id:16} {t.company:20} {t.primary_url}{extra}{flag}")
    return 0


def _cmd_batch(args: argparse.Namespace) -> int:
    try:
        registry = _load_registry(args.fingerprints)
    except reg.RegistryError as exc:
        print(f"cannot start: {exc}", file=sys.stderr)
        return 2

    config = ScanConfig(
        settle_ms=args.settle_ms,
        interact=not args.no_interact,
    )

    with Store(args.db) as store:
        if not store.targets():
            print("the watchlist is empty — load one with "
                  "`radar watchlist import targets.yaml`", file=sys.stderr)
            return 2
        if args.only:
            missing = set(args.only) - {r["id"] for r in store.targets()}
            if missing:
                print(f"unknown target ids: {sorted(missing)}", file=sys.stderr)
                return 2
        jobs = bat.jobs_for(store, target_ids=args.only,
                            industries=args.industry, limit=args.limit,
                            tiers=args.tier)
        if args.due:
            jobs, fresh = bat.due_split(store, jobs)
            if fresh:
                # Say what was narrowed, or a cron of --due batches reads as
                # full coverage when it is anything but.
                print(f"  {len(fresh)} targets not due yet (scanned within "
                      f"their tier's cadence) — skipped")
        if not jobs:
            print("nothing to scan after filtering", file=sys.stderr)
            return 2
        report = asyncio.run(bat.run_batch(
            store, registry, jobs,
            scan_config=config,
            concurrency=args.concurrency,
            keep_evidence=not args.no_evidence,
            note=args.note,
            progress=_say,
        ))
        print(f"\n{report.summary()}")
        if report.failed:
            print("  not judged:")
            for o in report.failed:
                detail = f" — {o.error}" if o.error else ""
                print(f"    {o.target_id:20} {o.status:12} {o.url}{detail}")
        print(f"  database: {store.path}")

    return 0 if report.ok else 1


def _cmd_redetect(args: argparse.Namespace) -> int:
    try:
        registry = _load_registry(args.fingerprints)
    except reg.RegistryError as exc:
        print(f"fingerprint registry unusable: {exc}", file=sys.stderr)
        return 2
    with Store(args.db) as store:
        stats = bat.redetect(store, registry, run_id=args.run, progress=_say)
        if args.diff and (stats["gained"] or stats["lost"]):
            hosts = {r["id"]: (r["target_id"], r["page_host"]) for r in store.conn.execute(
                "SELECT id, target_id, page_host FROM scan")}
            _say(f"  fingerprints {stats['fingerprints_hash']}")
            for label, rows in (("+", stats["gained"]), ("-", stats["lost"])):
                for scan_id, vendor in rows:
                    target, host = hosts.get(scan_id, ("?", "?"))
                    _say(f"  {label} {vendor:24} {target:14} scan {scan_id} ({host})")
    return 0 if stats["scans"] else 1


def _cmd_stack(args: argparse.Namespace) -> int:
    verdicts = ("DETECTED", "PROBABLE") if args.all else ("DETECTED",)
    with Store(args.db) as store:
        rows = store.stack_of(args.target_id, verdicts)
        scans = store.scans_for(args.target_id, limit=5)

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    if not scans:
        print(f"no scans stored for {args.target_id!r}", file=sys.stderr)
        return 1

    if not any(s["status"] in ev.TRUSTWORTHY_STATUSES for s in scans):
        # Not the same thing as "runs nothing".
        print(f"{args.target_id} — never scanned successfully "
              f"(last: {scans[0]['status']}); no stack can be reported")
        for s in scans:
            print(f"    {s['started_at'][:19]}  {s['status']:12} {s['url']}")
        return 1

    print(f"{args.target_id} — {len(rows)} vendors")
    print("  recent scans:")
    for s in scans:
        print(f"    {s['started_at'][:19]}  {s['status']:12} {s['url']}")
    current = None
    for row in rows:
        if row["category"] != current:
            current = row["category"]
            print(f"\n  [{current}]")
        print(f"    {row['verdict']:9} {row['score']:4.1f}  {row['name']:36} "
              f"{row['direct_layers']}")
    return 0


def _cmd_probable(args: argparse.Namespace) -> int:
    from . import mining
    with Store(args.db) as store:
        rows = mining.probable_only(store)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("no vendor is stuck at PROBABLE — every current hit has two signals")
        return 0
    print(f"{'companies':>9}  {'fingerprint':22} {'category':12} fired layers / sample")
    for r in rows:
        print(f"{r['count']:>9}  {r['fingerprint_id']:22} {r['category']:12} "
              f"{','.join(r['layers']) or '-'}  e.g. {', '.join(r['targets'][:4])}")
    print(f"\n{len(rows)} fingerprint(s) — add a second signal "
          f"(try patterns on /fingerprints against stored evidence first)")
    return 0


def _cmd_suggest_urls(args: argparse.Namespace) -> int:
    from . import mining
    with Store(args.db) as store:
        rows = mining.suggest_urls(store, max_per_target=args.max_per_target)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("no candidates — either every target already watches several "
              "URLs, or the evidence predates anchor collection (rescan first)")
        return 0
    for r in rows:
        for url in r["urls"]:
            print(f"  {r['target_id']:24} {url}")
    print(f"\n{len(rows)} target(s) — add the useful ones to targets.yaml "
          f"under that target's urls:")
    return 0


def _cmd_hygiene(args: argparse.Namespace) -> int:
    from . import hygiene
    with Store(args.db) as store:
        rows = hygiene.report(store, streak=args.streak)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print(f"no target has {args.streak}+ consecutive failed attempts — watchlist looks healthy")
        return 0
    print(f"{'streak':>6}  {'status':13} {'target':22} {'tier':4} last good        url / note")
    for r in rows:
        good = (r["last_good_at"] or "never")[:10]
        note = f"  [{r['note']}]" if r["note"] else ""
        print(f"{r['streak']:>6}  {r['dominant']:13} {r['target_id']:22} "
              f"{r['tier'] or '-':4} {good:16} {r['url']}{note}")
    print()
    print(f"{len(rows)} URL(s) flagged - fix the domain, disable the target, "
          "or record why it stays")
    return 0


def _cmd_notify(args: argparse.Namespace) -> int:
    from . import notify
    with Store(args.db) as store:
        report = notify.notify_new_signals(
            store, language=args.lang, webhook=args.webhook,
            dry_run=args.dry_run, progress=_say)
    if report.get("error"):
        print(f"notify failed: {report['error']}", file=sys.stderr)
        return 1
    if args.dry_run:
        print(f"{report['pending']} signal(s) pending — nothing sent (dry run)")
    else:
        print(f"sent {report['sent']} of {report['pending']} pending signal(s)")
    return 0


def _cmd_unknown_hosts(args: argparse.Namespace) -> int:
    from . import mining
    try:
        registry = _load_registry(args.fingerprints)
    except reg.RegistryError as exc:
        print(f"fingerprint registry unusable: {exc}", file=sys.stderr)
        return 2
    with Store(args.db) as store:
        rows = mining.unknown_hosts(store, registry,
                                    min_targets=args.min_targets,
                                    include_noise=args.include_noise,
                                    include_exchanges=args.include_exchanges)
    rows = rows[:args.limit]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("no unknown third-party hosts above the threshold — "
              "the registry covers what the watchlist loads")
        return 0
    print(f"{'companies':>9}  {'domain':32}  example hosts (declared-only)")
    for r in rows:
        extra = f"  (+{r['declared_only_targets']} declared-only)" if r["declared_only_targets"] else ""
        print(f"{r['targets']:>9}  {r['domain']:32}  {', '.join(r['hosts'])}{extra}")
        print(f"{'':>9}  {'':32}  e.g. {', '.join(r['sample_targets'])}")
    return 0


def _cmd_runs(args: argparse.Namespace) -> int:
    with Store(args.db) as store:
        rows = [dict(r) for r in store.runs()]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("no runs stored yet")
        return 0
    print(f"{'run':>4}  {'started':19}  {'scans':>5}  {'targets':>7}  {'fps':>4}  note")
    for r in rows:
        # An unfinished run means the batch died partway — worth seeing at a
        # glance, because a cron that dies halfway looks fine otherwise.
        mark = "" if r["finished_at"] else "  ⚠ 중단됨(미완료)"
        print(f"{r['id']:>4}  {(r['started_at'] or '')[:19]:19}  {r['scan_count']:>5}  "
              f"{r['target_count'] or 0:>7}  {r['fingerprint_count'] or '-':>4}  "
              f"{r['note'] or ''}{mark}")
    return 0


def _cmd_signals(args: argparse.Namespace) -> int:
    with Store(args.db) as store:
        found = sig.all_signals(store, args.kind, language=args.lang)
        if args.industry:
            found = [s for s in found if s.industry in set(args.industry)]
        coverage = store.coverage()
        quiet = sig.quiet_greenfield(store, args.lang)

    if args.json:
        print(json.dumps([s.as_dict() for s in found], ensure_ascii=False, indent=2))
        return 0

    unjudged = [c for c in coverage.values() if not c["judged"]]
    if not found:
        print("no signals")
    current = None
    for signal in found:
        if signal.kind != current:
            current = signal.kind
            print(f"\n[{current}]  priority {signal.priority}")
        print(f"  {signal.company:14} {signal.industry:12} {signal.headline}")
        print(f"  {'':14} {'':12} {signal.detail}")
    if quiet:
        print(f"\n{len(quiet)} target(s) have no engagement platform but too little "
              f"MarTech to qualify (score < {sig.GREENFIELD_MIN_MATURITY}): "
              + ", ".join(f"{q['company']}({q['maturity']['score']})" for q in quiet))
    if unjudged:
        # Never let these be mistaken for companies that run nothing.
        print(f"\n{len(unjudged)} target(s) produced no signal because they were "
              f"never scanned successfully: "
              + ", ".join(sorted(c["target_id"] for c in unjudged)))
    return 0


def _cmd_changes(args: argparse.Namespace) -> int:
    with Store(args.db) as store:
        observations, _ = sig.timeline(store, args.target_id)
        changes = sig.changes_for(store, args.target_id)

    if args.json:
        print(json.dumps({
            "timeline": [
                {"run_id": o.run_id, "scanned_at": o.scanned_at, "judged": o.judged,
                 "urls_ok": o.urls_ok, "urls_scanned": o.urls_scanned,
                 "vendors": sorted(o.vendors)}
                for o in observations
            ],
            "changes": [c.as_dict() for c in changes],
        }, ensure_ascii=False, indent=2))
        return 0

    if not observations:
        print(f"no scans stored for {args.target_id!r}", file=sys.stderr)
        return 1

    print(f"{args.target_id} — {len(observations)} runs "
          f"({sum(1 for o in observations if o.judged)} judged)")
    for o in observations:
        mark = "  " if o.judged else "! "
        state = f"{len(o.vendors)} vendors" if o.judged else "not judged"
        print(f"  {mark}run {o.run_id:<4} {o.scanned_at[:19]}  "
              f"{o.urls_ok}/{o.urls_scanned} urls ok  {state}")

    moved = [c for c in changes if c.state not in (sig.STATE_STABLE, sig.STATE_BASELINE)]
    shown = changes if args.all else moved
    if not shown:
        print("\n  nothing changed")
        return 0
    print()
    for c in sorted(shown, key=lambda c: (c.category, c.name)):
        print(f"  {c.state:19} {c.name:34} [{c.category}] "
              f"since run {c.since_run} ({c.run_streak} of {c.judged_runs} judged runs)")
    return 0


def _cmd_brief(args: argparse.Namespace) -> int:
    with Store(args.db) as store:
        text, usable = brf.company_brief(store, args.target_id, language=args.lang)
    print(text)
    return 0 if usable else 1


def _cmd_digest(args: argparse.Namespace) -> int:
    with Store(args.db) as store:
        text = brf.weekly_digest(store, args.lang)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
        print(f"\n  written to {args.out}", file=sys.stderr)
    return 0


def _cmd_worker(args: argparse.Namespace) -> int:
    try:
        registry = _load_registry(args.fingerprints)
    except reg.RegistryError as exc:
        print(f"cannot start: {exc}", file=sys.stderr)
        return 2
    config = ScanConfig(settle_ms=args.settle_ms)
    with Store(args.db) as store:
        name = args.name or wrk.default_worker_name()
        _say(f"worker {name} — {'waiting for work' if args.watch else 'draining the queue'}")
        report = asyncio.run(wrk.drain_queue(
            store, registry, scan_config=config, worker=name, run_id=args.run,
            concurrency=args.concurrency, progress=_say, idle_exit=not args.watch))
        closed = wrk.finish_runs(store, registry)
        _say(report.summary() + (f", closed run(s) {closed}" if closed else ""))
    return 0 if report.failed == 0 else 1


def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print("the dashboard needs fastapi, uvicorn and jinja2:\n"
              "  .venv/bin/pip install -r requirements.txt", file=sys.stderr)
        return 2
    from web.app import create_app

    try:
        app = create_app(args.db, args.targets_file, args.fingerprints, args.lang)
    except (tgt.TargetError, reg.RegistryError) as exc:
        print(f"cannot start: {exc}", file=sys.stderr)
        return 2

    with Store(args.db) as store:
        if not store.targets():
            print(f"note: the watchlist is empty — workspace is {workspace.describe()}.\n"
                  "      Add companies on the Manage page, or "
                  "`radar watchlist import targets.yaml`.", file=sys.stderr)

    settings = cfg.load()
    if args.host not in ("127.0.0.1", "localhost", "::1") and not settings.server.requires_login:
        # The dashboard can edit the watchlist and queue scans. On a public
        # address with no token, so can anyone who finds the port.
        print(f"refusing to bind {args.host} without an access token.\n"
              f"  set one in radar.toml:\n\n"
              f"    [server]\n    token = \"{security.suggest_token()}\"\n",
              file=sys.stderr)
        return 2
    _say(f"dashboard on http://{args.host}:{args.port}"
         + ("" if settings.server.requires_login else "  (no token — local only)"))
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def _cmd_watchlist(args: argparse.Namespace) -> int:
    if args.watchlist_command == "import":
        try:
            watchlist = tgt.load(args.path)
        except tgt.TargetError as exc:
            print(f"cannot import: {exc}", file=sys.stderr)
            return 2
        with Store(args.db) as store:
            stats = store.import_watchlist(watchlist, replace=args.replace)
        print(f"imported {stats['added']} new, {stats['updated']} updated, "
              f"{stats['industries']} industries"
              + (f", {stats['disabled']} disabled" if stats["disabled"] else ""))
        return 0

    if args.watchlist_command == "export":
        with Store(args.db) as store:
            text = tgt.to_yaml(store.industries(), store.targets())
        if args.out:
            with open(args.out, "w", encoding="utf-8") as handle:
                handle.write(text)
            print(f"written to {args.out}", file=sys.stderr)
        else:
            print(text, end="")
        return 0

    with Store(args.db) as store:
        rows = [dict(r) for r in store.targets()]
        industries = {r["code"]: r["label"] for r in store.industries()}
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("the watchlist is empty — `radar watchlist import targets.yaml`")
        return 0
    disabled = sum(1 for r in rows if not r["enabled"])
    print(f"{len(rows)} companies, {len(industries)} industries"
          + (f", {disabled} paused" if disabled else ""))
    for row in rows:
        urls = json.loads(row["urls_json"])
        flag = "" if row["enabled"] else "  (paused)"
        extra = f"  +{len(urls) - 1} url" if len(urls) > 1 else ""
        print(f"  {row['id']:16} {row['company']:20} "
              f"{industries.get(row['industry'], row['industry']):14} "
              f"{urls[0]}{extra}{flag}")
    return 0


def _cmd_backup(args: argparse.Namespace) -> int:
    with Store(args.db) as store:
        result = tsk.backup_snapshot(
            store, Path(args.dir) if args.dir else None, keep=args.keep)
    _say(f"backup written: {result['path']} ({result['size_mb']:.1f} MB) — "
         f"{result['kept']} kept, {result['pruned']} pruned")
    return 0


def _cmd_task(args: argparse.Namespace) -> int:
    try:
        registry = _load_registry(args.fingerprints)
    except reg.RegistryError as exc:
        print(f"fingerprint registry unusable: {exc}", file=sys.stderr)
        return 2
    with Store(args.db) as store:
        try:
            output = tsk.run(args.kind, store, registry,
                             {"dry_run": args.dry_run}, say=_say)
        except Exception as exc:  # noqa: BLE001 — report, do not trace
            print(f"task {args.kind} failed: {exc}", file=sys.stderr)
            return 1
    print(output)
    return 0


def _cmd_home(args: argparse.Namespace) -> int:
    root = workspace.home()
    print(workspace.describe())
    for name in ("radar.toml", "targets.yaml", "data/radar.db", "fingerprints"):
        mark = "✓" if (root / name).exists() else "·"
        note = "" if (root / name).exists() or name != "fingerprints" else "  (using the shipped set)"
        print(f"  {mark} {name}{note}")
    return 0


_COMMANDS = {
    "home": _cmd_home,
    "backup": _cmd_backup,
    "task": _cmd_task,
    "scan": _cmd_scan,
    "watchlist": _cmd_watchlist,
    "detect": _cmd_detect,
    "fingerprints": _cmd_fingerprints,
    "targets": _cmd_targets,
    "batch": _cmd_batch,
    "redetect": _cmd_redetect,
    "stack": _cmd_stack,
    "runs": _cmd_runs,
    "signals": _cmd_signals,
    "changes": _cmd_changes,
    "serve": _cmd_serve,
    "worker": _cmd_worker,
    "brief": _cmd_brief,
    "unknown-hosts": _cmd_unknown_hosts,
    "notify": _cmd_notify,
    "hygiene": _cmd_hygiene,
    "suggest-urls": _cmd_suggest_urls,
    "probable": _cmd_probable,
    "digest": _cmd_digest,
}


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    # The workspace must be pinned before any default_path() is consulted.
    workspace.set_home(args.home)
    try:
        return _COMMANDS[args.command](args)
    except StoreError as exc:
        # Cron reads this. A sentence beats a traceback.
        print(f"database unusable: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
