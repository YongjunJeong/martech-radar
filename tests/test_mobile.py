"""The mobile: URL profile — one page, two histories."""

from __future__ import annotations

import pytest

from radar import evidence as ev
from radar import security
from radar import targets as tgt
from test_collector import scan


def test_watchlist_accepts_the_mobile_prefix():
    assert tgt._check_url("mobile:https://shop.example/", "t") \
        == "mobile:https://shop.example/"
    with pytest.raises(tgt.TargetError):
        tgt._check_url("mobile:ftp://shop.example/", "t")


def test_security_check_sees_through_the_prefix():
    checked = security.check_scan_target("mobile:https://example.com/")
    assert checked  # validated as the real address, prefix not part of it


def test_split_mobile_round_trip():
    assert ev.split_mobile("mobile:https://x/") == (True, "https://x/")
    assert ev.split_mobile("https://x/") == (False, "https://x/")


def test_mobile_scan_is_served_the_mobile_page(site):
    result = scan(f"mobile:{site}/mobile_probe.html")
    assert result["scan"]["status"] == ev.STATUS_OK
    assert result["scan"]["profile"] == "mobile"
    # The identity keeps the prefix; the page was fetched from the real URL.
    assert result["scan"]["url"].startswith("mobile:")
    assert result["scan"]["final_url"] == f"{site}/mobile_probe.html"
    assert result["scan"]["title"] == "MOBILE-UA"   # UA sniffing saw a phone


def test_desktop_scan_of_the_same_page_stays_desktop(site):
    result = scan(f"{site}/mobile_probe.html")
    assert result["scan"]["profile"] == "desktop"
    assert result["scan"]["title"] == "DESKTOP-UA"


def test_same_host_anchor_paths_are_collected(site):
    result = scan(f"{site}/mobile_probe.html")
    paths = result["evidence"]["dom"]["anchor_paths"]
    assert "/goods/1234" in paths and "/about" in paths
    assert not any("elsewhere" in p for p in paths)   # same-host only
