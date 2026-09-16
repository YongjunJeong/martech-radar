"""Dashboard.

The dashboard is where a wrong number turns into a wrong meeting, so these
tests are about meaning, not markup: does a blocked company stay out of the
greenfield list, does a verdict carry its evidence, does a drill-down
actually reach the raw observation.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from radar import detector as det
from radar import evidence as ev
from radar import registry as reg
from radar.store import Store
from radar.targets import Target, TargetSet
from web.app import create_app

BRAZE = {"network": {"hosts": ["sdk.iad-03.braze.com"]},
         "runtime": {"window_keys": ["braze"]}}
BUSY = {
    "network": {"hosts": ["connect.facebook.net", "static.criteo.net",
                          "cdn.taboola.com", "widgets.outbrain.com",
                          "www.googletagmanager.com", "www.google-analytics.com",
                          "static.hotjar.com", "api.amplitude.com"]},
    "runtime": {"window_keys": ["fbq", "criteo_q", "_taboola", "obApi",
                                "google_tag_manager", "GoogleAnalyticsObject",
                                "hjBootstrap", "amplitude"]},
}

WATCHLIST = """
industries:
  fashion: 패션
  commerce: 종합몰
targets:
  - id: holder
    company: 브레이즈몰
    industry: fashion
    urls: [https://holder.test/]
  - id: open
    company: 무보유몰
    industry: fashion
    urls: [https://open.test/]
  - id: walled
    company: 차단몰
    industry: commerce
    urls: [https://walled.test/]
"""


def merge(*parts):
    out = {}
    for part in parts:
        for section, values in part.items():
            for key, items in values.items():
                out.setdefault(section, {}).setdefault(key, []).extend(items)
    return out


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("web")
    targets_file = tmp / "targets.yaml"
    targets_file.write_text(WATCHLIST, encoding="utf-8")
    db = tmp / "radar.db"
    registry = reg.load()

    with Store(db) as store:
        store.import_watchlist(TargetSet(
            targets=(
                Target(id="holder", company="브레이즈몰", industry="fashion",
                       urls=("https://holder.test/",)),
                Target(id="open", company="무보유몰", industry="fashion",
                       urls=("https://open.test/",)),
                Target(id="walled", company="차단몰", industry="commerce",
                       urls=("https://walled.test/",)),
            ),
            industries={"fashion": "패션", "commerce": "종합몰"}, source="test"))
        for run in (1, 2):
            run_id = store.start_run(3)
            for target_id, evidence, status in (
                ("holder", merge(BUSY, BRAZE), ev.STATUS_OK),
                ("open", BUSY, ev.STATUS_OK),
                ("walled", {}, ev.STATUS_BLOCKED),
            ):
                result = {"schema_version": 1,
                          "scan": {"url": f"https://{target_id}.test/",
                                   "page_host": f"{target_id}.test", "status": status,
                                   "started_at": f"2026-08-2{run}T00:00:00+00:00",
                                   "duration_ms": 1000},
                          "evidence": evidence, "counts": {}, "warnings": []}
                scan_id = store.record_scan(run_id, target_id, result)
                store.record_detections(scan_id, det.detect(result, registry))
            store.finish_run(run_id)

    # Korean explicitly: these tests assert Korean chrome, and the default is English.
    return TestClient(create_app(str(db), str(targets_file), language="ko"))


def test_every_page_renders(client):
    for path in ("/", "/signals", "/companies", "/vendors", "/health",
                 "/companies/holder", "/companies/open", "/companies/walled",
                 "/vendors/braze", "/vendors?unseen=1"):
        assert client.get(path).status_code == 200, path


def test_unknown_ids_are_404_not_500(client):
    assert client.get("/companies/nope").status_code == 404
    assert client.get("/vendors/nope").status_code == 404


def test_a_blocked_company_is_not_listed_as_greenfield(client):
    body = client.get("/companies?state=greenfield").text
    assert "무보유몰" in body
    assert "차단몰" not in body, "a bot wall is not an empty stack"


def test_a_blocked_company_appears_under_its_own_filter(client):
    body = client.get("/companies?state=unjudged").text
    assert "차단몰" in body and "브레이즈몰" not in body


def test_a_blocked_company_detail_refuses_to_report_a_stack(client):
    body = client.get("/companies/walled").text
    assert "스택을 보고할 수 없습니다" in body


def test_greenfield_signal_reaches_the_signal_inbox(client):
    body = client.get("/signals").text
    assert "무보유몰" in body
    assert 'href="/companies/open"' in body, "the inbox must link into the company"


def test_a_verdict_carries_its_evidence_into_the_page(client):
    body = client.get("/companies/holder").text
    assert "Braze" in body
    assert "sdk.iad-03.braze.com" in body, "the raw observation must be reachable"
    assert 'href="/vendors/braze"' in body


def test_company_detail_shows_the_run_timeline(client):
    body = client.get("/companies/holder").text
    assert "타임라인" in body
    # The label is translated; the run numbers are not.
    assert "실행 1" in body and "실행 2" in body


def test_a_skipped_run_is_marked_in_the_timeline(client):
    body = client.get("/companies/walled").text
    assert "이력에서 제외" in body


def test_vendor_page_lists_its_users_and_its_patterns(client):
    body = client.get("/vendors/braze").text
    assert "브레이즈몰" in body
    assert "appboycdn.com" in body, "the fingerprint itself should be inspectable"


def test_vendor_share_counts_only_judged_companies(client):
    # Two judged companies, one running Braze -> 50%, not 33%.
    body = client.get("/vendors?category=engagement").text
    assert "50%" in body


def test_unseen_fingerprints_are_hidden_by_default_and_visible_on_request(client):
    default = client.get("/vendors?category=engagement").text
    assert "MoEngage" not in default
    assert "MoEngage" in client.get("/vendors?category=engagement&unseen=1").text


def test_industry_overview_separates_the_three_buckets(client):
    body = client.get("/").text
    assert "engagement 보유" in body and "미보유 (greenfield)" in body and "판정 불가" in body


def test_engagement_is_the_first_category_shown(client):
    """Jinja's groupby re-sorts alphabetically and buried engagement under
    advertising. The commercially decisive category leads."""
    import re
    body = client.get("/companies/holder").text
    cats = re.findall(r'<h3 class="cat">([^<]+)</h3>', body)
    assert cats and cats[0] == "engagement", cats
    vendors = re.findall(r'<h2 class="cat">([^<]+)</h2>', client.get("/vendors").text)
    assert vendors and vendors[0] == "engagement", vendors


def test_the_dashboard_renders_in_english_with_no_korean_chrome(tmp_path_factory, client):
    """The whole point of the catalogue, checked end to end.

    Only the fixture watchlist's own company names may be Korean — those are
    data an operator wrote, not chrome.
    """
    import re
    from web.app import create_app

    english = TestClient(create_app(client.app.state.db_path,
                                    language="en"))
    hangul = re.compile(r"[가-힣]")
    for path in ("/", "/signals", "/companies", "/vendors", "/companies/holder"):
        body = english.get(path).text
        # Strip the company names the fixture supplies as data.
        for name in ("브레이즈몰", "무보유몰", "차단몰", "패션", "종합몰"):
            body = body.replace(name, "")
        assert not hangul.search(body), f"{path} still shows Korean chrome"


def test_the_dashboard_still_renders_in_korean(client):
    import re
    assert re.search(r"[가-힣]", client.get("/").text)


def test_operations_page_lists_scan_scopes_and_queues_a_task(client):
    body = client.get("/ops").text
    assert "운영" in body and "/ops/scan" in body and 'name="kind" value="digest"' in body
    response = client.post("/ops/task", data={"kind": "hygiene"},
                           headers={"origin": "http://testserver"}, follow_redirects=False)
    assert response.status_code == 303 and "/ops?msg=" in response.headers["location"]
    # The row exists and is readable before any worker touches it.
    listing = client.get("/ops").text
    assert "/ops/tasks/1" in listing
    detail = client.get("/ops/tasks/1")
    assert detail.status_code == 200 and "대기" in detail.text
    assert client.get("/api/tasks/1").json()["state"] == "pending"
    assert client.get("/ops/tasks/999").status_code == 404
    assert client.post("/ops/task", data={"kind": "rm_rf"},
                       headers={"origin": "http://testserver"}).status_code == 400


def test_operations_scan_scope_queues_a_run(client):
    response = client.post("/ops/scan", data={"scope": "all"},
                           headers={"origin": "http://testserver"}, follow_redirects=False)
    assert response.status_code == 303 and "/runs/" in response.headers["location"]
    assert client.post("/ops/scan", data={"scope": "tier:nope"},
                       headers={"origin": "http://testserver"},
                       follow_redirects=False).headers["location"].startswith("/ops?msg=")


def test_a_company_brief_is_one_click_away(client):
    body = client.get("/companies/holder").text
    assert "/companies/holder/brief" in body
    brief = client.get("/companies/holder/brief")
    assert brief.status_code == 200 and "Braze" in brief.text and "greenfield" not in brief.text
    walled = client.get("/companies/walled/brief").text
    assert "콜드메일" in walled                    # refused, not blank
    assert client.get("/companies/nobody/brief").status_code == 404
