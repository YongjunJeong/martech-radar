"""Vendor fingerprint registry.

A fingerprint is a set of *patterns* grouped by which evidence field they
apply to. The registry knows nothing about scan results — it only loads,
validates and exposes the patterns. Matching lives in `detector`.

Keeping the two apart is what lets us re-classify stored scans months
later: add a fingerprint here, re-run detection, and every historical scan
gains the new vendor without touching a browser.
"""

from __future__ import annotations

import hashlib
import re

from . import workspace
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Iterable, Iterator, Sequence

import yaml

# Evidence fields a pattern can be written against. Anything else in a YAML
# file is a typo, and a typo must be loud: a misspelled field would silently
# match nothing, and "nobody in Korea uses Braze" would look like a finding
# instead of a bug.
FIELDS = ("hosts", "scripts", "window", "storage", "tokens", "dom", "console")

STRENGTH_STRONG = "strong"
STRENGTH_WEAK = "weak"

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")
_REGEX_PREFIX = "re:"


class RegistryError(Exception):
    """Raised for any malformed fingerprint file."""


class StrictLoader(yaml.SafeLoader):
    """YAML loader that rejects duplicate mapping keys.

    Stock PyYAML keeps the last of two identical keys. A second
    `weak_signals:` in the same entry would therefore delete the first one
    silently, and a vendor would quietly stop matching. Fail instead.
    """


def _no_duplicate_keys(loader: StrictLoader, node: Any, deep: bool = False) -> dict:  # type: ignore[type-arg]
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise RegistryError(
                f"duplicate key {key!r} on line {key_node.start_mark.line + 1}"
            )
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys
)


@dataclass(frozen=True)
class Signal:
    field: str
    pattern: str
    strength: str
    regex: re.Pattern[str] | None = None

    @property
    def is_regex(self) -> bool:
        return self.regex is not None

    def describe(self) -> str:
        return f"{self.field}:{self.pattern}"


@dataclass(frozen=True)
class Fingerprint:
    id: str
    name: str
    category: str
    signals: tuple[Signal, ...]
    vendor_url: str | None = None
    note: str | None = None

    def signals_for(self, field: str) -> tuple[Signal, ...]:
        return tuple(s for s in self.signals if s.field == field)


@dataclass(frozen=True)
class Registry:
    fingerprints: tuple[Fingerprint, ...]
    source_files: tuple[str, ...]
    #: sha256 over the fingerprint files' bytes. `fingerprint_count` cannot
    #: tell "same 75 vendors, one pattern edited" from "nothing changed";
    #: this can, and every run records it.
    content_hash: str = ""

    def __iter__(self) -> Iterator[Fingerprint]:
        return iter(self.fingerprints)

    def __len__(self) -> int:
        return len(self.fingerprints)

    def get(self, fingerprint_id: str) -> Fingerprint | None:
        for fp in self.fingerprints:
            if fp.id == fingerprint_id:
                return fp
        return None

    def categories(self) -> tuple[str, ...]:
        return tuple(sorted({fp.category for fp in self.fingerprints}))

    def by_category(self, category: str) -> tuple[Fingerprint, ...]:
        return tuple(fp for fp in self.fingerprints if fp.category == category)


def _compile(field: str, raw: object, strength: str, where: str) -> Signal:
    if not isinstance(raw, str) or not raw.strip():
        raise RegistryError(f"{where}: {field} pattern must be a non-empty string, got {raw!r}")
    pattern = raw.strip()
    compiled: re.Pattern[str] | None = None
    if pattern.startswith(_REGEX_PREFIX):
        body = pattern[len(_REGEX_PREFIX):]
        if not body:
            raise RegistryError(f"{where}: empty regex in {field}")
        try:
            compiled = re.compile(body)
        except re.error as exc:
            raise RegistryError(f"{where}: bad regex {body!r} in {field}: {exc}") from exc
    return Signal(field=field, pattern=pattern, strength=strength, regex=compiled)


def _read_signal_block(block: object, strength: str, where: str) -> list[Signal]:
    if block is None:
        return []
    if not isinstance(block, dict):
        raise RegistryError(f"{where}: signal block must be a mapping of field -> list")
    signals: list[Signal] = []
    for field, patterns in block.items():
        if field not in FIELDS:
            raise RegistryError(
                f"{where}: unknown evidence field {field!r} (known: {', '.join(FIELDS)})"
            )
        if isinstance(patterns, str):
            patterns = [patterns]
        if not isinstance(patterns, list) or not patterns:
            raise RegistryError(f"{where}: {field} must be a non-empty list of patterns")
        for raw in patterns:
            signals.append(_compile(field, raw, strength, where))
    return signals


