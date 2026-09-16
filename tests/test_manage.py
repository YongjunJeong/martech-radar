"""The writable dashboard.

Every test here is about a write doing exactly what it says and nothing
more: the watchlist changes, a scan gets queued rather than run, and the
two ways in that a form opens up — someone else's page posting for you, and
a URL box pointed at the network behind the server — stay shut.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from radar import config as cfg
from radar import security
from radar.store import Store
from radar.targets import Target, TargetSet
from web.app import create_app

WATCH = TargetSet(
    targets=(Target(id="acme", company="에이스몰", industry="fashion",
                    urls=("https://acme.example/",)),),
    industries={"fashion": "패션"}, source="test")


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "radar.db"
    with Store(path) as store:
        store.import_watchlist(WATCH)
    return str(path)


@pytest.fixture
def client(db):
    return TestClient(create_app(db))


def form(client, path, **fields):
    return client.post(path, data=fields, follow_redirects=False)


# --- editing the watchlist --------------------------------------------------


def test_a_company_can_be_added_from_the_page(client, db):
    response = form(client, "/manage/save", id="newco", company="새회사",
                    industry="fashion", urls="https://new.example/\nhttps://new.example/x",
                    enabled="1")
    assert response.status_code == 303
    with Store(db) as store:
        row = store.target("newco")
    assert row["company"] == "새회사"
    assert json.loads(row["urls_json"]) == ["https://new.example/",
                                            "https://new.example/x"]


def test_an_edit_updates_rather_than_duplicates(client, db):
    form(client, "/manage/save", id="acme", company="에이스몰 주식회사",
         industry="fashion", urls="https://acme.example/", enabled="1")
    with Store(db) as store:
        assert len(store.targets()) == 1
        assert store.target("acme")["company"] == "에이스몰 주식회사"


def test_pausing_from_the_page_stops_the_next_scan(client, db):
    from radar.batch import jobs_for

    form(client, "/manage/toggle", id="acme")          # no `enabled` field = pause
    with Store(db) as store:
        assert store.target("acme")["enabled"] == 0
        assert jobs_for(store) == []


def test_a_bad_id_is_refused_with_a_message(client, db):
    response = form(client, "/manage/save", id="Not Valid", company="X",
                    industry="fashion", urls="https://x.example/")
    assert response.status_code == 303
    assert "msg=" in response.headers["location"]
    with Store(db) as store:
        assert len(store.targets()) == 1


def test_an_unknown_industry_is_refused(client, db):
    form(client, "/manage/save", id="x", company="X", industry="nope",
         urls="https://x.example/")
    with Store(db) as store:
        assert store.target("x") is None


# --- the robots exception has to carry a reason ----------------------------


def test_an_exception_without_a_reason_is_refused(client, db):
    form(client, "/manage/save", id="acme", company="에이스몰", industry="fashion",
         urls="https://acme.example/", enabled="1", robots_policy="override")
    with Store(db) as store:
        assert store.target("acme")["robots_policy"] == "respect"


def test_an_exception_with_a_reason_is_recorded(client, db):
    form(client, "/manage/save", id="acme", company="에이스몰", industry="fashion",
         urls="https://acme.example/", enabled="1", robots_policy="override",
         robots_note="담당자 서면 동의 2026-08")
    with Store(db) as store:
        row = store.target("acme")
    assert row["robots_policy"] == "override"
    assert "동의" in row["robots_note"]


# --- scanning is queued, never run inline ----------------------------------


def test_scan_queues_work_instead_of_running_it(client, db):
    response = form(client, "/manage/scan", scope="all")
    assert response.status_code == 303
    assert "/runs/" in response.headers["location"]
    with Store(db) as store:
        run_id = int(response.headers["location"].rsplit("/", 1)[1].split("?")[0])
        progress = store.run_progress(run_id)
    assert progress["total"] == 1
    assert progress["pending"] == 1, "the dashboard must not scan in the request"
    assert progress["done"] == 0


def test_the_run_page_shows_the_queue(client, db):
    location = form(client, "/manage/scan", scope="all").headers["location"]
    body = client.get(location.split("?")[0]).text
    assert "acme" in body or "에이스몰" in body
    assert "radar worker" in body, "it should say how to get the work done"


# --- the two ways a form opens things up -----------------------------------


def test_a_url_pointing_at_the_local_network_is_refused(client, db):
    form(client, "/manage/save", id="internal", company="Internal",
         industry="fashion", urls="http://127.0.0.1:8000/admin", enabled="1")
    with Store(db) as store:
        assert store.target("internal") is None, \
            "a URL box that fetches anything is a way into the host's network"


def test_a_cross_origin_write_is_refused(client, db):
    response = client.post("/manage/toggle", data={"id": "acme"},
                           headers={"origin": "https://evil.example"},
                           follow_redirects=False)
    assert response.status_code == 403
    with Store(db) as store:
        assert store.target("acme")["enabled"] == 1


# --- the door ---------------------------------------------------------------


@pytest.fixture
def locked(db, monkeypatch):
    settings = cfg.Config(server=cfg.ServerSettings(token="s3cret"))
    monkeypatch.setattr(cfg, "load", lambda *a, **k: settings)
    return TestClient(create_app(db))


def test_with_a_token_set_every_page_needs_a_session(locked):
    response = locked.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_health_stays_reachable_for_a_monitor(locked):
    assert locked.get("/health").status_code == 200


def test_the_right_token_opens_the_door(locked):
    response = locked.post("/login", data={"token": "s3cret"}, follow_redirects=False)
    assert response.status_code == 303
    assert security.COOKIE_NAME in response.cookies
    assert locked.get("/", follow_redirects=False).status_code == 200


def test_the_wrong_token_does_not(locked):
    response = locked.post("/login", data={"token": "guess"}, follow_redirects=False)
    assert "bad=1" in response.headers["location"]
    assert locked.get("/", follow_redirects=False).status_code == 303


def test_a_write_without_a_session_is_refused(locked, db):
    locked.cookies.clear()
    response = locked.post("/manage/toggle", data={"id": "acme"}, follow_redirects=False)
    assert response.status_code in (303, 401)
    with Store(db) as store:
        assert store.target("acme")["enabled"] == 1


def test_a_fresh_install_can_create_its_first_industry_from_the_page(tmp_path):
    """The new-user walkthrough finding.

    On an empty database the industry dropdown was empty and the field was
    required — so the first company could never be saved from the page.
    """
    empty = str(tmp_path / "empty.db")
    c = TestClient(create_app(empty))
    assert "radar watchlist import" in c.get("/manage").text, "it should say how to get unstuck"

    form(c, "/manage/industry", code="fashion", label="패션", label_en="Fashion")
    form(c, "/manage/save", id="first", company="첫회사", industry="fashion",
         urls="https://first.example/", enabled="1")
    with Store(empty) as store:
        assert store.target("first")["industry"] == "fashion"
