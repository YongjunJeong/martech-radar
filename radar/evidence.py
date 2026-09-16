"""Normalisation and redaction helpers for raw browser evidence.

Everything in this module is pure: no Playwright, no network, no disk. The
rules about what we are allowed to keep live here so they can be tested
without launching a browser.

Two rules drive the whole file:

1. We store *names*, never *values*. Cookie names but not cookie values,
   storage keys but not storage contents, hostnames but not query strings.
2. Anything that survives normalisation must still be useful to a
   fingerprint rule.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

SCHEMA_VERSION = 1

# --- URL handling -----------------------------------------------------------

# Long digit runs and long hex blobs are almost always per-user or per-session
# identifiers. Collapsing them keeps paths comparable across scans without
# storing anything identifying.
_LONG_DIGITS = re.compile(r"\d{8,}")
_LONG_HEX = re.compile(r"\b[0-9a-f]{16,}\b", re.I)
_UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I
)


def normalise_path(path: str) -> str:
    """Strip identifying blobs out of a URL path, keeping its shape."""
    if not path:
        return "/"
    path = _UUID.sub("{uuid}", path)
    path = _LONG_HEX.sub("{hex}", path)
    path = _LONG_DIGITS.sub("{num}", path)
    if len(path) > 200:
        path = path[:200] + "…"
    return path


def normalise_storage_name(name: str) -> str:
    r"""Strip identifying blobs out of a cookie / storage key name.

    A key name is a name, so the "names never values" rule allows it — but
    `ab.storage.deviceId.b9a58994-a795-...` is a per-visitor identifier
    wearing a name's clothes, and it is worthless for fingerprinting because
    the random half is different on every visit.

    Vendor patterns are prefix-anchored (`^ab\.storage\.`, `^WZRK_`,
    `^_hj`), so replacing the variable tail costs no detection accuracy. Any
    query string riding along in a key is dropped for the same reason paths
    drop theirs.
    """
    if not name:
        return ""
    name = _UUID.sub("{uuid}", name)
    name = _LONG_HEX.sub("{hex}", name)
    name = _LONG_DIGITS.sub("{num}", name)
    if "?" in name:
        name = name.split("?", 1)[0] + "?{query}"
    if len(name) > 120:
        name = name[:120] + "…"
    return name


def normalise_storage_names(names) -> list[str]:
    seen: dict[str, None] = {}
    for name in names or []:
        if isinstance(name, str):
            cleaned = normalise_storage_name(name)
            if cleaned:
                seen.setdefault(cleaned, None)
    return list(seen)


#: A watchlist URL may ask for the mobile profile: "mobile:https://…".
#: The prefix stays in the stored scan URL — that is what keeps the mobile
#: history of a page separate from its desktop history.
MOBILE_PREFIX = "mobile:"


def split_mobile(url: str) -> tuple[bool, str]:
    """("mobile:https://x/" -> (True, "https://x/")); plain URLs pass through."""
    if url.startswith(MOBILE_PREFIX):
        return True, url[len(MOBILE_PREFIX):]
    return False, url


def split_url(url: str) -> tuple[str, str]:
    """Return (host, normalised path). Query strings and fragments are dropped."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ("", "")
    host = (parts.hostname or "").lower()
    return (host, normalise_path(parts.path or "/"))


# --- Vendor account identifiers ---------------------------------------------

