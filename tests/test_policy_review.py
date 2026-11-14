from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

with pytest.MonkeyPatch.context() as imports:
    imports.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    from deploy import review_policy_references as review
    from deploy import review_collector_followup as followup


def row(hour=20, state="completed", status="UNAVAILABLE", load=True):
    at = datetime(2026, 11, 9, hour, 45, tzinfo=review.ZONE)
    payload = {
        "state": state,
        "results": [{"status": status, "request_count": 3, "issues": ["robots_disallowed"]}],
        "duration_ms": 2000,
    }
    if load:
        payload["process_load"] = {"cpu_seconds": 0.2, "peak_rss_bytes": 30 * 1024**2}
    return {"MESSAGE": json.dumps(payload), "__REALTIME_TIMESTAMP": str(int(at.timestamp() * 1e6))}


def test_successful_execution_is_not_document_success_and_night_is_not_expected():
    result = review.summarize([row(22)], datetime(2026, 11, 10, 4, tzinfo=review.ZONE))
    assert result["findings"] == []
    assert result["automatic_document_statuses"] == {"UNAVAILABLE": 1}
    assert result["automatic_document_issues"] == {"robots_disallowed": 1}
    assert result["process_cpu_seconds_total"] == 0.2


def test_missing_load_night_visits_and_service_failure_are_visible():
    result = review.summarize([row(3, load=False), {"MESSAGE": "Failed with result 'oom-kill'."}])
    assert set(result["findings"]) == {
        "policy_visits_outside_daytime",
        "policy_load_samples_missing",
        "policy_service_failure_logged",
    }
    assert result["process_peak_rss_bytes"] is None
    assert "policy_execution_records_missing" in review.summarize([])["findings"]


def test_gaps_and_stopped_daytime_execution_are_not_healthy():
    result = review.summarize([row(16), row(18)], datetime(2026, 11, 10, 4, tzinfo=review.ZONE))
    assert "policy_daytime_execution_gap" in result["findings"]
    assert "policy_last_daytime_execution_missing" in result["findings"]


def test_future_check_does_not_touch_db_or_system(tmp_path, monkeypatch):
    monkeypatch.setattr(review.subprocess, "run", lambda *a, **k: pytest.fail("future system read"))
    now = datetime(2026, 11, 9, 16, tzinfo=review.ZONE)
    assert review.review(tmp_path, "2026-11-10", now)["state"] == "waiting_for_date"
    tomorrow = (datetime.now(review.ZONE) + timedelta(days=1)).date().isoformat()
    assert (
        followup.run(
            tmp_path,
            tomorrow,
            tmp_path / "out",
            tmp_path / "backups",
            tmp_path / "absent.json",
            include_policy=True,
        )["state"]
        == "waiting_for_date"
    )
    assert not list(tmp_path.iterdir())


def test_review_reads_saved_state_and_does_not_create_collection(tmp_path, monkeypatch):
    path = tmp_path / "data/radar.db"
    path.parent.mkdir()
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE target(id TEXT,enabled INTEGER); CREATE TABLE policy_reference(target_id TEXT,payload_json TEXT);"
        )
        db.executemany("INSERT INTO target VALUES (?,1)", [("a",), ("b",)])
        db.execute(
            "INSERT INTO policy_reference VALUES (?,?)",
            ("a", json.dumps({"status": "UNAVAILABLE", "issues": ["robots_disallowed"]})),
        )

    def run(args, **kwargs):
        if args[0] == "journalctl":
            return SimpleNamespace(stdout=json.dumps(row(22)))
        if args[3].endswith(".timer"):
            return SimpleNamespace(stdout="ActiveState=active\nUnitFileState=enabled")
        return SimpleNamespace(stdout="ActiveState=inactive\nResult=success\nMemoryMax=201326592")

    monkeypatch.setattr(review.subprocess, "run", run)
    before = path.read_bytes()
    result = review.review(tmp_path, "2026-11-10", datetime(2026, 11, 10, 4, tzinfo=review.ZONE))
    assert result["findings"] == []
    assert result["stored_references"]["not_yet_attempted"] == 1
    assert result["stored_references"]["latest_statuses"] == {"UNAVAILABLE": 1}
    assert path.read_bytes() == before


def test_policy_check_still_runs_when_main_collection_is_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(
        followup.review_rollout, "run", lambda *a: {"state": "incomplete_at_deadline"}
    )
    monkeypatch.setattr(
        review, "review", lambda *a: {"findings": ["policy_timer_not_enabled_active"]}
    )
    out = tmp_path / "out"
    out.mkdir()
    result = followup.run(
        tmp_path,
        "2026-11-10",
        out,
        tmp_path / "backups",
        tmp_path / "baseline.json",
        include_policy=True,
    )
    assert result["state"] == "incomplete_at_deadline"
    assert json.loads(Path(result["policy_review"]).read_text())["findings"] == [
        "policy_timer_not_enabled_active"
    ]


def test_isolated_collector_error_is_a_finding_even_when_service_exits_normally():
    result = review.summarize([row(22, state='completed_with_errors', status='ERROR')])
    assert 'policy_collector_errors' in result['findings']
    assert result['automatic_document_statuses']=={'ERROR':1}
