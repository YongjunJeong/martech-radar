"""Browser evidence collector.

Given one public URL, drive a real Chromium session, watch what it loads and
executes, and return a structured, privacy-safe record of the evidence.

This module deliberately knows nothing about vendors. It never names one
or Braze or any fingerprint. That separation is what lets us re-run detection
against evidence collected months ago when a fingerprint rule improves, without
re-scanning a single site.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from functools import lru_cache
from dataclasses import dataclass, asdict, field
from datetime import datetime, UTC
from pathlib import Path

from playwright.async_api import async_playwright, Error as PlaywrightError

from . import config as _config
from . import evidence as ev
from .robots import RobotsCache


@lru_cache(maxsize=1)
def _settings() -> _config.Config:
    """Read radar.toml once per process."""
    return _config.load()

COLLECTOR_VERSION = "0.2.0"

_PROBE_JS = (Path(__file__).parent / "probe.js").read_text(encoding="utf-8")

# Android Chrome, for `mobile:`-prefixed URLs — Korean commerce sites
# routinely serve a different tag stack to mobile visitors on the same URL.
MOBILE_USER_AGENT = ("Mozilla/5.0 (Linux; Android 14; SM-S921N) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/124.0.0.0 Mobile Safari/537.36")
MOBILE_VIEWPORT = {"width": 390, "height": 844}

# A standard desktop Chrome UA. This is not an anti-detection measure: several
# MarTech SDKs skip initialisation for headless user agents, which would show up
# as a false NOT_DETECTED. We record the exact UA in every result so a reader
# always knows what the site was shown.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

# Headers on the main document that carry technology signal. Values are kept
# because they are server configuration, not user data.
_INTERESTING_HEADERS = (
    "server", "x-powered-by", "via", "x-cache", "x-served-by", "x-vercel-id",
    "x-nextjs-cache", "x-shopify-stage", "x-magento-cache-debug", "x-drupal-cache",
    "x-generator", "x-aspnet-version", "x-akamai-transformed", "cf-ray",
    "content-security-policy", "content-security-policy-report-only",
    "report-to", "reporting-endpoints", "x-amz-cf-id", "x-goog-generation",
    "x-litespeed-cache", "x-varnish", "x-application-context", "x-edge-origin",
)


@dataclass
class ScanConfig:
    """Every timing and size bound the scanner obeys, in one place."""

    nav_timeout_ms: int = 20_000
    #: Quiet observation after load — SDKs frequently self-initialise on a timer.
    settle_ms: int = 6_000
    #: Scroll and move the mouse to wake up lazily-loaded tags.
    interact: bool = True
    interaction_ms: int = 3_000
    #: Absolute ceiling for one scan. One bad site must never hang the batch.
    hard_timeout_ms: int = 90_000
    #: Read script bodies to find vendor hostnames referenced but not yet called.
    scan_js_bodies: bool = True
    max_js_bodies: int = 40
    max_js_body_bytes: int = 600_000
    max_console_messages: int = 150
    user_agent: str | None = DEFAULT_USER_AGENT
    viewport_width: int = 1440
    viewport_height: int = 900
    locale: str = field(default_factory=lambda: _settings().scan.locale)
    timezone_id: str = field(default_factory=lambda: _settings().scan.timezone)
    headless: bool = True
    #: Honour each site's robots.txt. See `radar/robots.py`.
    respect_robots: bool = field(default_factory=lambda: _settings().scan.respect_robots)
    robots_user_agent: str = field(default_factory=lambda: _settings().scan.robots_user_agent)
    #: Minimum gap between two requests to the same site.
    per_site_delay_ms: int = field(default_factory=lambda: _settings().scan.per_site_delay_ms)


class Collector:
    """Owns one Chromium process; scans run in fresh isolated contexts.

    Reuse the same Collector across a batch — launching Chromium costs about a
    second, and every scan still starts from an empty cookie jar.
    """

    def __init__(self, config: ScanConfig | None = None):
        self.config = config or ScanConfig()
        self._pw = None
        self._browser = None
        self._baseline_window_keys: set[str] = set()
        self._robots = RobotsCache(user_agent=self.config.robots_user_agent)
        # Politeness is per site, not global: two different sites have no
        # reason to wait for each other.
        self._last_visit: dict[str, float] = {}
        self._site_locks: dict[str, asyncio.Lock] = {}
        self._restart_lock = asyncio.Lock()

    async def __aenter__(self) -> Collector:
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self.config.headless,
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )
        self._baseline_window_keys = await self._capture_baseline_keys()

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._pw:
            await self._pw.stop()
            self._pw = None

    async def _ensure_browser(self) -> None:
        """Relaunch Chromium if the driver died under us.

        One site's JS dialog racing a page close is enough to take the whole
        Node driver down, and without this every job queued behind it would
        come back BROWSER_ERROR in a few milliseconds each.
        """
        if self._browser is not None and self._browser.is_connected():
            return
        async with self._restart_lock:
            if self._browser is not None and self._browser.is_connected():
                return
            try:
                await self.stop()
            except Exception:  # noqa: BLE001 - the old driver is already gone
                self._browser = None
                self._pw = None
            await self.start()

    _BASELINE_URL = "https://radar-baseline.invalid/"

    async def _capture_baseline_keys(self) -> set[str]:
        """Global names an empty page already has.

        Subtracting these turns a 1,200-name dump into the handful of names the
        site actually created, which is the difference between noise and
        evidence.

        The baseline page is served by route interception rather than
        `about:blank`: an opaque origin is missing roughly 240 globals that a
        normal https document exposes, and every one of those would otherwise
        look like something the site introduced.
        """
        context = await self._browser.new_context(
            user_agent=self.config.user_agent,
            viewport={"width": self.config.viewport_width, "height": self.config.viewport_height},
        )
        page = await context.new_page()
        collect_js = (
            "() => { const k = []; for (const x in window) k.push(x); "
            "return k.concat(Object.getOwnPropertyNames(window)); }"
        )
        try:
            await context.route(
                "**/*",
                lambda route: route.fulfill(
                    status=200,
                    content_type="text/html",
                    body="<!doctype html><html><head></head><body></body></html>",
                ),
            )
            keys: set[str] = set()
            try:
                await page.goto(self._BASELINE_URL, wait_until="domcontentloaded", timeout=10_000)
                keys |= set(await page.evaluate(collect_js))
            except PlaywrightError:
                pass
            await page.goto("about:blank")
            keys |= set(await page.evaluate(collect_js))
            return keys
        finally:
            await context.close()

    def remember_robots_in(self, memory) -> None:
        """Persist robots.txt answers somewhere that outlives this process."""
        self._robots.memory = memory

    async def _fetch_text(self, url: str) -> tuple[int, str | None]:
        """Fetch a small text file the same way the scan fetches the site.

        With a plain HTTP client this loses to the bot protection in front
        of many sites — which produced the worst possible outcome: the
        robots.txt of the sites most likely to have rules was the robots.txt
        we could not read, so those sites got *less* deference than the ones
        that publish openly. Navigating a real page gets the same answer a
        visitor would.
        """
        context = await self._browser.new_context(
            user_agent=self.config.user_agent,
            locale=self.config.locale,
            timezone_id=self.config.timezone_id,
        )
        try:
            page = await context.new_page()
            response = await page.goto(url, wait_until="domcontentloaded", timeout=12_000)
            if response is None:
                return 0, None
            if not response.ok:
                return response.status, None
            body = await page.evaluate("() => document.body ? document.body.innerText : ''")
            return response.status, body
        finally:
            await context.close()

    async def _be_polite(self, url: str, extra_delay: float | None) -> None:
        """Wait out the per-site gap before touching the same site again."""
        origin = RobotsCache.origin_of(url)
        lock = self._site_locks.setdefault(origin, asyncio.Lock())
        gap = max(self.config.per_site_delay_ms / 1000, extra_delay or 0.0)
        async with lock:
            last = self._last_visit.get(origin)
            if last is not None:
                waited = time.monotonic() - last
                if waited < gap:
                    await asyncio.sleep(gap - waited)
            self._last_visit[origin] = time.monotonic()

    async def scan(self, url: str) -> dict:
        """Scan one URL. Always returns a result dict; never raises."""
        if self._browser is None:
            raise RuntimeError("Collector.start() must be called before scan()")
        await self._ensure_browser()
        started = time.monotonic()
        started_at = datetime.now(UTC)

        _, real_url = ev.split_mobile(url)
        crawl_delay: float | None = None
        if self.config.respect_robots:
            decision = await self._robots.check(real_url, self._fetch_text)
            crawl_delay = decision.crawl_delay
            if not decision.allowed:
                result = _empty_result(url)
                result["scan"]["status"] = ev.STATUS_SKIPPED_BY_ROBOTS
                result["scan"]["block_marker"] = decision.reason
                result["warnings"].append(
                    "robots.txt disallows this URL — not visited, and nothing "
                    "may be inferred from the absence")
                result["scan"]["started_at"] = started_at.isoformat()
                result["scan"]["duration_ms"] = int((time.monotonic() - started) * 1000)
                result["scan"]["collector_version"] = COLLECTOR_VERSION
                result["scan"]["config"] = asdict(self.config)
                return result

        await self._be_polite(real_url, crawl_delay)
        try:
            result = await asyncio.wait_for(
                self._scan_inner(url),
                timeout=self.config.hard_timeout_ms / 1000,
            )
        except TimeoutError:
            result = _empty_result(url)
            result["scan"]["status"] = ev.STATUS_TIMEOUT
            result["warnings"].append(
                f"hard timeout after {self.config.hard_timeout_ms}ms — no evidence collected"
            )
        except PlaywrightError as exc:
            result = _empty_result(url)
            result["scan"]["status"] = ev.STATUS_BROWSER_ERROR
            result["scan"]["error"] = ev.redact(str(exc), 400)
        except Exception as exc:  # noqa: BLE001 - a scan must never kill the batch
            result = _empty_result(url)
            result["scan"]["status"] = ev.STATUS_BROWSER_ERROR
            result["scan"]["error"] = f"{type(exc).__name__}: {ev.redact(str(exc), 300)}"

        result["scan"]["started_at"] = started_at.isoformat()
        result["scan"]["duration_ms"] = int((time.monotonic() - started) * 1000)
        result["scan"]["collector_version"] = COLLECTOR_VERSION
        result["scan"]["config"] = asdict(self.config)
        return result

    # -- the actual session ---------------------------------------------------

    async def _scan_inner(self, url: str) -> dict:
        cfg = self.config
        mobile, real_url = ev.split_mobile(url)
        result = _empty_result(url)      # the prefixed URL is the identity
        result["scan"]["profile"] = "mobile" if mobile else "desktop"
        warnings: list[str] = result["warnings"]

        context = await self._browser.new_context(
            user_agent=MOBILE_USER_AGENT if mobile else cfg.user_agent,
            viewport=(dict(MOBILE_VIEWPORT) if mobile
                      else {"width": cfg.viewport_width, "height": cfg.viewport_height}),
            is_mobile=mobile,
            has_touch=mobile,
            locale=cfg.locale,
            timezone_id=cfg.timezone_id,
            ignore_https_errors=True,
        )
        page = await context.new_page()
        recorder = _SessionRecorder(cfg)
        recorder.attach(page, context)

        try:
            response = None
            try:
                # `commit` resolves as soon as the server responds. Waiting for
                # DOMContentLoaded here instead would mark every heavy retail
                # site as TIMEOUT — a heavy retail home page can take 20s to reach it —
                # and a TIMEOUT is excluded from detection, so we would throw
                # away perfectly good evidence on exactly the accounts we care
                # about most. Real failures (DNS, TLS, refused) still raise.
                response = await page.goto(
                    real_url, wait_until="commit", timeout=cfg.nav_timeout_ms
                )
            except PlaywrightError as exc:
                message = str(exc).splitlines()[0]
                if "Timeout" in message:
                    result["scan"]["status"] = ev.STATUS_TIMEOUT
                    warnings.append(f"navigation timeout: {ev.redact(message, 200)}")
                else:
                    result["scan"]["status"] = ev.STATUS_NAV_FAILED
                    result["scan"]["error"] = ev.redact(message, 300)
                    # A DNS or TLS failure leaves nothing to observe.
                    await context.close()
                    return result

            # DOM readiness and `load` are both best-effort. Ad-heavy pages
            # often never fire `load` cleanly, and we would rather observe a
            # partially loaded page than give up on it.
            for state, budget in (
                ("domcontentloaded", cfg.nav_timeout_ms),
                ("load", cfg.settle_ms),
            ):
                try:
                    await page.wait_for_load_state(state, timeout=budget)
                except PlaywrightError:
                    warnings.append(f"{state} did not fire within its budget")

            if cfg.interact:
                await self._nudge(page, warnings)

            await page.wait_for_timeout(cfg.settle_ms)

            probe = await self._run_probe(page, warnings)
            await recorder.drain()

            self._assemble(result, url, page, response, probe, recorder, warnings)
        finally:
            try:  # noqa: SIM105 — a failed close must not mask the scan result
                await context.close()
            except PlaywrightError:
                pass
        return result

    async def _nudge(self, page, warnings: list[str]) -> None:
        """Scroll and move the pointer so lazily-triggered tags actually fire.

        Plenty of personalisation and recommendation SDKs bind to first scroll or
        first interaction. Without this, a scanner sees a strictly smaller stack
        than a real visitor would.
        """
        try:
            await page.mouse.move(400, 300)
            for ratio in (0.3, 0.6, 0.9):
                await page.evaluate(
                    "(r) => window.scrollTo({top: document.body.scrollHeight * r, behavior: 'instant'})",
                    ratio,
                )
                await page.wait_for_timeout(self.config.interaction_ms // 4)
            await page.evaluate("() => window.scrollTo({top: 0, behavior: 'instant'})")
            await page.mouse.move(700, 500)
            await page.wait_for_timeout(self.config.interaction_ms // 4)
        except PlaywrightError as exc:
            warnings.append(f"interaction skipped: {ev.redact(str(exc), 150)}")

    async def _run_probe(self, page, warnings: list[str]) -> dict:
        try:
            return await page.evaluate(_PROBE_JS)
        except PlaywrightError as exc:
            warnings.append(f"runtime probe failed: {ev.redact(str(exc), 200)}")
            return {}

    # -- turning raw observations into the output contract --------------------

    def _assemble(self, result, url, page, response, probe, recorder, warnings) -> None:
        scan = result["scan"]
        page_info = probe.get("page_info") or {}
        final_url = page.url or url
        page_host, _ = ev.split_url(final_url)

        scan["final_url"] = final_url.split("?")[0]
        scan["page_host"] = page_host
        scan["title"] = page_info.get("title", "")
        scan["http_status"] = response.status if response else None

        if ev.is_browser_error_page(final_url, page_host):
            # `goto` committed, but on Chrome's error page: there is no site
            # here to observe, and its host must not enter the history.
            scan["status"] = ev.STATUS_NAV_FAILED
            scan["error"] = f"browser error page ({final_url.split('?')[0]})"
            scan["page_host"] = None
            return

        block_marker = ev.detect_block(
            scan["http_status"], scan["title"], page_info.get("visible_text_head", ""),
            page_host
        )
        if block_marker:
            scan["status"] = ev.STATUS_BLOCKED
            scan["block_marker"] = block_marker
            warnings.append(
                "site appears to be blocking automated access — this result must "
                "never be read as evidence that a technology was removed"
            )
        elif scan["status"] == ev.STATUS_OK and not probe:
            scan["status"] = ev.STATUS_BROWSER_ERROR
            warnings.append("runtime probe returned nothing")

        headers = recorder.document_headers
        csp_text = " ".join(
            filter(None, [
                headers.get("content-security-policy", ""),
                headers.get("content-security-policy-report-only", ""),
                probe.get("meta_csp", ""),
            ])
        )

        # Hostnames named inside script source but never actually contacted.
        referenced = list(recorder.referenced_hosts)
        referenced += ev.extract_hosts_from_text(probe.get("inline_script_sample", ""), 300)

        identifiers = list(recorder.identifiers)
        identifiers += ev.extract_identifiers(probe.get("inline_script_sample", "") or "")
        for src in probe.get("script_srcs", []) or []:
            identifiers += ev.extract_identifiers(src)
        for name in (probe.get("nested_keys", {}) or {}).get("google_tag_manager", []):
            identifiers += ev.extract_identifiers(name)

        window_keys = [
            k for k in (probe.get("window_keys") or [])
            if k not in self._baseline_window_keys
        ]

        perf_hosts, perf_entries = [], []
        for entry in probe.get("performance_resources", []) or []:
            kind, _, res_url = entry.partition(" ")
            host, path = ev.split_url(res_url)
            if not _is_real_host(host):
                continue
            perf_hosts.append(host)
            perf_entries.append({"initiator": kind, "host": host, "path": path})

        result["evidence"] = {
            "network": {
                "requests": recorder.requests,
                "hosts": _sorted_unique([r["host"] for r in recorder.requests] + perf_hosts),
                "third_party_domains": _sorted_unique([
                    ev.base_domain(r["host"]) for r in recorder.requests
                    if ev.is_third_party(r["host"], page_host)
                ]),
                "failed_hosts": _sorted_unique(recorder.failed_hosts),
                "performance_entries": perf_entries[:400],
            },
            "scripts": _sorted_unique(
                [f"{r['host']}{r['path']}" for r in recorder.requests
                 if r["resource_type"] == "script"]
                + [f"{h}{p}" for h, p in (ev.split_url(s) for s in probe.get("script_srcs", []) or []) if h]
            ),
            "referenced_hosts": _sorted_unique(referenced),
            "identifiers": _dedupe_identifiers(identifiers),
            "headers": headers,
            "csp_hosts": ev.hosts_from_csp(csp_text),
            "set_cookie_names": ev.normalise_storage_names(recorder.set_cookie_names),
            "runtime": {
                "window_keys": sorted(window_keys),
                "nested_keys": probe.get("nested_keys", {}),
                "inline_script_tokens": probe.get("inline_script_tokens", []),
            },
            # Key *names*, with any per-visitor blob in them normalised away:
            # `ab.storage.deviceId.<uuid>` is an identifier wearing a name's
            # clothes, and the random half never helps a fingerprint match.
            "storage": {
                "cookie_names": ev.normalise_storage_names(probe.get("cookie_names")),
                "local_storage_keys": ev.normalise_storage_names(probe.get("local_storage_keys")),
                "session_storage_keys": ev.normalise_storage_names(probe.get("session_storage_keys")),
                "indexeddb_names": ev.normalise_storage_names(probe.get("indexeddb_names")),
                "cache_names": ev.normalise_storage_names(probe.get("cache_names")),
            },
            "service_workers": probe.get("service_workers", []),
            "dom": {
                "link_hrefs": _sorted_unique(
                    [f"{h}{p}" for h, p in (ev.split_url(u) for u in probe.get("link_hrefs", []) or []) if h]
                ),
                "iframe_hosts": _sorted_unique(
                    [ev.split_url(u)[0] for u in probe.get("iframe_srcs", []) or []]
                ),
                "anchor_paths": probe.get("anchor_paths", []),
                "meta_tags": probe.get("meta_tags", []),
                "custom_elements": probe.get("custom_elements", []),
                "data_attributes": probe.get("data_attributes", []),
                "html_length": page_info.get("html_length", 0),
                "frame_count": page_info.get("frame_count", 0),
                "lang": page_info.get("lang", ""),
            },
            "console": recorder.console,
        }

        warnings.extend(probe.get("probe_errors", []) or [])
        result["counts"] = {
            "requests": len(recorder.requests),
            "hosts": len(result["evidence"]["network"]["hosts"]),
            "third_party_domains": len(result["evidence"]["network"]["third_party_domains"]),
            "scripts": len(result["evidence"]["scripts"]),
            "window_keys": len(window_keys),
            "referenced_hosts": len(result["evidence"]["referenced_hosts"]),
            "csp_hosts": len(result["evidence"]["csp_hosts"]),
            "cookie_names": len(result["evidence"]["storage"]["cookie_names"]),
            "identifiers": len(result["evidence"]["identifiers"]),
        }

        # Last, because it needs the counts: a 200 response that carries no
        # observable stack is not an observation of the site.
        if scan["status"] == ev.STATUS_OK:
            thin = ev.detect_thin(result["counts"], scan["title"])
            if thin:
                scan["status"] = ev.STATUS_THIN
                scan["block_marker"] = f"thin:{thin}"
                warnings.append(
                    f"page carries no observable stack ({thin}) — likely a "
                    "maintenance notice or redirect stub; excluded from detection"
                )


class _SessionRecorder:
    """Collects everything observable from outside the page."""

    def __init__(self, config: ScanConfig):
        self.config = config
        self.requests: list[dict] = []
        self.failed_hosts: list[str] = []
        self.console: list[dict] = []
        self.identifiers: list[dict] = []
        self.referenced_hosts: set[str] = set()
        self.document_headers: dict[str, str] = {}
        self.set_cookie_names: list[str] = []
        self._index: dict[tuple, int] = {}
        self._body_tasks: list[asyncio.Task] = []
        self._bodies_read = 0
        self._main_document_seen = False

    def attach(self, page, context) -> None:
        page.on("request", self._on_request)
        page.on("response", self._on_response)
        page.on("requestfailed", self._on_request_failed)
        page.on("console", self._on_console)
        page.on("pageerror", self._on_page_error)
        # Handled here rather than by the driver's default: a dialog opening
        # while the page is being closed makes the default dismiss reject
        # inside Node, and an unhandled rejection there kills the browser.
        page.on("dialog", self._on_dialog)

    # -- listeners ------------------------------------------------------------

    async def _on_dialog(self, dialog) -> None:
        with contextlib.suppress(PlaywrightError):
            await dialog.dismiss()

    def _on_request(self, request) -> None:
        try:
            url = request.url
            if url.startswith(("data:", "blob:", "about:")):
                return
            # Account IDs are read from the full URL before the query is dropped.
            self.identifiers.extend(ev.extract_identifiers(url))
            host, path = ev.split_url(url)
            if not _is_real_host(host):
                return
            key = (host, path, request.resource_type, request.method)
            if key in self._index:
                self.requests[self._index[key]]["count"] += 1
                return
            self._index[key] = len(self.requests)
            self.requests.append({
                "host": host,
                "path": path,
                "resource_type": request.resource_type,
                "method": request.method,
                "status": None,
                "count": 1,
                "navigation": request.is_navigation_request(),
            })
        except Exception:  # noqa: BLE001 - a listener must never break a scan
            pass

    def _on_response(self, response) -> None:
        try:
            host, path = ev.split_url(response.url)
            key = (host, path, response.request.resource_type, response.request.method)
            if key in self._index:
                self.requests[self._index[key]]["status"] = response.status

            if not self._main_document_seen and response.request.is_navigation_request():
                self._main_document_seen = True
                self._body_tasks.append(asyncio.create_task(self._read_headers(response)))

            if (
                self.config.scan_js_bodies
                and response.request.resource_type == "script"
                and self._bodies_read < self.config.max_js_bodies
            ):
                self._bodies_read += 1
                self._body_tasks.append(asyncio.create_task(self._read_script(response)))
        except Exception:  # noqa: BLE001
            pass

    def _on_request_failed(self, request) -> None:
        try:
            host, _ = ev.split_url(request.url)
            if _is_real_host(host):
                self.failed_hosts.append(host)
        except Exception:  # noqa: BLE001
            pass

    def _on_console(self, message) -> None:
        # Vendors label their own console output — `[SomeSDK] ready` —
        # which makes this a genuinely useful, and often overlooked, layer.
        if len(self.console) >= self.config.max_console_messages:
            return
        try:  # noqa: SIM105 — a listener must never raise into Playwright
            self.console.append({
                "type": message.type,
                "text": ev.redact(message.text, 240),
            })
        except Exception:  # noqa: BLE001
            pass

    def _on_page_error(self, error) -> None:
        if len(self.console) >= self.config.max_console_messages:
            return
        try:  # noqa: SIM105 — a listener must never raise into Playwright
            self.console.append({"type": "pageerror", "text": ev.redact(str(error), 240)})
        except Exception:  # noqa: BLE001
            pass

    # -- async follow-ups -----------------------------------------------------

    async def _read_headers(self, response) -> None:
        try:
            headers = await response.all_headers()
        except PlaywrightError:
            return
        for name in _INTERESTING_HEADERS:
            if name in headers:
                self.document_headers[name] = headers[name][:8000]
        raw_cookies = headers.get("set-cookie", "")
        self.set_cookie_names = _sorted_unique(
            line.split("=")[0].strip() for line in raw_cookies.splitlines() if "=" in line
        )

    async def _read_script(self, response) -> None:
        """Look inside script bodies for hostnames the page has not called yet."""
        try:
            body = await response.body()
        except PlaywrightError:
            return
        if not body or len(body) > self.config.max_js_body_bytes:
            return
        try:
            text = body.decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            return
        for host in ev.extract_hosts_from_text(text, 200):
            self.referenced_hosts.add(host)
        self.identifiers.extend(ev.extract_identifiers(text))

    async def drain(self, timeout: float = 15.0) -> None:
        """Finish outstanding body reads before the page goes away."""
        if not self._body_tasks:
            return
        done, pending = await asyncio.wait(self._body_tasks, timeout=timeout)
        for task in pending:
            task.cancel()


# --- helpers -----------------------------------------------------------------


def _sorted_unique(values) -> list[str]:
    return sorted({v for v in values if v})


def _is_real_host(host: str) -> bool:
    """Reject the malformed hosts third-party scripts occasionally request."""
    return bool(host) and "." in host and not host.endswith(".")


def _dedupe_identifiers(identifiers: list[dict]) -> list[dict]:
    seen, out = set(), []
    for item in identifiers:
        key = (item["kind"], item["value"])
        if key not in seen:
            seen.add(key)
            out.append(item)
    return sorted(out, key=lambda i: (i["kind"], i["value"]))


def _empty_result(url: str) -> dict:
    return {
        "schema_version": ev.SCHEMA_VERSION,
        "scan": {
            "url": url,
            "final_url": None,
            "page_host": None,
            "status": ev.STATUS_OK,
            "http_status": None,
            "title": "",
            "error": None,
            "started_at": None,
            "duration_ms": None,
            "collector_version": COLLECTOR_VERSION,
        },
        "evidence": {},
        "counts": {},
        "warnings": [],
    }


async def collect_url(url: str, config: ScanConfig | None = None) -> dict:
    """Convenience wrapper for a single one-off scan."""
    async with Collector(config) as collector:
        return await collector.scan(url)
