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
import shutil
import tomllib
from urllib.parse import urlsplit
from datetime import date, datetime, UTC
from zoneinfo import ZoneInfo
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
    #: Opt in on hosts/containers configured for Chromium user namespaces.
    #: Keep existing deployments unchanged; failure must not disable sandbox.
    chromium_sandbox: bool = False

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
    #: Exact hosts with known delayed tag initialisation. Other sites keep the
    #: ordinary observation window; this never changes verdict thresholds.
    minimum_observation_ms_by_host: dict[str, int] = field(default_factory=dict)
    #: Optional site-owned bootstrap checkpoint, independent of any vendor SDK.
    #: Only the existence of this top-level window key is read; never called.
    readiness_window_key_by_host: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class VendorSettings:
    """Legacy config compatibility. Affiliation no longer affects analysis."""
    home: str = ""

    @property
    def has_home(self) -> bool:
        return bool(self.home.strip())


def validate_public_origin(value):
    if not isinstance(value, str):
        raise ConfigError('[server]: public_origin must be an HTTPS origin or empty')
    if not value:
        return
    try:
        parsed = urlsplit(value)
        valid = (value.startswith('https://') and parsed.hostname and not parsed.username
                 and not parsed.password and not parsed.path and not parsed.query
                 and not parsed.fragment and not any(c.isspace() for c in value)
                 and parsed.port not in (0, 443) and value.isascii()
                 and parsed.netloc == parsed.netloc.lower())
    except ValueError:
        valid = False
    if not valid:
        raise ConfigError('[server]: public_origin must be a lowercase HTTPS origin without a path or default :443 port')


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
    # Canonical HTTPS origin behind the hosting reverse proxy. Empty for local use.
    public_origin: str = ""

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


#: All observed change types, independent of vendor and category.
DEFAULT_NOTIFY_KINDS = ("STACK_CHANGE", "NOT_SEEN", "NEW_DETECTION", "HOST_CHANGED")


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
    spread_weekly: bool = False
    daily_limit: int = 0
    not_before: str = ""

    def seconds_for(self, tier: str | None) -> float:
        raw = self.tiers.get(tier or "", self.default)
        return parse_duration(raw)


@dataclass(frozen=True)
class AnalysisSettings:
    since: str = ""
    timezone: str = "Asia/Seoul"

    @property
    def cutoff(self) -> str:
        if not self.since:
            return ""
        return datetime.combine(date.fromisoformat(self.since), datetime.min.time(),
                                ZoneInfo(self.timezone)).astimezone(UTC).isoformat()


@dataclass(frozen=True)
class PreparationSettings:
    # A dashboard-only switch can enable this without changing the live
    # collector's config file or restarting an older collector process.
    enabled: bool = field(default_factory=lambda: os.environ.get("RADAR_PREPARATION_ENABLED") == "1")
    mkb_url: str = "http://127.0.0.1:8611"
    insider_search: str = ""  # Deprecated: accepted for old configs, never used.
    claude_binary: str = field(default_factory=lambda: shutil.which("claude") or str(Path.home() / ".local/bin/claude"))
    model: str = "sonnet"
    timeout_seconds: int = 240
    # Optional private gateway keeps CLI authentication outside the web image.
    gateway_url: str = ""
    gateway_token_file: str = ""
    backend: str = "cli"  # Existing installations retain their current execution path.
    codex_binary: str = "codex"
    codex_model: str = ""  # Empty uses the Codex CLI default, never the Claude alias.
    api_model: str = ""  # Explicit API model ID; CLI aliases are not API IDs.
    api_key_file: str = ""


