"""The call list: ordered, reasoned, and only from observation."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from radar import calls
from radar import detector as det
from radar import evidence as ev
from radar import registry as reg
from radar import signals as sig
from radar.store import Store
from radar.targets import Target, TargetSet

BRAZE = {"network": {"hosts": ["sdk.iad-03.braze.com"]}, "runtime": {"window_keys": ["braze"]}}
BUSY = {
    "network": {"hosts": ["connect.facebook.net", "static.criteo.net", "cdn.taboola.com",
                          "widgets.outbrain.com", "www.googletagmanager.com",
                          "www.google-analytics.com", "static.hotjar.com", "api.amplitude.com"]},
    "runtime": {"window_keys": ["fbq", "criteo_q", "_taboola", "obApi", "google_tag_manager",
                                "GoogleAnalyticsObject", "hjBootstrap", "amplitude"]},
}
THIN_BUSY = {"network": {"hosts": ["www.googletagmanager.com", "connect.facebook.net"]},
             "runtime": {"window_keys": ["google_tag_manager", "fbq"]}}


@pytest.fixture(scope="module")
def registry():
    return reg.load()


def merge(*parts):
    out = {}
    for part in parts:
        for section, values in part.items():
            for key, items in values.items():
                out.setdefault(section, {}).setdefault(key, []).extend(items)
    return out


@pytest.fixture
def store(tmp_path, registry):
    with Store(tmp_path / "radar.db") as s:
        s.import_watchlist(TargetSet(
            targets=(Target(id="leaver", company="떠나는몰", industry="fashion", urls=("https://leaver.test/",)),
                     Target(id="open", company="무보유몰", industry="fashion", urls=("https://open.test/",)),
                     Target(id="small", company="작은몰", industry="fashion", urls=("https://small.test/",)),
                     Target(id="paused", company="중지몰", industry="fashion", urls=("https://paused.test/",),
                            enabled=False)),
            industries={"fashion": "패션"}, source="test"))
        for run, week in ((1, 0), (2, 1), (3, 2)):
            run_id = s.start_run(4)
            when = (date(2026, 8, 20) + timedelta(days=7 * week)).isoformat() + "T00:00:00+00:00"
            for target, evidence in (("leaver", merge(BUSY, BRAZE) if run < 3 else BUSY),
                                     ("open", BUSY), ("small", THIN_BUSY), ("paused", BUSY)):
                result = {"schema_version": 1,
                          "scan": {"url": f"https://{target}.test/", "status": ev.STATUS_OK,
                                   "page_host": f"{target}.test", "started_at": when},
                          "evidence": evidence, "counts": {}, "warnings": []}
                scan_id = s.record_scan(run_id, target, result)
                s.record_detections(scan_id, det.detect(result, registry))
        yield s


def test_tiers_are_ordered_and_reasoned(store):
    tiers = calls.call_list(store, language="ko")
    names = [t["tier"] for t in tiers]
    assert names == ["leaving"]
    leaver = tiers[0]["entries"][0]
    assert leaver["company"] == "떠나는몰" and "Braze" in leaver["reason"]
    assert any("재확인 가능 시점" in line for line in leaver["evidence"])
    assert leaver["confirm_at"] is not None
    assert any("Braze" in f and "확정 아님" in f for f in leaver["facts"])


def test_disabled_dismissed_and_thin_accounts_stay_off_the_list(store):
    tiers = calls.call_list(store, language="ko")
    listed = {e["company"] for t in tiers for e in t["entries"]}
    assert "중지몰" not in listed and "작은몰" not in listed
    leaver = tiers[0]["entries"][0]
    store.set_signal_ack(leaver["key"], "DISMISSED")
    tiers = calls.call_list(store, language="ko")
    assert all(e["company"] != "떠나는몰" for t in tiers for e in t["entries"])


def test_the_rendered_list_reads_top_down(store):
    text = calls.render(calls.call_list(store, language="ko"), "ko")
    assert "미검출 변화" in text and "경쟁사" not in text and "미보유" not in text
    assert "1. **떠나는몰**" in text and "인용 가능한 사실:" in text


def test_a_sunset_ab_tool_is_called_a_leftover(store, registry):
    from radar import brief as brf
    from radar.strings import translator
    t = translator("ko")
    stack = [{"name": "Google Optimize (sunset)", "category": "experimentation", "indirect_only": 0}]
    maturity = sig.Maturity(total=1, analytics=0, advertising=0, attribution=0, experimentation=1)
    facts = brf.citable_facts(t, stack, [], maturity, 3)
    assert any("잔존 스크립트" in f for f in facts)
    assert not any("실험 문화" in f for f in facts)


def test_two_competitors_arriving_at_one_company_is_one_call(store, registry):
    run_id = store.start_run(1)
    both = merge(BUSY, BRAZE, {"network": {"hosts": ["cdn.useinsider.com"]},
                               "runtime": {"window_keys": ["Insider"]}})
    result = {"schema_version": 1,
              "scan": {"url": "https://open.test/", "status": ev.STATUS_OK,
                       "page_host": "open.test", "started_at": "2026-09-15T00:00:00+00:00"},
              "evidence": both, "counts": {}, "warnings": []}
    scan_id = store.record_scan(run_id, "open", result)
    store.record_detections(scan_id, det.detect(result, registry))
    tiers = {t["tier"]: t for t in calls.call_list(store, language="ko")}
    arrivals = [e for e in tiers["arrivals"]["entries"] if e["target_id"] == "open"]
    assert len(arrivals) == 1
    assert "Braze" in arrivals[0]["reason"] and "Insider" in arrivals[0]["reason"]