# These are configuration identifiers, not personal data: a GTM container ID or
# a GA measurement ID tells us which tools an account has provisioned. They are
# the one thing we deliberately pull out of query strings before dropping them,
# because a container ID lets us see tags that never fired during our visit.
_ID_PATTERNS = [
    ("gtm_container", re.compile(r"\bGTM-[A-Z0-9]{4,10}\b")),
    ("ga4_measurement", re.compile(r"\bG-[A-Z0-9]{6,12}\b")),
    ("ua_property", re.compile(r"\bUA-\d{4,12}-\d{1,4}\b")),
    ("google_ads", re.compile(r"\bAW-\d{6,15}\b")),
    ("floodlight", re.compile(r"\bDC-\d{6,15}\b")),
    ("optimize", re.compile(r"\bOPT-[A-Z0-9]{4,10}\b")),
    ("facebook_pixel", re.compile(r"facebook\.com/tr/?\?(?:[^#]*&)?id=(\d{8,20})")),
    ("hotjar_site", re.compile(r"static\.hotjar\.com/c/hotjar-(\d{4,12})\.js")),
    ("criteo_partner", re.compile(r"criteo\.(?:com|net)/js/ld/ld\.js\?a=(\d{3,10})")),
    ("kakao_pixel", re.compile(r"\bkakaoPixel\((\d{6,20})\)")),
    # The partner/advertiser slug is the account name — worth more than the
    # vendor hit alone. Insider leaks it twice (CDN subdomain + versioned
    # global); Moloco retail media names the advertiser in the event host.
    ("insider_partner", re.compile(r"\b([a-z0-9][a-z0-9-]{1,40})\.api\.useinsider\.com")),
    ("insider_partner", re.compile(r"__INSIDER_SCRIPT_VERSION_([A-Za-z0-9_]{2,40})__")),
    ("moloco_advertiser", re.compile(r"\b([a-z0-9][a-z0-9-]{1,40})-evt\.(?:rmp|mcm)-api\.moloco\.com")),
]


def extract_identifiers(text: str) -> list[dict[str, str]]:
    """Pull vendor account/container IDs out of a URL or script fragment."""
    found: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for kind, pattern in _ID_PATTERNS:
        for match in pattern.finditer(text or ""):
            value = match.group(1) if match.groups() else match.group(0)
            key = (kind, value)
            if key not in seen:
                seen.add(key)
                found.append({"kind": kind, "value": value})
    return found


# --- Hostnames referenced inside JavaScript ---------------------------------

# A TLD allowlist is what keeps `Array.prototype.slice` from looking like a
# hostname. It costs us a few exotic domains and saves us thousands of false
# positives in minified bundles.
# Word-like TLDs (`at`, `is`, `id`, `group`, `space`) are deliberately absent:
# `element.at` and `item.group` appear in every bundle and would flood the
# results with property chains masquerading as hostnames.
_TLDS = (
    "com|net|org|io|ai|co|kr|jp|cn|tw|hk|sg|au|uk|eu|de|fr|es|it|nl|ru|br|mx|tr"
    "|ca|ch|se|dk|fi|pl|cz|pt|gr|il|ae|za|ph|nz|vn|th|my|hu|ro|ua"
    "|app|dev|cloud|tech|digital|shop|store|site|agency|systems"
    "|edu|gov|info|biz|tv|cc|xyz"
)
_HOST_IN_TEXT = re.compile(
    r"(?<![\w.-])((?:[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])?\.){1,4}(?:" + _TLDS + r"))(?![\w-])"
)
# Property chains that survive the TLD filter often enough to be worth naming.
_HOST_DENYLIST = {
    "prototype.co", "prototype.com", "object.co", "window.co", "this.co",
    "node.io", "process.env", "module.exports",
}


def extract_hosts_from_text(text: str, limit: int = 400) -> list[str]:
    """Find hostnames mentioned inside script source.

    This is one of the layers a static HTML scanner cannot reach: a first-party
    bundle that talks to `sdk.braze.eu` names that host in its own source even
    when the request has not fired yet.
    """
    hosts: list[str] = []
    seen: set[str] = set()
    for match in _HOST_IN_TEXT.finditer(text or ""):
        host = match.group(1).lower().rstrip(".")
        if host in seen or host in _HOST_DENYLIST:
            continue
        label = host.split(".")[0]
        if len(label) < 3:  # `a.io`, `e.co` — minifier output, not a real host
            continue
        seen.add(host)
        hosts.append(host)
        if len(hosts) >= limit:
            break
    return hosts


# --- Content-Security-Policy -------------------------------------------------

_CSP_SOURCE = re.compile(r"(?:https?://)?(\*\.)?([a-z0-9.-]+\.[a-z]{2,})", re.I)