@dataclass(frozen=True)
class Config:
    scan: ScanSettings = field(default_factory=ScanSettings)
    vendor: VendorSettings = field(default_factory=VendorSettings)
    server: ServerSettings = field(default_factory=ServerSettings)
    ui: UISettings = field(default_factory=UISettings)
    cadence: CadenceSettings = field(default_factory=CadenceSettings)
    notify: NotifySettings = field(default_factory=NotifySettings)
    analysis: AnalysisSettings = field(default_factory=AnalysisSettings)
    preparation: PreparationSettings = field(default_factory=PreparationSettings)
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
    values = dict(values)
    default = values.pop("default", "7d")
    spread = values.pop("spread_weekly", False)
    limit = values.pop("daily_limit", 0)
    not_before = values.pop("not_before", "")
    if not_before:
        try:
            date.fromisoformat(not_before)
        except (TypeError, ValueError) as exc:
            raise ConfigError('[cadence]: not_before must be a YYYY-MM-DD date') from exc
    if not isinstance(spread, bool) or type(limit) is not int or limit < 0:
        raise ConfigError("[cadence]: spread_weekly must be boolean; daily_limit must be a nonnegative integer")
    parse_duration(default, "cadence.default")
    for tier, raw in values.items():
        parse_duration(raw, f"cadence.{tier}")
    return CadenceSettings(default=str(default), spread_weekly=spread, daily_limit=limit, not_before=not_before,
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

    unknown = set(document) - {"scan", "vendor", "server", "ui", "cadence", "notify", "analysis", "preparation"}
    if unknown:
        raise ConfigError(f"{config_path.name}: unknown sections {sorted(unknown)}")

    config = Config(
        scan=_build(ScanSettings, _section(document, "scan"), "scan"),
        vendor=_build(VendorSettings, _section(document, "vendor"), "vendor"),
        server=_build(ServerSettings, _section(document, "server"), "server"),
        ui=_build(UISettings, _section(document, "ui"), "ui"),
        cadence=_read_cadence(_section(document, "cadence")),
        notify=_read_notify(_section(document, "notify")),
        analysis=_build(AnalysisSettings, _section(document, "analysis"), "analysis"),
        preparation=_build(PreparationSettings, _section(document, "preparation"), "preparation"),
        source=str(config_path),
    )
    validate_public_origin(config.server.public_origin)
    if type(config.scan.concurrency) is not int or config.scan.concurrency < 1:
        raise ConfigError("[scan]: concurrency must be a positive integer")
    if (type(config.preparation.enabled) is not bool
            or type(config.preparation.timeout_seconds) is not int
            or not 30 <= config.preparation.timeout_seconds <= 600):
        raise ConfigError("[preparation]: enabled must be boolean; timeout_seconds must be 30..600")
    if (not isinstance(config.preparation.gateway_url, str)
            or not isinstance(config.preparation.gateway_token_file, str)
            or bool(config.preparation.gateway_url) != bool(config.preparation.gateway_token_file)):
        raise ConfigError('[preparation]: gateway_url and gateway_token_file must be set together as strings')
    p = config.preparation
    if p.backend not in ("cli", "codex", "anthropic"):
        raise ConfigError('[preparation]: backend must be cli, codex or anthropic')
    if (not isinstance(p.codex_binary, str) or not p.codex_binary.strip()
            or not isinstance(p.codex_model, str)):
        raise ConfigError('[preparation]: codex_binary must be nonempty; codex_model must be a string')
    if not isinstance(p.api_model, str) or not isinstance(p.api_key_file, str):
        raise ConfigError('[preparation]: API model and key file must be strings')
    if p.backend == "anthropic" and (not p.api_model.strip() or not p.api_key_file.strip()):
        raise ConfigError('[preparation]: anthropic requires api_model and api_key_file')
    if type(config.scan.per_site_delay_ms) is not int or config.scan.per_site_delay_ms < 0:
        raise ConfigError("[scan]: per_site_delay_ms must be a nonnegative integer")
    if type(config.scan.chromium_sandbox) is not bool:
        raise ConfigError('[scan]: chromium_sandbox must be boolean')
    waits = config.scan.minimum_observation_ms_by_host
    if (not isinstance(waits, dict) or any(
            not isinstance(host, str) or not host or host != host.lower()
            or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789.-' for c in host)
            or any(not part or part.startswith('-') or part.endswith('-') for part in host.split('.'))
            or type(ms) is not int or not 0 <= ms <= 60000 for host, ms in waits.items())):
        raise ConfigError('[scan]: minimum_observation_ms_by_host needs exact lowercase hosts and integer milliseconds 0..60000')
    checkpoints = config.scan.readiness_window_key_by_host
    if (not isinstance(checkpoints, dict) or any(
            not isinstance(host, str) or not host or host != host.lower()
            or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789.-' for c in host)
            or any(not part or part.startswith('-') or part.endswith('-') for part in host.split('.'))
            or not isinstance(key, str) or not key or len(key) > 128
            or not (key[0].isascii() and (key[0].isalpha() or key[0] in '_$'))
            or any(not (c.isascii() and (c.isalnum() or c in '_$')) for c in key)
            for host, key in checkpoints.items())):
        raise ConfigError('[scan]: readiness_window_key_by_host needs exact lowercase hosts and simple window key names')
    try:
        _ = config.analysis.cutoff
    except (ValueError, TypeError, KeyError) as exc:
        raise ConfigError("[analysis]: since must be YYYY-MM-DD and timezone must be an IANA zone") from exc
    _cache.clear()          # one live config; no reason to hold stale ones
    _cache[key] = config
    return config
