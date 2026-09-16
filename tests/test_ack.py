"""Signal verdicts: sticky across recomputes, cleared on request."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from radar import notify
from radar import signals as sig
from radar.store import Store
from radar.targets import Target, TargetSet
from web.app import create_app


def result(url: str) -> dict:
    return {
        "schema_version": 1,
        "scan": {"url": url, "final_url": url, "page_host": "x", "status": "OK",
                 "http_status": 200, "title": "t",
                 "started_at": "2026-09-04T00:00:00+00:00", "duration_ms": 5},
        "evidence": {}, "counts": {}, "warnings": [],
    }


def detection(fp: str) -> dict:
    return {"id": fp, "name": fp.title(), "category": "engagement",
            "verdict": "DETECTED", "score": 90, "direct_layers": ["network"],
            "indirect_layers": [], "indirect_only": False, "match_count": 1,
            "matches": []}


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "radar.db"
    with Store(path) as s:
        s.import_watchlist(TargetSet(
            targets=(Target(id="shop", company="가나다몰", industry="fashion",
                            urls=("https://shop.example/",)),),
            industries={"fashion": "패션"}, source="test"))
        run = s.start_run(1)
        scan = s.record_scan(run, "shop", result("https://shop.example/"), False)
        s.record_detections(scan, {"detections": []})
        run = s.start_run(1)
        scan = s.record_scan(run, "shop", result("https://shop.example/"), False)
        s.record_detections(scan, {"detections": [detection("braze")]})
    return path


def signal_and_key(path):
    with Store(path) as s:
        signal = sig.all_signals(s)[0]
        return signal, notify.signal_key(signal)


def test_ack_round_trip_and_clear(db):
    _, key = signal_and_key(db)
    with Store(db) as s:
        s.set_signal_ack(key, "ACKED")
        assert s.signal_acks()[key]["state"] == "ACKED"
        s.set_signal_ack(key, "DISMISSED")
        assert s.signal_acks()[key]["state"] == "DISMISSED"
        s.set_signal_ack(key, None)
        assert s.signal_acks() == {}
        with pytest.raises(ValueError):
            s.set_signal_ack(key, "MAYBE")


def test_dismissed_signals_leave_the_default_view(db):
    _, key = signal_and_key(db)
    client = TestClient(create_app(db_path=str(db)))
    assert "가나다몰" in client.get("/signals").text

    response = client.post("/signals/ack",
                           data={"key": key, "state": "DISMISSED"},
                           follow_redirects=False)
    assert response.status_code == 303

    page = client.get("/signals").text
    assert "가나다몰" not in page and "dismissed=1" in page      # hidden, linked
    assert "가나다몰" in client.get("/signals?dismissed=1").text  # still reachable

    client.post("/signals/ack", data={"key": key, "state": ""},
                follow_redirects=False)
    assert "가나다몰" in client.get("/signals").text              # back to open


def test_bad_state_is_refused(db):
    client = TestClient(create_app(db_path=str(db)))
    response = client.post("/signals/ack", data={"key": "k", "state": "MAYBE"},
                           follow_redirects=False)
    assert response.status_code == 400
