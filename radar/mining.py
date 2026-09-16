"""Finding the vendors we have no fingerprint for.

The registry only catches what someone has written a pattern for, so its
blind spot is silent by definition: a market's local vendors simply never
appear in any stack. This module inverts the registry — take every
third-party host the collector actually saw, subtract what fingerprints
already claim and what is obviously the site itself, and rank what is left
by how many companies it appears on. A host on thirty watchlist sites is
either a vendor that deserves a fingerprint or infrastructure worth naming
in the noise list; either way, somebody should look at it once.

Evidence-first on purpose: candidates come from stored scans, so every
suggested domain is one the watchlist demonstrably loads — never a guess.
"""

from __future__ import annotations

from typing import Any

from . import detector as det
from .registry import Registry
from .store import Store

#: ccTLDs that sell under second-level labels — "cu.co.kr" registers under
#: "co.kr", so the registrable unit there is three labels, not two.
_SLD_CCTLDS = frozenset({"kr", "jp", "uk", "au", "nz", "za", "br", "in", "id", "th", "tw", "hk"})
_SLDS = frozenset({
    "co", "or", "go", "ac", "ne", "re", "pe", "mil", "es", "hs", "ms", "sc", "kg",
    "com", "net", "org", "gov", "edu",
})

#: Ubiquitous non-vendor infrastructure that would otherwise top every list.
#: Deliberately tiny: anything arguable stays visible and gets judged by a
#: human instead of a blocklist.
NOISE_DOMAINS = frozenset({
    # fonts, CDNs, clouds, OS/browser plumbing
    "googleapis.com", "gstatic.com", "googleusercontent.com",
    "jsdelivr.net", "unpkg.com", "jquery.com", "bootstrapcdn.com",
    "cloudfront.net", "amazonaws.com", "on.aws", "awswaf.com", "run.app",
    "akamaihd.net", "fontawesome.com", "typekit.net", "cdnfonts.com",
    "github.io", "rawgit.com", "website-files.com", "framerstatic.com",
    "framerusercontent.com", "ampproject.org", "ipify.org",
    "cdn-apple.com", "gov.kr", "go.kr",
    # portal / social / video infrastructure that is not a vendor choice
    "kakaocdn.net", "daumcdn.net", "daum.net", "kakao.com", "onkakao.net",
    "nate.com", "navercorp.com", "google.co.kr", "adtrafficquality.google",
    "merchant-center-analytics.goog", "googlevideo.com", "ggpht.com",
    "youtube.com", "ytimg.com", "vimeo.com", "vimeocdn.com",
    "instagram.com", "cdninstagram.com", "temu.com", "opera.com",
    "openai.com", "yahoo.com", "yimg.jp", "toastcdn.net", "skplanet.com",
    # share/login widgets and app SDK infra; the *pixels* proper live on
    # facebook.net / ads-twitter.com and are fingerprinted
    "facebook.com", "twitter.com", "t.co",
})

#: Programmatic plumbing: SSP/DSP bid and cookie-sync endpoints. Loaded by
#: whichever ad stack the site runs, not chosen by the marketer — so they
#: are "known-irrelevant", hidden by default but one flag away.
AD_EXCHANGE_DOMAINS = frozenset({
    "pubmatic.com", "rubiconproject.com", "casalemedia.com", "adnxs.com",
    "bidswitch.net", "crwdcntrl.net", "rlcdn.com", "teads.tv", "1rx.io",
    "3lift.com", "smartadserver.com", "unrulymedia.com", "media.net",
    "agkn.com", "clmbtech.com", "socdm.com", "360yield.com",
    "openx.net", "openxcdn.net", "adsrvr.org", "id5-sync.com", "tapad.com",
    "quantserve.com", "bidence.net", "meba.kr", "adkernel.com",
    "exelbid.com", "ladsp.com", "adingo.jp", "ymmobi.com", "adteip.net",
    "mediacategory.com", "momento.dev",
})


def registrable_domain(host: str) -> str:
    """The unit a vendor registers: sdk.a.example.co.kr -> example.co.kr."""
    labels = [p for p in host.lower().strip(".").split(".") if p]
    if len(labels) < 2:
        return host.lower()
    if labels[-1] in _SLD_CCTLDS and len(labels) >= 3 and labels[-2] in _SLDS:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _known_domains(registry: Registry) -> set[str]:
    """Registrable domains any fingerprint's host signals already claim."""
    known: set[str] = set()
    for fp in registry:
        for signal in fp.signals_for("hosts"):
            if signal.regex is None:
                known.add(registrable_domain(signal.pattern))
    return known


def _host_matches_registry(host: str, registry: Registry) -> bool:
    for fp in registry:
        for signal in fp.signals_for("hosts"):
            if det._matches_value(signal, host):
                return True
    return False


