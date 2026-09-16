"""The watchlist.

One YAML file, edited by hand, is the whole target-management story. It is
also the only place industry labels are defined: a target may not use an
industry that is not declared at the top of the file, because a typo'd
"retail" / "Retail" / "리테일" would quietly split the industry view in the
dashboard into three half-empty columns.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Iterator

import yaml

from . import workspace
from .registry import RegistryError, StrictLoader

#: Target ids are keys in the history, so they stay boring on purpose.
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_]*$")

_ALLOWED_TARGET_KEYS = {
    "id", "company", "company_en", "industry", "tier", "urls", "note", "enabled",
    "robots_policy", "robots_note",
}

class TargetError(Exception):
    """Raised for a malformed targets file."""

@dataclass(frozen=True)
class Industry:
    """A vertical and its labels.

    Labels live beside the watchlist rather than in code so a new market
    ships a new `targets.yaml` and touches nothing else.
    """
    code: str
    label: str
    label_en: str = ""

    def display(self, language: str = "ko") -> str:
        if language != "ko" and self.label_en:
            return self.label_en
        return self.label

@dataclass(frozen=True)
class Target:
    id: str
    company: str
    industry: str
    urls: tuple[str, ...]
    company_en: str | None = None
    tier: str | None = None
    note: str | None = None
    enabled: bool = True
    #: "respect" (default) or "override". Overriding is a per-company
    #: decision that has to carry a reason — see `robots_note`.
    robots_policy: str = "respect"
    robots_note: str | None = None

    @property
    def primary_url(self) -> str:
        return self.urls[0]

@dataclass(frozen=True)
class TargetSet:
    targets: tuple[Target, ...]
    industries: dict[str, Industry]
    source: str

    def __iter__(self) -> Iterator[Target]:
        return iter(self.targets)

    def __len__(self) -> int:
        return len(self.targets)

    def enabled(self) -> tuple[Target, ...]:
        return tuple(t for t in self.targets if t.enabled)

    def get(self, target_id: str) -> Target | None:
        for t in self.targets:
            if t.id == target_id:
                return t
        return None

    def industry(self, code: str) -> Industry:
        """Look up a vertical. Accepts the bare-label shape too.

        A `TargetSet` built by hand (tests, callers) may pass
        `{"fashion": "패션"}`; treating that as a label rather than
        rejecting it keeps the simple case simple.
        """
        value = self.industries.get(code)
        if value is None:
            return Industry(code=code, label=code)
        if isinstance(value, str):
            return Industry(code=code, label=value)
        return value

    def industry_label(self, code: str, language: str = "ko") -> str:
        return self.industry(code).display(language)

    def labels(self, language: str = "ko") -> dict[str, str]:
        """Plain code -> label mapping, for templates and reports."""
        return {code: self.industry(code).display(language) for code in self.industries}

    def by_industry(self) -> dict[str, tuple[Target, ...]]:
        grouped: dict[str, list[Target]] = {}
        for t in self.targets:
            grouped.setdefault(t.industry, []).append(t)
        return {k: tuple(v) for k, v in sorted(grouped.items())}

def _check_url(url: object, where: str) -> str:
    """http(s) URLs, optionally behind the mobile: profile prefix."""
    if not isinstance(url, str):
        raise TargetError(f"{where}: url must be a string, got {url!r}")
    bare = url[len("mobile:"):] if url.startswith("mobile:") else url
    if not bare.startswith(("http://", "https://")):
        raise TargetError(f"{where}: url must start with http:// or https:// "
                          f"(optionally prefixed mobile:), got {url!r}")
    if " " in url.strip():
        raise TargetError(f"{where}: url contains whitespace: {url!r}")
    return url.strip()

def _read_target(entry: object, industries: dict[str, str], index: int) -> Target:
    where = f"target #{index}"
    if not isinstance(entry, dict):
        raise TargetError(f"{where}: each target must be a mapping")

    target_id = entry.get("id")
    if not isinstance(target_id, str) or not ID_PATTERN.match(target_id):
        raise TargetError(f"{where}: id must be lowercase snake_case, got {target_id!r}")
    where = f"target {target_id!r}"

    unknown = set(entry) - _ALLOWED_TARGET_KEYS
    if unknown:
        raise TargetError(f"{where}: unknown keys {sorted(unknown)}")

    company = entry.get("company")
    if not isinstance(company, str) or not company.strip():
        raise TargetError(f"{where}: company is required")

    industry = entry.get("industry")
    if industry not in industries:
        raise TargetError(
            f"{where}: industry {industry!r} is not declared at the top of the file "
            f"(declared: {', '.join(sorted(industries))})"
        )

    raw_urls = entry.get("urls")
    if isinstance(raw_urls, str):
        raw_urls = [raw_urls]
    if not isinstance(raw_urls, list) or not raw_urls:
        raise TargetError(f"{where}: urls must be a non-empty list")
    urls = tuple(_check_url(u, where) for u in raw_urls)
    if len(set(urls)) != len(urls):
        raise TargetError(f"{where}: duplicate urls")

    enabled = entry.get("enabled", True)
    if not isinstance(enabled, bool):
        raise TargetError(f"{where}: enabled must be true or false")

    policy = entry.get("robots_policy", "respect")
    if policy not in ("respect", "override"):
        raise TargetError(f"{where}: robots_policy must be respect or override")
    robots_note = entry.get("robots_note")
    if policy == "override" and not str(robots_note or "").strip():
        raise TargetError(
            f"{where}: robots_policy: override needs a robots_note explaining why")

    return Target(
        id=target_id,
        company=company.strip(),
        company_en=entry.get("company_en"),
        industry=industry,
        tier=entry.get("tier"),
        urls=urls,
        note=entry.get("note"),
        enabled=enabled,
        robots_policy=policy,
        robots_note=robots_note,
    )

def default_path() -> Path:
    return workspace.path("targets.yaml")

def load(path: Path | str | None = None) -> TargetSet:
    target_path = Path(path) if path else default_path()
    if not target_path.is_file():
        raise TargetError(f"targets file not found: {target_path}")

    try:
        document: Any = yaml.load(target_path.read_text(encoding="utf-8"), StrictLoader)
    except RegistryError as exc:
        raise TargetError(f"{target_path.name}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise TargetError(f"{target_path.name}: invalid YAML: {exc}") from exc

    if not isinstance(document, dict):
        raise TargetError(f"{target_path.name}: expected a mapping at the top level")

    raw_industries = document.get("industries")
    if not isinstance(raw_industries, dict) or not raw_industries:
        raise TargetError(f"{target_path.name}: an 'industries' mapping is required")
    industries = {}
    for code, value in raw_industries.items():
        code = str(code)
        # Two accepted shapes: a bare label, or a table that also carries the
        # second label. The short form stays valid so a watchlist
        # that predates the lookup keeps loading.
        if isinstance(value, str):
            industries[code] = Industry(code=code, label=value)
            continue
        if not isinstance(value, dict):
            raise TargetError(
                f"{target_path.name}: industry {code!r} must be a label or a table")
        unknown = set(value) - {"label", "label_en"}
        if unknown:
            raise TargetError(f"{target_path.name}: industry {code!r}: unknown keys {sorted(unknown)}")
        label = value.get("label")
        if not isinstance(label, str) or not label.strip():
            raise TargetError(f"{target_path.name}: industry {code!r} needs a label")
        industries[code] = Industry(
            code=code, label=label.strip(),
            label_en=str(value.get("label_en", "")).strip(),
        )

    entries = document.get("targets")
    if not isinstance(entries, list) or not entries:
        raise TargetError(f"{target_path.name}: a non-empty 'targets' list is required")

    targets: list[Target] = []
    seen_ids: set[str] = set()
    seen_urls: dict[str, str] = {}
    for i, entry in enumerate(entries):
        target = _read_target(entry, industries, i)
        if target.id in seen_ids:
            raise TargetError(f"duplicate target id {target.id!r}")
        seen_ids.add(target.id)
        for url in target.urls:
            if url in seen_urls:
                raise TargetError(
                    f"url {url} is listed by both {seen_urls[url]!r} and {target.id!r}"
                )
            seen_urls[url] = target.id
        targets.append(target)

    targets.sort(key=lambda t: (t.industry, t.id))
    return TargetSet(
        targets=tuple(targets),
        industries=industries,
        source=str(target_path),
    )

def to_yaml(industries, targets) -> str:
    """Render a watchlist back out as YAML.

    The database is the working copy; this is how it becomes a file again —
    to commit, to hand to a colleague, or to move to another machine.
    """
    lines = ["# Exported watchlist. Import it with:",
             "#   radar watchlist import <file>", "", "industries:"]
    for row in industries:
        lines.append(f"  {row['code']}:")
        lines.append(f"    label: {_scalar(row['label'])}")
        if row["label_en"]:
            lines.append(f"    label_en: {_scalar(row['label_en'])}")
    lines += ["", "targets:"]
    for row in targets:
        lines.append(f"  - id: {row['id']}")
        lines.append(f"    company: {_scalar(row['company'])}")
        if row["company_en"]:
            lines.append(f"    company_en: {_scalar(row['company_en'])}")
        lines.append(f"    industry: {row['industry']}")
        if row["tier"]:
            lines.append(f"    tier: {_scalar(row['tier'])}")
        if row["note"]:
            lines.append(f"    note: {_scalar(row['note'])}")
        if not row["enabled"]:
            lines.append("    enabled: false")
        if row["robots_policy"] == "override":
            lines.append("    robots_policy: override")
            lines.append(f"    robots_note: {_scalar(row['robots_note'] or '')}")
        lines.append("    urls:")
        for url in json.loads(row["urls_json"]):
            lines.append(f"      - {url}")
    return "\n".join(lines) + "\n"

def _scalar(value: str) -> str:
    """Quote only when YAML would otherwise misread the value."""
    text = str(value)
    if not text or text.strip() != text or text[0] in "#&*!|>%@`{}[],\"'":
        return json.dumps(text, ensure_ascii=False)
    if text.lower() in {"true", "false", "null", "yes", "no", "on", "off", "~"}:
        return json.dumps(text, ensure_ascii=False)
    if ": " in text or text.endswith(":"):
        return json.dumps(text, ensure_ascii=False)
    return text
