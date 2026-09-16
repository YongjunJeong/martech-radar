"""Login and target-address checks for a dashboard that is not on localhost.

Deliberately small. This is a single-operator internal tool, so it needs a
door with a lock, not an identity system — and pretending otherwise would
invite someone to trust it with more than it can carry.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import socket
from urllib.parse import urlparse

COOKIE_NAME = "radar_session"


def session_value(token: str) -> str:
    """A cookie value that proves knowledge of the token without carrying it."""
    return hashlib.sha256(f"radar:{token}".encode()).hexdigest()


def session_is_valid(cookie: str | None, token: str) -> bool:
    if not cookie:
        return False
    return hmac.compare_digest(cookie, session_value(token))


def token_matches(supplied: str, token: str) -> bool:
    return hmac.compare_digest(supplied.strip(), token.strip())


def suggest_token() -> str:
    return secrets.token_urlsafe(24)


class UnsafeTarget(ValueError):
    """Raised for a URL a shared dashboard must not be told to fetch."""


def check_scan_target(url: str, allow_private: bool = False) -> str:
    if url.startswith("mobile:"):      # profile prefix, not part of the address
        url = url[len("mobile:"):]
    """Validate a URL typed into the dashboard before anything visits it.

    A box that takes a URL and makes the server fetch it is a way into
    whatever network the server sits on. On a laptop that does not matter;
    on a shared host it is the first thing anyone would try.

    A host that does not resolve is allowed through. It is not a route into
    anything — the scan will simply fail as unreachable, which the history
    already records honestly — and refusing it would mean a watchlist you
    cannot edit while DNS is having a bad afternoon.

    This checks the address at the time of saving. A name that resolves
    publicly now and privately later is not caught here; the defence against
    that is network-level, not a string check.
    """
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise UnsafeTarget("only http and https URLs can be scanned")
    if not parsed.hostname:
        raise UnsafeTarget("that URL has no host")
    if allow_private:
        return url.strip()

    host = parsed.hostname
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return url.strip()

    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (address.is_private or address.is_loopback or address.is_link_local
                or address.is_reserved or address.is_multicast):
            raise UnsafeTarget(
                f"{host} resolves to {address}, which is not a public address")
    return url.strip()
