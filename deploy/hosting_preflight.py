"""Read-only cutover checks; never activates collection or changes services."""

import argparse
import json
from pathlib import Path
import shutil
from urllib.parse import urlsplit

from radar import config
from radar.recovery import inspect_database


def check(home, backup_directory, public_host=None):
    home, backup_directory = Path(home).resolve(), Path(backup_directory).resolve()
    findings = []
    marker = home / ".radar-hosting"
    if not marker.is_file() or marker.read_text().strip() != "hosting-v1":
        findings.append("hosting_workspace_marker_missing")
    settings = config.load(home / "radar.toml")
    if len(settings.server.token.strip()) < 32:
        findings.append("strong_login_token_required")
    if not settings.server.public_origin:
        findings.append("https_public_origin_required")
    if public_host is not None and urlsplit(settings.server.public_origin).netloc != public_host:
        findings.append('public_host_differs_from_proxy')
    if settings.server.allow_private_targets:
        findings.append("private_scan_targets_must_be_disabled")
    if not settings.scan.chromium_sandbox:
        findings.append("chromium_sandbox_required")
    if settings.scan.timezone != "Asia/Seoul":
        findings.append("timezone_differs_from_packaged_schedule")
    if settings.cadence.default != "1d" or settings.cadence.tiers or settings.cadence.daily_limit:
        findings.append("daily_all_company_schedule_not_configured")
    if (settings.preparation.enabled and not settings.preparation.gateway_url.startswith("https://")
            and settings.preparation.gateway_url != "http://preparation:8612"):
        findings.append("https_preparation_gateway_required")
    if backup_directory == home or backup_directory.is_relative_to(home):
        findings.append("backup_directory_must_be_outside_workspace")
    if not backup_directory.is_dir():
        findings.append("backup_directory_missing")
    state = inspect_database(home / "data/radar.db")
    if any(state[k] for k in ("active_collection_jobs", "active_tasks", "active_generation_jobs")):
        findings.append("unfinished_work_in_cutover_snapshot")
    if not state["counts"]["target"]:
        findings.append("empty_watchlist")
    free = shutil.disk_usage(home).free
    if free < max(2 * 1024**3, 3 * (home / "data/radar.db").stat().st_size):
        findings.append("insufficient_database_disk_headroom")
    return {
        "ready": not findings,
        "findings": findings,
        "database": state,
        "free_bytes": free,
        "collection_activated": (home / ".collection-enabled").is_file(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--backups", type=Path, required=True)
    parser.add_argument('--public-host', required=True)
    args = parser.parse_args()
    try:
        result = check(args.home, args.backups, args.public_host)
    except Exception as exc:
        result = {"ready": False, "error": type(exc).__name__}
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["ready"] else 2)


if __name__ == "__main__":
    main()