def unknown_hosts(store: Store, registry: Registry,
                  min_targets: int = 2,
                  include_noise: bool = False,
                  include_exchanges: bool = False) -> list[dict[str, Any]]:
    """Rank third-party registrable domains no fingerprint accounts for.

    Only the latest trustworthy scan per (target, url) is read — the report
    should describe the stacks of today, not an accumulation of every host
    ever seen. Direct layers (network) drive the ranking; CSP/source-only
    sightings are reported but never counted as presence, for the same
    reason the detector keeps them out of the arithmetic.
    """
    known = _known_domains(registry)
    verdict_cache: dict[str, bool] = {}
    found: dict[str, dict[str, Any]] = {}

    for scan in store.latest_scans():
        stored = store.load_evidence(scan["id"])
        if stored is None:
            continue
        page_host = (stored.get("scan") or {}).get("page_host") or ""
        own = {registrable_domain(page_host),
               registrable_domain(det._host_of(scan["url"]))}
        pools = det._layered_values(stored.get("evidence") or {}).get("hosts", {})

        for layer, values in pools.items():
            direct = layer in det.DIRECT_LAYERS
            for value in values:
                host = det._host_of(value)
                if not host or "." not in host:
                    continue
                domain = registrable_domain(host)
                if domain in own:
                    continue
                if not include_noise and domain in NOISE_DOMAINS:
                    continue
                if not include_exchanges and domain in AD_EXCHANGE_DOMAINS:
                    continue
                if domain in known:
                    continue
                matched = verdict_cache.get(host)
                if matched is None:
                    matched = _host_matches_registry(host, registry)
                    verdict_cache[host] = matched
                if matched:
                    continue
                entry = found.setdefault(domain, {
                    "domain": domain,
                    "targets": set(), "declared_targets": set(),
                    "hosts": set(),
                })
                entry["hosts"].add(host)
                (entry["targets"] if direct
                 else entry["declared_targets"]).add(scan["target_id"])

    rows = []
    for entry in found.values():
        direct_targets = entry["targets"]
        if len(direct_targets) < min_targets:
            continue
        rows.append({
            "domain": entry["domain"],
            "targets": len(direct_targets),
            "sample_targets": sorted(direct_targets)[:5],
            "declared_only_targets": len(entry["declared_targets"] - direct_targets),
            "hosts": sorted(entry["hosts"])[:4],
        })
    rows.sort(key=lambda r: (-r["targets"], r["domain"]))
    return rows


# -- extra-URL suggestions ----------------------------------------------------

#: Path shapes that usually mean "a commerce detail page" on Korean sites.
PRODUCT_PATH_HINTS = ("/product", "/goods", "/item", "/detail", "/prod/",
                      "/p/", "/display/", "/exhibition", "/event/")


def suggest_urls(store: Store, max_per_target: int = 1) -> list[dict[str, Any]]:
    """Product-page candidates for targets watching only one URL.

    Vendors frequently load only on product or event pages; the home page
    alone under-reports the stack. Candidates come from `anchor_paths` in
    stored evidence (collected from probe v0.2.0 on), so every suggestion
    is a link the home page actually shows. Targets already watching more
    than one URL are left alone.
    """
    single_url: dict[str, Any] = {}
    for target in store.targets(enabled_only=True):
        import json as _json
        urls = _json.loads(target["urls_json"])
        if len(urls) == 1:
            single_url[target["id"]] = target

    suggestions: list[dict[str, Any]] = []
    for scan in store.latest_scans():
        target = single_url.get(scan["target_id"])
        if target is None:
            continue
        stored = store.load_evidence(scan["id"])
        if stored is None:
            continue
        paths = ((stored.get("evidence") or {}).get("dom") or {}).get("anchor_paths") or []
        hits = [p for p in paths
                if any(h in p.lower() for h in PRODUCT_PATH_HINTS)]
        if not hits:
            continue
        host = (stored.get("scan") or {}).get("page_host") or ""
        picked = sorted(hits, key=len)[:max_per_target]
        suggestions.append({
            "target_id": scan["target_id"],
            "company": target["company"],
            "urls": [f"https://{host}{p}" for p in picked],
        })
    suggestions.sort(key=lambda r: r["target_id"])
    return suggestions


# -- weak-verdict report ------------------------------------------------------


def probable_only(store: Store) -> list[dict[str, Any]]:
    """Vendors stuck at PROBABLE: one direct signal, never a second.

    Each row is a fingerprint that some companies' current stacks carry at
    PROBABLE with no DETECTED anywhere on the same company — exactly the
    patterns worth strengthening, with the layers that did fire as the
    starting point. The companion to `unknown_hosts`: that finds vendors
    with no fingerprint, this finds fingerprints with too little grip.
    """
    per_vendor: dict[str, dict[str, Any]] = {}
    for target_id, stack in store.stacks_of(("DETECTED", "PROBABLE")).items():
        for row in stack:
            if row["verdict"] != "PROBABLE":
                continue
            entry = per_vendor.setdefault(row["fingerprint_id"], {
                "fingerprint_id": row["fingerprint_id"],
                "name": row["name"], "category": row["category"],
                "targets": [], "layers": set(),
            })
            entry["targets"].append(target_id)
            for layer in (row["direct_layers"] or "").split(","):
                if layer:
                    entry["layers"].add(layer)
    rows = [{**e, "targets": sorted(e["targets"]),
             "count": len(e["targets"]), "layers": sorted(e["layers"])}
            for e in per_vendor.values()]
    rows.sort(key=lambda r: (-r["count"], r["fingerprint_id"]))
    return rows
