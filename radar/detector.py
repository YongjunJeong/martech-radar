"""Turn collected evidence into per-vendor verdicts.

The rule we care about commercially is not "did a string appear" but "how
independently was it seen". A vendor named inside a CSP header is a policy
that permits it; a vendor whose hostname the browser actually contacted,
whose global the page created and whose IndexedDB it opened is a vendor
that is running. Those two must never come out with the same verdict, or
the removal signals turn into noise.

So every match is attributed to an evidence *layer*, layers are split into
direct (observed executing) and indirect (merely referenced), and the
verdict is a function of the layers, not the number of patterns hit.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any

from . import evidence as ev
from .registry import Fingerprint, Registry, Signal, STRENGTH_STRONG

DETECTOR_VERSION = "0.1.0"

VERDICT_DETECTED = "DETECTED"
VERDICT_PROBABLE = "PROBABLE"
VERDICT_NOT_DETECTED = "NOT_DETECTED"

LAYER_NETWORK = "network"
LAYER_RUNTIME = "runtime"
LAYER_STORAGE = "storage"
LAYER_DOM = "dom"
LAYER_CONSOLE = "console"
LAYER_SOURCE = "source"
LAYER_DECLARED = "declared"

DIRECT_LAYERS = frozenset({LAYER_NETWORK, LAYER_RUNTIME, LAYER_STORAGE, LAYER_DOM, LAYER_CONSOLE})
INDIRECT_LAYERS = frozenset({LAYER_SOURCE, LAYER_DECLARED})

# A layer contributes at most one score, so a vendor with twenty host
# patterns cannot out-score a vendor with one — breadth of *evidence*
# counts, breadth of *patterns* does not.
_POINTS = {STRENGTH_STRONG: 2.0, "weak": 1.0}

THRESHOLD_DETECTED = 2.0
THRESHOLD_PROBABLE = 1.0

MAX_MATCHES_KEPT = 12

# Substring matching would be wrong here: `notuseinsider.com` must not match
# `useinsider.com`. Domain matching is exact-or-subdomain.
_CASE_SENSITIVE_FIELDS = frozenset({"window"})
_SUBSTRING_FIELDS = frozenset({"scripts", "console"})


@dataclass(frozen=True)
class Match:
    layer: str
    field: str
    pattern: str
    value: str
    strength: str

    def as_dict(self) -> dict[str, str]:
        return {
            "layer": self.layer,
            "field": self.field,
            "pattern": self.pattern,
            "value": self.value,
            "strength": self.strength,
        }


@dataclass
class Detection:
    id: str
    name: str
    category: str
    verdict: str
    score: float
    layers: dict[str, float] = dc_field(default_factory=dict)
    matches: list[Match] = dc_field(default_factory=list)
    indirect_only: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "verdict": self.verdict,
            "score": round(self.score, 2),
            "layers": {k: round(v, 2) for k, v in sorted(self.layers.items())},
            "direct_layers": sorted(layer for layer in self.layers if layer in DIRECT_LAYERS),
            "indirect_layers": sorted(layer for layer in self.layers if layer in INDIRECT_LAYERS),
            "indirect_only": self.indirect_only,
            "matches": [m.as_dict() for m in self.matches[:MAX_MATCHES_KEPT]],
            "match_count": len(self.matches),
        }


def _host_of(url_or_host: str) -> str:
    value = url_or_host.strip()
    if not value:
        return ""
    if "//" in value:
        value = value.split("//", 1)[1]
    return value.split("/", 1)[0].split("?", 1)[0].split("@")[-1].split(":")[0].lower()


def _strings(values: Any) -> list[str]:
    if not values:
        return []
    if isinstance(values, dict):
        out: list[str] = []
        for key, nested in values.items():
            out.append(str(key))
            if isinstance(nested, list):
                out.extend(f"{key}.{child}" for child in nested if isinstance(child, str))
        return out
    if isinstance(values, str):
        return [values]
    return [v for v in values if isinstance(v, str)]


def _layered_values(evidence: dict[str, Any]) -> dict[str, dict[str, list[str]]]:
    """Flatten a scan result into {field: {layer: [values]}}."""
    net = evidence.get("network") or {}
    runtime = evidence.get("runtime") or {}
    storage = evidence.get("storage") or {}
    dom = evidence.get("dom") or {}

    scripts = _strings(evidence.get("scripts"))
    request_hosts = [r.get("host", "") for r in (net.get("requests") or []) if isinstance(r, dict)]
    perf_hosts = [p.get("host", "") for p in (net.get("performance_entries") or []) if isinstance(p, dict)]

    network_hosts = (
        _strings(net.get("hosts"))
        + _strings(net.get("third_party_domains"))
        + _strings(net.get("failed_hosts"))
        + request_hosts
        + perf_hosts
        + _strings(dom.get("iframe_hosts"))
        + [_host_of(s) for s in scripts]
    )

    console_texts = [
        c.get("text", "") for c in (evidence.get("console") or []) if isinstance(c, dict)
    ]

    return {
        "hosts": {
            LAYER_NETWORK: network_hosts,
            LAYER_SOURCE: _strings(evidence.get("referenced_hosts")),
            LAYER_DECLARED: _strings(evidence.get("csp_hosts")),
        },
        "scripts": {LAYER_NETWORK: scripts},
        "window": {
            LAYER_RUNTIME: _strings(runtime.get("window_keys")) + _strings(runtime.get("nested_keys")),
        },
        "storage": {
            LAYER_STORAGE: (
                _strings(storage.get("cookie_names"))
                + _strings(storage.get("local_storage_keys"))
                + _strings(storage.get("session_storage_keys"))
                + _strings(storage.get("indexeddb_names"))
                + _strings(storage.get("cache_names"))
                + _strings(evidence.get("set_cookie_names"))
                + _strings(evidence.get("service_workers"))
            ),
        },
        "tokens": {LAYER_SOURCE: _strings(runtime.get("inline_script_tokens"))},
        "dom": {
            LAYER_DOM: (
                _strings(dom.get("custom_elements"))
                + _strings(dom.get("data_attributes"))
                + _strings(dom.get("meta_tags"))
                + _strings(dom.get("link_hrefs"))
            ),
        },
        "console": {LAYER_CONSOLE: console_texts},
    }


def _matches_value(signal: Signal, value: str) -> bool:
    if not value:
        return False
    if signal.regex is not None:
        return signal.regex.search(value) is not None
    if signal.field == "hosts":
        host = _host_of(value)
        target = signal.pattern.lower()
        return host == target or host.endswith("." + target)
    if signal.field in _SUBSTRING_FIELDS:
        return signal.pattern.lower() in value.lower()
    if signal.field in _CASE_SENSITIVE_FIELDS:
        return value == signal.pattern
    return value.lower() == signal.pattern.lower()


def _detect_one(fp: Fingerprint, pools: dict[str, dict[str, list[str]]]) -> Detection:
    layers: dict[str, float] = {}
    matches: list[Match] = []

    for signal in fp.signals:
        for layer, values in pools.get(signal.field, {}).items():
            hit: str | None = None
            for value in values:
                if _matches_value(signal, value):
                    hit = value
                    break
            if hit is None:
                continue
            points = _POINTS[signal.strength]
            layers[layer] = max(layers.get(layer, 0.0), points)
            matches.append(
                Match(
                    layer=layer,
                    field=signal.field,
                    pattern=signal.pattern,
                    value=ev.redact(hit, 120),
                    strength=signal.strength,
                )
            )

    # Only direct layers set the verdict. Indirect evidence is kept in the
    # trail because "allowed by CSP" and "named in the bundle" are useful
    # context, but it must never be arithmetic: a vendor listed in a policy
    # and mentioned in a script would otherwise add up to DETECTED without
    # the browser ever having contacted it.
    score = sum(v for k, v in layers.items() if k in DIRECT_LAYERS)
    indirect = sorted(k for k in layers if k in INDIRECT_LAYERS)
    if score >= THRESHOLD_DETECTED:
        verdict = VERDICT_DETECTED
    elif score >= THRESHOLD_PROBABLE or indirect:
        verdict = VERDICT_PROBABLE
    else:
        verdict = VERDICT_NOT_DETECTED

    matches.sort(key=lambda m: (m.layer not in DIRECT_LAYERS, m.strength != STRENGTH_STRONG, m.layer))
    return Detection(
        id=fp.id,
        name=fp.name,
        category=fp.category,
        verdict=verdict,
        score=score,
        layers=layers,
        matches=matches,
        indirect_only=bool(indirect) and score == 0.0,
    )


def detect(result: dict[str, Any], registry: Registry) -> dict[str, Any]:
    """Classify one scan result. Never judges an untrustworthy scan."""
    scan = result.get("scan") or {}
    status = scan.get("status")
    header = {
        "detector_version": DETECTOR_VERSION,
        "fingerprint_files": list(registry.source_files),
        "fingerprint_count": len(registry),
        "url": scan.get("url"),
        "page_host": scan.get("page_host"),
        "scan_status": status,
        "scanned_at": scan.get("started_at"),
    }

    if status not in ev.TRUSTWORTHY_STATUSES:
        # A bot wall or a timeout tells us nothing about the vendor stack.
        # Reporting NOT_DETECTED here is how a scanner invents a churn event.
        return {
            **header,
            "judged": False,
            "reason": f"scan status {status!r} is not trustworthy; detection skipped",
            "detections": [],
        }

    pools = _layered_values(result.get("evidence") or {})
    detections = [_detect_one(fp, pools) for fp in registry]
    found = [d for d in detections if d.verdict != VERDICT_NOT_DETECTED]
    found.sort(key=lambda d: (-d.score, d.category, d.name.lower()))

    return {
        **header,
        "judged": True,
        "reason": None,
        "detections": [d.as_dict() for d in found],
    }
