"""Verified online backup and restore into a NEW, collection-disabled workspace."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import UTC, datetime
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import sqlite3
import tempfile
import uuid

FORMAT = "radar-recovery-v1"
DEFAULT_KEEP = 3
DEFAULT_MAX_BYTES = 80 * 1024**3
DEFAULT_MIN_FREE_BYTES = 2 * 1024**3
TABLES = ("target", "scan", "detection", "job", "preparation_draft", "preparation_job")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inspect_database(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        db.execute("BEGIN")
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("Database integrity check failed")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Database foreign key check failed")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"target", "scan", "job", "meta"} <= tables:
            raise ValueError("Not a radar database")
        counts = {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in TABLES
            if table in tables
        }
        active = db.execute(
            "SELECT COUNT(*) FROM job WHERE state IN ('pending','claimed')"
        ).fetchone()[0]
        tasks = (
            db.execute("SELECT COUNT(*) FROM task WHERE state IN ('pending','running')").fetchone()[
                0
            ]
            if "task" in tables
            else 0
        )
        generation = (
            db.execute("SELECT COUNT(*) FROM preparation_job WHERE state='running'").fetchone()[0]
            if "preparation_job" in tables
            else 0
        )
        return {
            "counts": counts,
            "active_collection_jobs": active,
            "active_tasks": tasks,
            "active_generation_jobs": generation,
        }


def permitted(name):
    path = Path(name)
    return (
        (
            name in ("radar.toml", "data/radar.db")
            or (
                len(path.parts) == 2
                and path.parts[0] == "fingerprints"
                and path.suffix == ".yaml"
                and path.name not in (".", "..")
            )
        )
        and not path.is_absolute()
        and ".." not in path.parts
    )


def verify(folder):
    folder = Path(folder)
    manifest = json.loads((folder / "manifest.json").read_text())
    if manifest.get("format") != FORMAT or not isinstance(manifest.get("files"), dict):
        raise ValueError("Unknown backup format")
    if not {"radar.toml", "data/radar.db"} <= manifest["files"].keys():
        raise ValueError("Backup is incomplete")
    for name, expected in manifest["files"].items():
        path = folder / name
        if (
            not permitted(name)
            or not path.resolve().is_relative_to(folder.resolve())
            or path.is_symlink()
        ):
            raise ValueError("Unsafe backup member")
        if digest(path) != expected:
            raise ValueError("Backup checksum mismatch")
    state = inspect_database(folder / "data/radar.db")
    if state != manifest["database"]:
        raise ValueError("Backup database manifest mismatch")
    return manifest


def backup(home, destination, keep=DEFAULT_KEEP, *, max_bytes=DEFAULT_MAX_BYTES,
           min_free_bytes=DEFAULT_MIN_FREE_BYTES, database=None):
    home, destination = Path(home).resolve(), Path(destination).resolve()
    database = Path(database).resolve() if database else home / 'data/radar.db'
    config_path = home / 'radar.toml'
    if keep < 1:
        raise ValueError("keep must be positive")
    if max_bytes < 0 or min_free_bytes < 0:
        raise ValueError('backup byte limits must be non-negative')
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (destination / ".backup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # page_count includes committed WAL pages not yet in the main file.
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as sizing:
            db_size = sizing.execute('PRAGMA page_count').fetchone()[0] * sizing.execute('PRAGMA page_size').fetchone()[0]
        # Never delete the last recovery points just to make the next copy fit.
        minimum_copies = min(keep, 2)
        if max_bytes and db_size * minimum_copies > max_bytes:
            raise ValueError('Backup budget cannot retain two recovery points; expand storage or archive first')
        if shutil.disk_usage(destination).free < db_size + min_free_bytes:
            raise ValueError('Insufficient backup disk headroom; existing snapshots preserved')
        with tempfile.TemporaryDirectory(prefix=".radar-part-", dir=destination) as temporary:
            stage = Path(temporary)
            (stage / "data").mkdir()
            # A local workspace may use defaults with no config file. Restoring
            # an empty TOML preserves those defaults; never copy another home's config.
            original = config_path.read_bytes() if config_path.exists() else b''
            (stage / "radar.toml").write_bytes(original)
            os.chmod(stage / "radar.toml", 0o600)
            source = sqlite3.connect(
                database.as_uri() + "?mode=ro", uri=True, timeout=2
            )
            try:
                with closing(sqlite3.connect(stage / "data/radar.db")) as out:
                    source.backup(out, pages=256, sleep=0.05)
            finally:
                source.close()
            members = ["radar.toml", "data/radar.db"]
            for path in sorted((home / "fingerprints").glob("*.yaml")):
                if path.is_symlink():
                    raise ValueError("Fingerprint symlinks are not supported in backups")
                dest = stage / "fingerprints" / path.name
                dest.parent.mkdir(exist_ok=True)
                shutil.copyfile(path, dest)
                members.append(str(dest.relative_to(stage)))
            if (config_path.read_bytes() if config_path.exists() else b'') != original:
                raise ValueError("Configuration changed during backup; retry later")
            manifest = {
                "format": FORMAT,
                "created_at": datetime.now(UTC).isoformat(),
                "files": {name: digest(stage / name) for name in members},
                "database": inspect_database(stage / "data/radar.db"),
            }
            (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
            verify(stage)
            final = destination / (
                "radar-"
                + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
                + "-"
                + uuid.uuid4().hex[:8]
            )
            stage.rename(final)
        # Only snapshots atomically published by this format are eligible.
        # Do not re-hash every historical DB on each backup (O(retention * DB size)).
        valid = []
        for old in destination.glob("radar-*"):
            if (
                old.is_dir()
                and not old.is_symlink()
                and re.fullmatch(r"radar-\d{8}T\d{12}-[0-9a-f]{8}", old.name)
            ):
                try:
                    info = json.loads((old / "manifest.json").read_text())
                    if (
                        info.get("format") == FORMAT
                        and {"radar.toml", "data/radar.db"} <= info.get("files", {}).keys()
                    ):
                        valid.append(old)
                except (OSError, ValueError, KeyError, sqlite3.Error):
                    continue
        ordered = sorted(valid, reverse=True)
        retained = ordered[:keep]
        sizes = {p: sum(f.stat().st_size for f in p.rglob('*') if f.is_file()) for p in retained}
        while max_bytes and sum(sizes[p] for p in retained) > max_bytes and len(retained) > minimum_copies:
            retained.pop()
        if max_bytes and sum(sizes[p] for p in retained) > max_bytes:
            raise ValueError('Verified backup published but retention budget exceeded; snapshots preserved')
        for old in ordered[len(retained):]:
            shutil.rmtree(old)
        return final


def configured_backup(home, destination=None, *, database=None, keep=None,
                      max_gib=None, min_free_gib=None):
    """One policy for CLI, queued maintenance and scheduled containers.

    Explicit arguments override deployment environment, then safe defaults.
    Old standalone .db snapshots are never pruned by this archive format.
    """
    home = Path(home).resolve()
    destination = destination or os.environ.get('RADAR_BACKUP_DIRECTORY') or home / 'backups'
    keep = int(keep if keep is not None else os.environ.get('RADAR_BACKUP_KEEP', DEFAULT_KEEP))
    max_gib = float(max_gib if max_gib is not None else os.environ.get('RADAR_BACKUP_MAX_GIB', 80))
    min_free_gib = float(min_free_gib if min_free_gib is not None else os.environ.get('RADAR_BACKUP_MIN_FREE_GIB', 2))
    return backup(home, destination, keep, database=database,
                  max_bytes=int(max_gib * 1024**3), min_free_bytes=int(min_free_gib * 1024**3))


def restore(folder, destination, mode="staging"):
    manifest = verify(folder)
    destination = Path(destination).resolve()
    if mode not in ("staging", "hosting"):
        raise ValueError("Unknown deployment mode")
    destination.mkdir(mode=0o700)  # never overwrite an existing workspace
    for name in manifest["files"]:
        dest = destination / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(folder) / name, dest)
        os.chmod(dest, 0o600)
        if digest(dest) != manifest["files"][name]:
            raise ValueError("Restored member checksum mismatch")
    (destination / f".radar-{mode}").write_text(
        "isolated-staging\n" if mode == "staging" else "hosting-v1\n"
    )
    # A restored backup never carries collection activation or runtime locks.
    return inspect_database(destination / "data/radar.db")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("backup")
    make.add_argument("--home", type=Path, required=True)
    make.add_argument("--destination", type=Path)
    make.add_argument("--keep", type=int, help='deployment setting or 3')
    make.add_argument('--max-gib', type=float, help='deployment setting or 80; 0 disables')
    make.add_argument('--min-free-gib', type=float, help='deployment setting or 2')
    check = commands.add_parser("verify")
    check.add_argument("snapshot", type=Path)
    recover = commands.add_parser("restore")
    recover.add_argument("snapshot", type=Path)
    recover.add_argument("destination", type=Path)
    recover.add_argument("--mode", choices=("staging", "hosting"), default="staging")
    args = parser.parse_args()
    if args.command == "backup":
        print(configured_backup(args.home, args.destination, keep=args.keep,
                                max_gib=args.max_gib, min_free_gib=args.min_free_gib))
    elif args.command == "verify":
        print(json.dumps(verify(args.snapshot)["database"]))
    else:
        print(json.dumps(restore(args.snapshot, args.destination, args.mode)))


if __name__ == "__main__":
    main()
