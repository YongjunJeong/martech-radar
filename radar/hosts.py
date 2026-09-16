"""What counts as "the same site" when a landing host changes.

A watched URL may redirect: travel.travel.example landed on
nol.travel.example for months and then, one week, on nol.booking.example. The
first is the same company on another subdomain; the second is a different
company's homepage. Vendor history across that second kind of change is
not a change in what the company runs — it is a change in whose site we
looked at — and the signals must not pretend otherwise.

The registrable domain comes from `evidence.base_domain` — the same suffix
knowledge that groups a site's own requests — so there is one place that
knows co.kr from com.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit

from . import evidence as ev

def host_of(url_or_host: str) -> str:
    text = (url_or_host or "").strip()
    if "://" in text:
        return (urlsplit(text).hostname or "").lower()
    return text.lower().split("/")[0].split(":")[0]


def registrable(url_or_host: str) -> str:
    """`nol.travel.example` → `travel.example`; `shop.foo.co.kr` → `foo.co.kr`."""
    host = host_of(url_or_host).strip(".")
    if host.startswith("www."):
        host = host[4:]
    return ev.base_domain(host)


def sites(hosts: Iterable[str]) -> frozenset[str]:
    return frozenset(registrable(h) for h in hosts if h)


def brand(site: str) -> str:
    """The part of a registrable domain that names the company:
    `sample-cola.com` → `samplecola`, `sample-cafe.co.kr` → `samplecafe`."""
    labels = registrable(site).split(".")
    return labels[0].replace("-", "").replace("_", "") if labels else ""


def same_brand(a: str, b: str) -> bool:
    """samplebrand.co.kr and samplebrand.com are one company that moved; so are
    samplebrandusa.com and samplebrand.co.kr. One brand label containing the other
    (four letters or more) is the line — short enough to catch renames,
    long enough that samplefresh and samplemarket stay different."""
    x, y = brand(a), brand(b)
    if not x or not y:
        return False
    if x == y:
        return True
    shorter, longer = sorted((x, y), key=len)
    return len(shorter) >= 4 and shorter in longer


def off_domain(target_urls: Iterable[str], landing_hosts: Iterable[str]) -> bool:
    """True when the pages we judged belong to none of the watched domains,
    by registrable domain or by brand label.

    Empty on either side means "cannot say", which is False here on purpose:
    an absence of hosts must not read as a wrong host.
    """
    watched, landed = sites(target_urls), sites(landing_hosts)
    if not watched or not landed:
        return False
    return not any(same_brand(w, site) for w in watched for site in landed)


__all__ = ["host_of", "registrable", "sites", "brand", "same_brand", "off_domain"]
