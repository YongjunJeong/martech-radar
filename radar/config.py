"""Deployment settings.

Everything a different team in a different market would need to change lives
here or in the two YAML data files — never in code. A Turkish or Brazilian
deployment should be a fork of `targets.yaml` and a four-line `radar.toml`,
not a patch.

The file is optional. Without it the defaults are neutral — English UI,
UTC, a US locale — and a deployment says what it is by overriding them.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field

from . import workspace
from pathlib import Path
from typing import Any

CONFIG_ENV = "RADAR_CONFIG"
CONFIG_NAME = "radar.toml"


class ConfigError(Exception):
    """Raised for a malformed radar.toml."""


@dataclass(frozen=True)
class ScanSettings:
    #: Sites serve different tags to different markets. A page fetched with a
    #: Turkish locale is the page a Turkish visitor sees, which is the one a
    #: Turkish rep needs to know about.
    locale: str = "en-US"
    timezone: str = "UTC"
    concurrency: int = 3

    #: Read each site's robots.txt and skip what it disallows. Turning this
    #: off is a decision an operator has to make deliberately, and it is
    #: recorded in every scan result either way.
    respect_robots: bool = True

    #: The token robots.txt rules are matched against. "*" reads the rules
    #: everyone gets, which is the honest reading for a tool presenting a
    #: normal browser user agent.
    robots_user_agent: str = "*"

    #: Minimum gap between requests to the same site, on top of any
    #: Crawl-delay the site asks for.
    per_site_delay_ms: int = 1000


@dataclass(frozen=True)
class VendorSettings:
    """Which fingerprint, if any, is "ours".

    Set it and the tool separates "we lost this account" from "a competitor
    lost it" — the two most urgent signals it can produce, and they are not
    the same urgency. Leave it empty and every vendor is treated alike,
    which is the right setting for anyone who is not selling one of them.
    """
    home: str = ""

    @property
    def has_home(self) -> bool:
        return bool(self.home.strip())


@dataclass(frozen=True)
class ServerSettings:
    """Everything that changes when the dashboard is not on localhost.

    Empty token means no login, which is right for `127.0.0.1` and wrong for
    anything else — `radar serve` says so when it binds to a public address.
    """
    token: str = ""
    #: Reject scan targets that resolve to private or loopback addresses.
    #: On a shared server the URL box is otherwise a way in to the network.
    allow_private_targets: bool = False

    @property
    def requires_login(self) -> bool:
        return bool(self.token.strip())


@dataclass(frozen=True)
class UISettings:
    language: str = "en"


def parse_duration(raw: str, where: str = "cadence") -> float:
    """ "12h" / "1d" / "7d" -> seconds. Days and hours only, on purpose:
    a scan cadence finer than hours is a monitoring tool, not this one."""
    text = str(raw).strip().lower()
    unit = text[-1:]
    if unit not in ("d", "h") or not text[:-1].isdigit() or int(text[:-1]) <= 0:
        raise ConfigError(f"[{where}]: bad duration {raw!r} (use e.g. \"12h\", \"7d\")")
    return int(text[:-1]) * (86400 if unit == "d" else 3600)


#: The signal kinds a webhook hears about by default: everything that is a
#: change. GREENFIELD is a standing state, not an event — it belongs in the
#: dashboard and the digest, not in a ping.
DEFAULT_NOTIFY_KINDS = (
    "HOME_MIGRATION", "HOME_REMOVAL", "COMPETITOR_MIGRATION",
    "COMPETITOR_REMOVAL", "COMPETITOR_NEW", "HOME_NEW",
)


@dataclass(frozen=True)
class NotifySettings:
    """Where and what `radar notify` (and the end of a batch) sends."""
    webhook: str = ""
    #: "slack" posts {"text": ...} (Slack incoming-webhook shape, readable
    #: by most chat tools); "json" posts the structured signal list.
    format: str = "slack"
    kinds: tuple[str, ...] = DEFAULT_NOTIFY_KINDS
    max_items: int = 20


@dataclass(frozen=True)
class CadenceSettings:
    """How often each tier is due for a scan.

    Tier names are whatever the watchlist uses (P1/P2/…); `default` covers
    untiered targets and tiers not listed here. Only `batch --due` reads
    this — an explicit batch still scans whatever it is told to.
    """
    default: str = "7d"
    tiers: dict[str, str] = field(default_factory=dict)

    def seconds_for(self, tier: str | None) -> float:
        raw = self.tiers.get(tier or "", self.default)
        return parse_duration(raw)


@dataclass(frozen=True)
class Config:
    scan: ScanSettings = field(default_factory=ScanSettings)
    vendor: VendorSettings = field(default_factory=VendorSettings)
    server: ServerSettings = field(default_factory=ServerSettings)
    ui: UISettings = field(default_factory=UISettings)
    cadence: CadenceSettings = field(default_factory=CadenceSettings)
    notify: NotifySettings = field(default_factory=NotifySettings)
    source: str | None = None


def default_path() -> Path:
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    return workspace.path(CONFIG_NAME)


def _section(document: dict[str, Any], name: str) -> dict[str, Any]:
    value = document.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    return value


def _build(cls, values: dict[str, Any], name: str):
    known = {f for f in cls.__dataclass_fields__}
    unknown = set(values) - known
    if unknown:
        raise ConfigError(f"[{name}]: unknown keys {sorted(unknown)} (known: {sorted(known)})")
    return cls(**values)


#: (resolved path, mtime_ns) -> Config. The dashboard's signal sweep asks
#: for settings once per company; without this that is one TOML parse per
#: company per request. The mtime key keeps edits visible immediately.
_cache: dict[tuple[str, int], Config] = {}


def _read_cadence(values: dict[str, Any]) -> CadenceSettings:
    """[cadence] maps tier names to durations, so its keys are free-form."""
    default = values.pop("default", "7d")
    parse_duration(default, "cadence.default")
    for tier, raw in values.items():
        parse_duration(raw, f"cadence.{tier}")
    return CadenceSettings(default=str(default),
                           tiers={k: str(v) for k, v in values.items()})


def _read_notify(values: dict[str, Any]) -> NotifySettings:
    if "kinds" in values:
        values = dict(values, kinds=tuple(values["kinds"]))
    built = _build(NotifySettings, values, "notify")
    if built.format not in ("slack", "json"):
        raise ConfigError(f'[notify]: format must be "slack" or "json", got {built.format!r}')
    return built


def load(path: Path | str | None = None) -> Config:
    config_path = Path(path).expanduser() if path else default_path()
    if not config_path.is_file():
        return Config()
    stat = config_path.stat()
    key = (str(config_path), stat.st_mtime_ns, stat.st_size)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    try:
        with open(config_path, "rb") as handle:
            document = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{config_path.name}: invalid TOML: {exc}") from exc

    unknown = set(document) - {"scan", "vendor", "server", "ui", "cadence", "notify"}
    if unknown:
        raise ConfigError(f"{config_path.name}: unknown sections {sorted(unknown)}")

    config = Config(
        scan=_build(ScanSettings, _section(document, "scan"), "scan"),
        vendor=_build(VendorSettings, _section(document, "vendor"), "vendor"),
        server=_build(ServerSettings, _section(document, "server"), "server"),
        ui=_build(UISettings, _section(document, "ui"), "ui"),
        cadence=_read_cadence(_section(document, "cadence")),
        notify=_read_notify(_section(document, "notify")),
        source=str(config_path),
    )
    _cache.clear()          # one live config; no reason to hold stale ones
    _cache[key] = config
    return config
