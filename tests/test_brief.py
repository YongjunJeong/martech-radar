"""Evidence-backed briefs.

Check that observed facts appear in the brief, unsupported claims stay out,
and an unobservable company produces a refusal rather than a blank.
"""

from __future__ import annotations

import pytest

from radar import brief as brf
from radar import detector as det
from radar import evidence as ev
from radar import registry as reg
from radar.store import Store
from radar.targets import Target, TargetSet

BRAZE = {"network": {"hosts": ["sdk.iad-03.braze.com"]},
         "runtime": {"window_keys": ["braze"]}}
BUSY = {
    "network": {"hosts": ["connect.facebook.net", "static.criteo.net",
                          "cdn.taboola.com", "www.googletagmanager.com",
                          "www.google-analytics.com", "api.amplitude.com"]},
    "runtime": {"window_keys": ["fbq", "criteo_q", "_taboola",
                                "google_tag_manager", "GoogleAnalyticsObject", "amplitude"]},
}


def brief(store, target_id: str) -> str:
    """The text half, in Korean — these tests assert Korean strings.

    Asked for explicitly rather than assumed: the code's default language is
    neutral English, and a deployment chooses its own in radar.toml.
    """
    return brf.company_brief(store, target_id, language="ko")[0]


def merge(*parts):
    out = {}
    for part in parts:
        for section, values in part.items():
            for key, items in values.items():
                out.setdefault(section, {}).setdefault(key, []).extend(items)
    return out


@pytest.fixture(scope="module")
def registry():
    return reg.load()


@pytest.fixture
def store(tmp_path, registry):
    with Store(tmp_path / "radar.db") as s:
        s.import_watchlist(TargetSet(
            targets=(
                Target(id="open", company="무보유몰", company_en="OpenMall",
                       industry="fashion", urls=("https://open.test/",)),
                Target(id="held", company="보유몰", industry="beauty",
                       urls=("https://held.test/",)),
                Target(id="walled", company="차단몰", industry="commerce",
                       urls=("https://walled.test/",)),
            ),
            industries={"fashion": "패션", "beauty": "뷰티", "commerce": "종합몰"},
            source="test"))
        for _ in range(2):
            run = s.start_run(3)
            for target_id, evidence, status in (
                ("open", BUSY, ev.STATUS_OK),
                ("held", merge(BUSY, BRAZE), ev.STATUS_OK),
                ("walled", {}, ev.STATUS_BLOCKED),
            ):
                result = {"schema_version": 1,
                          "scan": {"url": f"https://{target_id}.test/", "status": status,
                                   "page_host": f"{target_id}.test",
                                   "started_at": "2026-08-22T00:00:00+00:00"},
                          "evidence": evidence, "counts": {}, "warnings": []}
                scan_id = s.record_scan(run, target_id, result)
                s.record_detections(scan_id, det.detect(result, registry))
        yield s


def test_a_greenfield_brief_states_the_absence_and_the_counts(store):
    text = brief(store, "open")
    assert "무보유몰 (OpenMall)" in text
    assert "greenfield" in text
    assert "광고 픽셀 3종" in text
    assert "A/B 테스트 도구 미검출" in text


def test_the_brief_names_the_incumbent_when_there_is_one(store):
    text = brief(store, "held")
    assert "Braze" in text
    assert "greenfield" not in text


def test_an_unobservable_company_gets_a_refusal_not_a_blank_brief(store):
    text, usable = brf.company_brief(store, "walled", language="ko")
    assert usable is False, "callers need a machine-readable stop, not just prose"
    assert "정상 스캔된 적이 없습니다" in text
    assert "콜드메일을 작성하지 마십시오" in text
    assert "BLOCKED" in text
    # And crucially, no stack is offered for it to draw on.
    assert "관측된 스택" not in text


def test_unknown_id_is_reported_not_crashed(store):
    text, usable = brf.company_brief(store, "nope", language="ko")
    assert "알 수 없는 기업" in text
    assert usable is False


def test_a_usable_brief_says_so(store):
    _text, usable = brf.company_brief(store, "open", language="ko")
    assert usable is True


def test_the_digest_separates_change_from_state(store):
    text = brf.weekly_digest(store, language="ko")
    assert "이번 주 변화" in text
    assert "공략 가능" in text
    assert "판정 불가 — greenfield 아님" in text
    assert "walled" in text


def test_digest_stats_agree_with_the_signal_list(store):
    stats = brf.digest_stats(store)
    assert stats["greenfield"] >= 1
    assert stats["unjudged"] == 1
    assert stats["signals"] == stats["changes"] + stats["greenfield"]


