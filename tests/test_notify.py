"""Webhook notifications: once per event, and never fatal to a batch."""

from __future__ import annotations

import pytest

from radar import notify
from radar import config as cfg
from radar.store import Store
from radar.targets import Target, TargetSet


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
def store_with_new_vendor(tmp_path):
    """Two judged runs; braze appears in the second -> one COMPETITOR_NEW."""
    with Store(tmp_path / "radar.db") as s:
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
        yield s


def test_each_signal_is_sent_exactly_once(store_with_new_vendor):
    s = store_with_new_vendor
    sent = []
    first = notify.notify_new_signals(s, webhook="https://hook.example/x",
                                      transport=lambda url, p: sent.append(p))
    assert first["sent"] == first["pending"] == 1
    assert "가나다몰" in sent[0]["text"] and "COMPETITOR_NEW" in sent[0]["text"]
    again = notify.notify_new_signals(s, webhook="https://hook.example/x",
                                      transport=lambda url, p: sent.append(p))
    assert again == {"pending": 0, "sent": 0} and len(sent) == 1


def test_dry_run_sends_and_records_nothing(store_with_new_vendor):
    s = store_with_new_vendor
    report = notify.notify_new_signals(s, webhook="https://hook.example/x",
                                       dry_run=True,
                                       transport=lambda url, p: pytest.fail("sent"))
    assert report == {"pending": 1, "sent": 0}
    assert s.notified_keys() == set()


def test_failed_delivery_stays_pending(store_with_new_vendor):
    s = store_with_new_vendor

    def broken(url, payload):
        raise OSError("connection refused")

    report = notify.notify_new_signals(s, webhook="https://hook.example/x",
                                       transport=broken)
    assert report["sent"] == 0 and "connection refused" in report["error"]
    assert s.notified_keys() == set()          # retried on the next batch
    sent = []
    retry = notify.notify_new_signals(s, webhook="https://hook.example/x",
                                      transport=lambda url, p: sent.append(p))
    assert retry["sent"] == 1 and len(sent) == 1


def test_no_webhook_is_a_reported_noop(store_with_new_vendor):
    report = notify.notify_new_signals(store_with_new_vendor, webhook="")
    assert report["sent"] == 0 and report["error"] == "no webhook configured"
    assert store_with_new_vendor.notified_keys() == set()


def test_notify_config_rejects_a_bad_format(tmp_path):
    path = tmp_path / "radar.toml"
    path.write_text('[notify]\nformat = "carrier-pigeon"\n')
    with pytest.raises(cfg.ConfigError):
        cfg.load(path)