def hosts_from_csp(csp: str) -> list[str]:
    """Extract the hostnames a site's CSP allows.

    A CSP is a declaration of every third party the site *intends* to talk to,
    including tools that only fire on other pages or after consent. It is the
    single highest-yield header we collect.
    """
    hosts: list[str] = []
    seen: set[str] = set()
    for token in re.split(r"[;\s]+", csp or ""):
        if not token or token.startswith("'") or token.lower() in {
            "self", "none", "unsafe-inline", "unsafe-eval", "data:", "blob:",
        }:
            continue
        match = _CSP_SOURCE.search(token)
        if not match:
            continue
        host = match.group(2).lower()
        if host not in seen:
            seen.add(host)
            hosts.append(host)
    return hosts


# --- Text redaction ----------------------------------------------------------

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"\+?\d[\d\s().-]{8,}\d")
_JWT = re.compile(r"\beyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}\b")


_QUERY_STRING = re.compile(r"\?[A-Za-z0-9_.\[\]%-]+=[^\s\"'<>)]*")


def redact(text: str, limit: int = 300) -> str:
    """Scrub anything that looks personal out of free text we keep (console logs)."""
    if not text:
        return ""
    text = _EMAIL.sub("{email}", text)
    text = _JWT.sub("{token}", text)
    text = _UUID.sub("{uuid}", text)
    # Long digit runs go before the phone pattern: the phone regex is loose
    # enough to swallow a plain 12-digit ID otherwise.
    text = _LONG_DIGITS.sub("{num}", text)
    text = _PHONE.sub("{phone}", text)
    # Console warnings quote whole URLs, and a query string is where tokens
    # ride. We keep console text only to spot vendor names in it, and those
    # are never in a query value.
    text = _QUERY_STRING.sub("?{query}", text)
    text = " ".join(text.split())
    return text[:limit]


# --- Scan status -------------------------------------------------------------

STATUS_OK = "OK"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_NAV_FAILED = "NAV_FAILED"
STATUS_BLOCKED = "BLOCKED"
STATUS_BROWSER_ERROR = "BROWSER_ERROR"
STATUS_THIN = "THIN"
#: The site's own robots.txt said not to. Recorded, never inferred from.
STATUS_SKIPPED_BY_ROBOTS = "SKIPPED_BY_ROBOTS"
#: The page answered, but gave us far less than it has every other time.
#: A half-loaded page is not a page that lost half its vendors.
STATUS_PARTIAL = "PARTIAL"

#: Statuses a detection engine is allowed to treat as a real observation.
TRUSTWORTHY_STATUSES = frozenset({STATUS_OK})

_BLOCK_HTTP_STATUSES = {401, 403, 405, 406, 407, 429, 451, 503}
_BLOCK_MARKERS = (
    "just a moment",
    "checking your browser",
    "attention required",
    "access denied",
    "verify you are human",
    "captcha",
    "unusual traffic",
    "request blocked",
    "security verification",      # STCLab bot manager challenge page
    "비정상적인 접근",
    "접근이 차단",
    "자동입력 방지",
)


# A maintenance notice answers 200 OK, so `detect_block` waves it through, and
# it carries no tags. Left alone it reads as "this company removed its entire
# stack overnight" — which is exactly what a real maintenance notice produced:
# five vendors to zero, three requests, no scripts.
#
# Emptiness alone cannot be the test. A site that genuinely runs no MarTech is
# a valid observation of exactly that, and grading it "cannot tell" would
# throw away real greenfield findings. What separates the two is that one page
# *is* the site and the other is a stand-in for it — and a stand-in says so in
# its title. So both conditions are required: it announces itself as an
# interstitial, and it carries nothing.
THIN_MAX_REQUESTS = 8

_INTERSTITIAL_TITLE_MARKERS = (
    "시스템 점검", "서비스 점검", "점검 안내", "점검중", "점검 중",
    "일시 중단", "서비스 준비", "잠시 후 다시",
    "under maintenance", "scheduled maintenance", "site maintenance",
    "temporarily unavailable", "service unavailable", "be right back",
)

#: Every channel a page could show us something through.
_EVIDENCE_CHANNELS = ("scripts", "window_keys", "cookie_names",
                      "csp_hosts", "identifiers", "third_party_domains")