def test_the_brief_is_only_observed_facts(store):
    """No case studies, no vendor material — just what we saw."""
    text = brief(store, "open")
    assert "메일에 인용 가능한 관측 사실" in text
    assert "이 목록에 없는 것은 메일에 쓰지 마십시오" in text
    # Nothing in a brief may point at an external knowledge source.
    assert "search" not in text.lower() and "knowledge" not in text.lower()


def _seed_minimal(tmp_path, evidence, statuses=("OK",)):
    s = Store(tmp_path / "radar.db")
    s.import_watchlist(TargetSet(
        targets=(Target(id="acme", company="에이스몰", industry="fashion",
                        urls=("https://acme.test/",)),),
        industries={"fashion": "패션"}, source="test"))
    registry = reg.load()
    for i, status in enumerate(statuses):
        run = s.start_run(1)
        result = {"schema_version": 1,
                  "scan": {"url": "https://acme.test/", "page_host": "acme.test",
                           "status": status,
                           "started_at": f"2026-09-{i+1:02d}T00:00:00+00:00"},
                  "evidence": evidence, "counts": {}, "warnings": []}
        scan_id = s.record_scan(run, "acme", result)
        s.record_detections(scan_id, det.detect(result, registry))
    return s


def test_brief_lists_observed_account_identifiers(tmp_path):
    evidence = dict(BUSY, identifiers=[
        {"kind": "gtm_container", "value": "GTM-AAA"},
        {"kind": "insider_partner", "value": "acme"}])
    with _seed_minimal(tmp_path, evidence) as s:
        text, usable = brf.company_brief(s, "acme")
    assert usable
    assert "insider_partner: `acme`" in text and "GTM-AAA" in text


def test_digest_reports_repeatedly_unreachable_targets(tmp_path):
    with _seed_minimal(tmp_path, BUSY,
                       statuses=("OK", "NAV_FAILED", "NAV_FAILED", "NAV_FAILED")) as s:
        text = brf.weekly_digest(s)
    assert "acme" in text and "NAV_FAILED x3" in text


def test_a_platform_lost_recently_is_not_called_greenfield(store, registry):
    """Two runs saw Braze on 보유몰; a third does not. That company is a
    removal candidate, and the brief must say so — "absent in all judged
    runs" would be a lie the email then repeats to the prospect."""
    run = store.start_run(1)
    result = {"schema_version": 1,
              "scan": {"url": "https://held.test/", "status": ev.STATUS_OK,
                       "page_host": "held.test",
                       "started_at": "2026-09-15T00:00:00+00:00"},
              "evidence": BUSY, "counts": {}, "warnings": []}
    scan_id = store.record_scan(run, "held", result)
    store.record_detections(scan_id, det.detect(result, registry))

    text = brief(store, "held")
    assert "greenfield" not in text
    assert "모두에서 미검출" not in text
    assert "현재 미검출" in text and "Braze" in text
    assert "이탈 후보이며 확정 아님" in text


def test_a_brief_refuses_when_the_latest_look_landed_on_someone_elses_domain(store, registry):
    """A watched URL redirected to another company's homepage one week. Nothing on
    that page is a fact about the original company, so there is no brief to write."""
    run = store.start_run(1)
    result = {"schema_version": 1,
              "scan": {"url": "https://held.test/", "status": ev.STATUS_OK,
                       "page_host": "nol.somebody-else.test",
                       "started_at": "2026-09-15T00:00:00+00:00"},
              "evidence": BUSY, "counts": {}, "warnings": []}
    scan_id = store.record_scan(run, "held", result)
    store.record_detections(scan_id, det.detect(result, registry))

    text, usable = brf.company_brief(store, "held", language="ko")
    assert usable is False
    assert "somebody-else.test" in text and "콜드메일" in text
    assert "광고 픽셀" not in text                      # nothing from that page is cited


def test_after_a_brand_domain_move_the_brief_counts_only_the_new_site(store, registry):
    """open.test moved to open-shop.test: same brand, new site. The brief
    says so, and "absent in all N judged runs" counts runs on the new site."""
    run = store.start_run(1)
    result = {"schema_version": 1,
              "scan": {"url": "https://open.test/", "status": ev.STATUS_OK,
                       "page_host": "www.open-shop.test",
                       "started_at": "2026-09-15T00:00:00+00:00"},
              "evidence": BUSY, "counts": {}, "warnings": []}
    scan_id = store.record_scan(run, "open", result)
    store.record_detections(scan_id, det.detect(result, registry))
    text, usable = brf.company_brief(store, "open", language="ko")
    assert usable is True
    assert "착지 변경" in text and "open.test → open-shop.test" in text
    assert "판정 가능한 1회 실행 모두에서 미검출" in text
