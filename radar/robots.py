"""robots.txt.

A tool that visits other people's sites unattended should read the file
they publish saying who may visit what. This is the opposite of working
around a block: when a site says no, the scan is recorded as skipped and
nothing is inferred from the absence.

That matters twice over here. A `SKIPPED_BY_ROBOTS` scan is not a
trustworthy observation, so it can never look like a vendor was removed —
the same protection a bot wall already gets.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

#: How long a fetched robots.txt is reused within one process.
CACHE_SECONDS = 3600

#: A site that does not answer within this gets the benefit of the doubt —
#: an unreachable robots.txt is not permission denied, it is no answer.
FETCH_TIMEOUT_MS = 8000


@dataclass
class Decision:
    allowed: bool
    reason: str
    crawl_delay: float | None = None


@dataclass
class _Entry:
    parser: RobotFileParser | None
    fetched_at: float
    note: str


@dataclass
class RobotsCache:
    #: Optional long-term memory. Without it the cache lasts one process;
    #: with it, a site whose robots.txt we have read once keeps being
    #: honoured even on a day its server refuses to hand the file over.

    """One cache per collector, keyed by scheme+host.

    Cached rather than refetched per URL because a watchlist typically lists
    several pages of the same site, and asking for the same file five times
    is exactly the impoliteness this module exists to avoid.
    """
    user_agent: str = "*"
    memory: object | None = None
    _entries: dict[str, _Entry] = field(default_factory=dict)

    @staticmethod
    def origin_of(url: str) -> str:
        parts = urlparse(url)
        return f"{parts.scheme}://{parts.netloc}"

    async def _load(self, origin: str, fetch) -> _Entry:
        cached = self._entries.get(origin)
        if cached and (time.monotonic() - cached.fetched_at) < CACHE_SECONDS:
            return cached

        body: str | None = None
        note = "no robots.txt"
        fetched = False
        try:
            status, text = await fetch(f"{origin}/robots.txt")
            if status == 200 and text is not None:
                body, note, fetched = text, "robots.txt honoured", True
            elif status in (401, 403):
                note = f"robots.txt not readable (http {status})"
            else:
                note, fetched = f"no robots.txt (http {status})", True
        except Exception as exc:  # noqa: BLE001 — never let this stop a scan
            note = f"robots.txt unreachable: {type(exc).__name__}"

        if fetched and self.memory is not None:
            self.memory.remember_robots(origin, body, note)
        elif not fetched and self.memory is not None:
            # The fetch failed. If we have read this site's rules before,
            # they are still the site's rules.
            remembered = self.memory.remembered_robots(origin)
            if remembered and remembered["body"]:
                body = remembered["body"]
                note = remembered["note"] + " (remembered)"

        parser: RobotFileParser | None = None
        if body is not None:
            parser = RobotFileParser()
            parser.parse(body.splitlines())

        entry = _Entry(parser=parser, fetched_at=time.monotonic(), note=note)
        self._entries[origin] = entry
        return entry

    async def check(self, url: str, fetch) -> Decision:
        entry = await self._load(self.origin_of(url), fetch)
        if entry.parser is None:
            return Decision(allowed=True, reason=entry.note)

        allowed = entry.parser.can_fetch(self.user_agent, url)
        delay = entry.parser.crawl_delay(self.user_agent)
        try:
            delay = float(delay) if delay is not None else None
        except (TypeError, ValueError):
            delay = None
        # Say *which* rules applied, so a skip made from a file we read last
        # week is distinguishable from one made from a file we read just now.
        remembered = " (remembered)" in entry.note
        reason = entry.note if allowed else (
            "disallowed by robots.txt (remembered)" if remembered
            else "disallowed by robots.txt")
        return Decision(allowed=allowed, reason=reason, crawl_delay=delay)
