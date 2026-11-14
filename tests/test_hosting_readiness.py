import importlib.util
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from radar import config, recovery
from radar.batch import Job
from radar.store import Store
from web.app import create_app

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deploy" / f"{name}.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def source(home):
    module("init_workspace").initialize(
        home, mode="hosting", public_origin="https://radar.example.test"
    )
    with Store(home / "data/radar.db") as store:
        store.upsert_industry("retail", "Retail")
        store.upsert_target(
            id="demo", company="Demo", industry="retail", urls=["https://example.com"]
        )
    return home


def test_verified_backup_restore_preserves_drafts_and_never_activates(tmp_path):
    from web.preparation import Drafts, empty_document

    home = source(tmp_path / "home")
    drafts = Drafts(home / "data/radar.db")
    drafts.initialize()
    drafts.save("demo", 0, empty_document())
    (home / ".collection-enabled").write_text("collection-enabled-v1")
    (home / "fingerprints").mkdir()
    (home / "fingerprints/custom.yaml").write_text("custom: fixture\n")
    snap = recovery.backup(home, tmp_path / "backups")
    assert recovery.verify(snap)["database"]["counts"]["preparation_draft"] == 1
    restored = tmp_path / "restored"
    state = recovery.restore(snap, restored, "hosting")
    assert state["counts"]["target"] == 1
    assert (restored / "fingerprints/custom.yaml").read_text() == "custom: fixture\n"
    assert not (restored / ".collection-enabled").exists()
    assert (restored / ".radar-hosting").read_text().strip() == "hosting-v1"
    assert (restored / "radar.toml").stat().st_mode & 0o777 == 0o600
    assert Drafts(restored / "data/radar.db").get("demo")["revision"] == 1
    with pytest.raises(FileExistsError):
        recovery.restore(snap, restored)


def test_backup_tampering_cannot_be_restored(tmp_path):
    snap = recovery.backup(source(tmp_path / "home"), tmp_path / "backups")
    (snap / "radar.toml").write_text("tampered")
    with pytest.raises(ValueError, match="checksum"):
        recovery.restore(snap, tmp_path / "destination")
    assert not (tmp_path / "destination").exists()


def test_backup_manifest_cannot_escape_directory(tmp_path):
    snap = recovery.backup(source(tmp_path / "home"), tmp_path / "backups")
    manifest = json.loads((snap / "manifest.json").read_text())
    manifest["files"]["../private"] = "x"
    (snap / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Unsafe"):
        recovery.verify(snap)


def test_backup_retention_does_not_delete_unrelated_folders(tmp_path):
    home = source(tmp_path / "home")
    backups = tmp_path / "backups"
    backups.mkdir()
    unrelated = backups / "radar-my-private-data"
    unrelated.mkdir()
    old = recovery.backup(home, backups, keep=1)
    new = recovery.backup(home, backups, keep=1)
    assert not old.exists() and new.exists() and unrelated.exists()


def test_cutover_preflight_refuses_pending_jobs_and_collection_start(tmp_path, monkeypatch):
    home = source(tmp_path / "home")
    backups = tmp_path / "backups"
    backups.mkdir()
    preflight = module("hosting_preflight")
    assert preflight.check(home, backups)["ready"]
    with Store(home / "data/radar.db") as store:
        store.queue_run([Job("demo", "Demo", "https://example.com")])
    assert "unfinished_work_in_cutover_snapshot" in preflight.check(home, backups)["findings"]
    monkeypatch.setenv("RADAR_HOME", str(home))
    monkeypatch.setenv("RADAR_DEPLOYMENT", "hosting")
    monkeypatch.setenv("RADAR_REQUIRE_SANDBOX", "1")
    with pytest.raises(ValueError, match="disabled"):
        module("entrypoint").validate(require_preparation=False)
    assert not (home / ".collection-enabled").exists()


def test_hosting_rejects_staging_workspace(tmp_path, monkeypatch):
    home = module("init_workspace").initialize(tmp_path / "stage")
    monkeypatch.setenv("RADAR_HOME", str(home))
    monkeypatch.setenv("RADAR_DEPLOYMENT", "hosting")
    with pytest.raises(ValueError, match="hosting workspace"):
        module("entrypoint").validate()


def test_https_cookie_links_and_same_origin_write(tmp_path, monkeypatch):
    home = source(tmp_path / "home")
    settings = config.load(home / "radar.toml")
    monkeypatch.setattr(config, "load", lambda *args: settings)
    app = create_app(str(home / "data/radar.db"), language="ko")
    client = TestClient(app, base_url="https://radar.example.test")
    response = client.post("/login", data={"token": settings.server.token}, follow_redirects=False)
    assert (
        "Secure" in response.headers["set-cookie"] and "HttpOnly" in response.headers["set-cookie"]
    )
    result = client.get("/")
    assert result.status_code == 200 and "https://radar.example.test/?" in result.text
    assert "http://radar.example.test" not in result.text
    assert (
        client.post(
            "/manage/toggle", data={"id": "demo"}, headers={"Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400
    assert (
        client.get(
            "/", headers={"X-Forwarded-Host": "evil.example", "X-Forwarded-Proto": "http"}
        ).status_code
        == 200
    )


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.com",
        "https://example.com/path",
        "https://u:p@example.com",
        "https://example.com?x=1",
        "https://예시.com",
        "HTTPS://example.com",
        "https://EXAMPLE.com",
        "https://example.com:443",
    ],
)
def test_public_origin_validation(origin, tmp_path):
    path = tmp_path / "radar.toml"
    path.write_text("[server]\npublic_origin=" + json.dumps(origin) + "\n")
    with pytest.raises(config.ConfigError):
        config.load(path)


def test_render_does_not_install_or_start_units(tmp_path):
    out = module("render_units").render(
        tmp_path / "units", Path("/opt/radar/deploy"), Path("/srv/radar/workspace")
    )
    assert len(list(out.iterdir())) == 6
    assert "Asia/Seoul" in (out / "radar-hosting-scan.timer").read_text()
    assert ".collection-enabled" in (out / "radar-hosting-scan.service").read_text()
    assert "Restart=no" in (out / "radar-hosting-scan.service").read_text()
    with pytest.raises(FileExistsError):
        module("render_units").render(out, Path("/opt/radar/deploy"), Path("/srv/radar/workspace"))