def _read_entry(entry: object, default_category: str | None, where: str) -> Fingerprint:
    if not isinstance(entry, dict):
        raise RegistryError(f"{where}: each fingerprint must be a mapping")
    fp_id = entry.get("id")
    if not isinstance(fp_id, str) or not _ID_RE.match(fp_id):
        raise RegistryError(f"{where}: id must be lowercase snake_case, got {fp_id!r}")
    where = f"{where} [{fp_id}]"
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise RegistryError(f"{where}: name is required")
    category = entry.get("category") or default_category
    if not isinstance(category, str) or not category.strip():
        raise RegistryError(f"{where}: category is required (set it on the entry or the file)")

    # Check for stray keys before anything else: a typo'd "sigmals:" should be
    # reported as a typo, not as the "has no signals" it also causes.
    unknown = set(entry) - {
        "id", "name", "category", "signals", "weak_signals", "vendor_url", "note",
    }
    if unknown:
        raise RegistryError(f"{where}: unknown keys {sorted(unknown)}")

    signals = _read_signal_block(entry.get("signals"), STRENGTH_STRONG, where)
    signals += _read_signal_block(entry.get("weak_signals"), STRENGTH_WEAK, where)
    if not signals:
        raise RegistryError(f"{where}: has no signals")

    seen: set[tuple[str, str]] = set()
    for sig in signals:
        key = (sig.field, sig.pattern)
        if key in seen:
            raise RegistryError(f"{where}: duplicate pattern {sig.describe()}")
        seen.add(key)

    return Fingerprint(
        id=fp_id,
        name=name.strip(),
        category=category.strip(),
        signals=tuple(signals),
        vendor_url=entry.get("vendor_url"),
        note=entry.get("note"),
    )


def load_file(path: Path) -> list[Fingerprint]:
    try:
        document = yaml.load(path.read_text(encoding="utf-8"), StrictLoader)
    except RegistryError as exc:
        raise RegistryError(f"{path.name}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise RegistryError(f"{path.name}: invalid YAML: {exc}") from exc
    if document is None:
        return []
    if isinstance(document, list):
        entries, default_category = document, None
    elif isinstance(document, dict):
        entries = document.get("fingerprints")
        default_category = document.get("category")
        if not isinstance(entries, list):
            raise RegistryError(f"{path.name}: expected a 'fingerprints' list")
    else:
        raise RegistryError(f"{path.name}: expected a list or a mapping at the top level")
    return [_read_entry(e, default_category, f"{path.name}#{i}") for i, e in enumerate(entries)]


def shipped_dir() -> Path:
    """The fingerprints that come with the package."""
    return workspace.package_root() / "fingerprints"


def default_dir() -> Path:
    """A workspace's own `fingerprints/` if it has one, else the shipped set.

    Override, not merge: a team that wants the shipped set plus one vendor
    copies the directory and adds to it. Merging two registries would make
    "which file is this rule in?" a question with two answers.
    """
    local = workspace.path("fingerprints")
    return local if local.is_dir() else shipped_dir()


def load(directory: Path | str | None = None) -> Registry:
    root = Path(directory) if directory else default_dir()
    if not root.is_dir():
        raise RegistryError(f"fingerprint directory not found: {root}")
    files = sorted(p for p in root.glob("*.yaml") if not p.name.startswith("_"))
    if not files:
        raise RegistryError(f"no fingerprint files in {root}")

    fingerprints: list[Fingerprint] = []
    origin: dict[str, str] = {}
    for path in files:
        for fp in load_file(path):
            if fp.id in origin:
                raise RegistryError(
                    f"duplicate fingerprint id {fp.id!r} in {path.name} "
                    f"(already defined in {origin[fp.id]})"
                )
            origin[fp.id] = path.name
            fingerprints.append(fp)

    fingerprints.sort(key=lambda fp: (fp.category, fp.id))
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return Registry(
        fingerprints=tuple(fingerprints),
        source_files=tuple(p.name for p in files),
        content_hash=digest.hexdigest()[:16],
    )


def signal_count(fingerprints: Iterable[Fingerprint]) -> int:
    return sum(len(fp.signals) for fp in fingerprints)


def summarise(registry: Registry) -> Sequence[tuple[str, int]]:
    counts: dict[str, int] = {}
    for fp in registry:
        counts[fp.category] = counts.get(fp.category, 0) + 1
    return sorted(counts.items())