def detect_thin(counts: dict | None, title: str = "") -> str | None:
    """Return why this page is a stand-in rather than the site, or None.

    Requires an interstitial title *and* an empty page. Either one alone is
    not enough: a plain site is empty but real, and a page that merely
    mentions maintenance is real but not empty.
    """
    if not counts:
        return None
    marker = next((m for m in _INTERSTITIAL_TITLE_MARKERS
                   if m in (title or "").lower()), None)
    if marker is None:
        return None
    if (counts.get("requests") or 0) > THIN_MAX_REQUESTS:
        return None
    if any(counts.get(channel) for channel in _EVIDENCE_CHANNELS):
        return None
    return f"interstitial title {marker!r} on a page with no observable evidence"


#: Hosts that are never anyone's website: a domain-parking page, or a bare
#: S3 bucket serving a gating page to addresses the site does not like.
#: Landing on one of these is a stand-in for the site, not the site.
_STANDIN_HOSTS = frozenset({
    "amazonaws.com", "perfectdomain.com", "unstoppable.ai", "unstoppabledomains.com",
    "sedoparking.com", "hugedomains.com", "dan.com", "afternic.com", "godaddy.com",
})


def detect_block(http_status: int | None, title: str, visible_text: str,
                 page_host: str | None = None) -> str | None:
    """Return the marker that suggests we were blocked, or None.

    We never try to get around a block. We just need `BLOCKED` to be a distinct
    state so that a bot wall is never mistaken for 'this site removed Braze'.
    """
    haystack = f"{title or ''} {visible_text or ''}".lower()
    for marker in _BLOCK_MARKERS:
        if marker in haystack:
            return f"page_text:{marker}"
    if http_status in _BLOCK_HTTP_STATUSES:
        return f"http_status:{http_status}"
    if page_host and base_domain(page_host) in _STANDIN_HOSTS:
        return f"standin_host:{base_domain(page_host)}"
    return None


# --- Domain grouping ---------------------------------------------------------

# Enough of the public suffix list to get Korean and common international sites
# right without pulling in a dependency that needs periodic updates.
_TWO_LEVEL_SUFFIXES = {
    # Korea and Japan, where this started
    "co.kr", "or.kr", "ne.kr", "go.kr", "re.kr", "pe.kr", "ac.kr", "hs.kr",
    "co.jp", "or.jp", "ne.jp", "ac.jp",
    # APAC
    "com.au", "com.cn", "com.tw", "com.hk", "com.sg", "co.in", "co.id",
    "co.th", "com.vn", "co.nz", "com.my", "com.ph", "com.pk", "com.bd",
    # EMEA
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.tr", "co.za", "com.eg",
    "com.sa", "com.ua", "com.pl", "com.ro", "com.gr", "com.cy", "com.ru",
    "co.il", "com.ng", "com.ke", "com.gh", "com.ma", "com.dz",
    # Americas
    "com.br", "com.mx", "com.ar", "com.co", "com.pe", "com.ve", "com.uy",
    "com.ec", "com.do", "com.gt", "com.py", "com.bo",
}


#: Chrome's own error page. Playwright reports a navigation that ended on it
#: as a success with a page — and the page is Chrome's, not the site's.
BROWSER_ERROR_HOSTS = frozenset({"chromewebdata", "chrome-error"})


def is_browser_error_page(final_url: str | None, page_host: str | None) -> bool:
    return (page_host or "").lower() in BROWSER_ERROR_HOSTS or \
        (final_url or "").lower().startswith("chrome-error://")


def base_domain(host: str) -> str:
    """Group `abc.api.useinsider.com` and `cdn.useinsider.com` under one owner."""
    host = (host or "").lower().strip(".")
    if not host or host.replace(".", "").isdigit():
        return host
    parts = host.split(".")
    if len(parts) < 3:
        return host
    if ".".join(parts[-2:]) in _TWO_LEVEL_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def is_third_party(host: str, page_host: str) -> bool:
    """True when a request left the site's own domain."""
    if not host or not page_host:
        return False
    return base_domain(host) != base_domain(page_host)
