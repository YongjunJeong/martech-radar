"""Single browser owner, preserved queues, and one recoverable backup policy."""

import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path

import pytest

from radar import batch, registry, recovery, tasks, worker, workspace
from radar.collector import ScanConfig
from radar.store import Store
from test_hosting_readiness import source, module


def heartbeat(store):
    store.set_meta('worker_seen', json.dumps({'name': 'test-worker', 'at': datetime.now(UTC).isoformat()}))


def test_scheduler_without_worker_does_not_queue_or_launch_browser(tmp_path, monkeypatch):
    home = source(tmp_path / 'home')
    async def unexpected(*args, **kwargs):
        raise AssertionError('coordinator must not launch Chromium')
    monkeypatch.setattr(worker, 'drain_queue', unexpected)
    with Store(home / 'data/radar.db') as store:
        with pytest.raises(batch.ExternalWorkerError, match='heartbeat'):
            asyncio.run(batch.run_batch(store, registry.load(), external_worker=True))
        assert not store.runs() and not store.active_runs()


def test_coordinator_timeout_preserves_work_for_worker(tmp_path):
    home = source(tmp_path / 'home')
    with Store(home / 'data/radar.db') as store:
        heartbeat(store)
        with pytest.raises(batch.ExternalWorkerError, match='wait limit'):
            asyncio.run(batch.run_batch(store, registry.load(), external_worker=True,
                                        worker_timeout_seconds=.001))
        run = store.runs()[0]
        progress = store.run_progress(run['id'])
        assert progress['pending'] == 1 and progress['finished_at'] is None
        assert store.scan_ids() == []


def test_external_worker_collects_local_page_and_coordinator_never_launches_browser(tmp_path, site, monkeypatch):
    home = source(tmp_path / 'home')
    path = home / 'data/radar.db'
    rules = registry.load()
    original = worker.drain_queue
    async def forbidden(*args, **kwargs):
        raise AssertionError('scheduled process opened a browser')
    monkeypatch.setattr(worker, 'drain_queue', forbidden)
    with Store(path) as coordinator:
        coordinator.upsert_target(id='demo', company='Demo', industry='retail', urls=[site + '/plain.html'])
        heartbeat(coordinator)
        async def consume():
            # Independent DB connection, as in the always-on worker.
            with Store(path) as consumer:
                await original(consumer, rules, scan_config=ScanConfig(
                    settle_ms=200, interaction_ms=100, nav_timeout_ms=8000, hard_timeout_ms=15000),
                    concurrency=1, idle_exit=True)
                worker.finish_runs(consumer, rules)
        async def run():
            report, _ = await asyncio.gather(
                batch.run_batch(coordinator, rules, external_worker=True), consume())
            return report
        report = asyncio.run(run())
        assert len(report.ok) == 1 and len(coordinator.scan_ids()) == 1
        assert coordinator.run_progress(report.run_id)['finished_at']


def test_hosting_coordinator_still_needs_cutover_activation(tmp_path, monkeypatch):
    home = source(tmp_path / 'home')
    monkeypatch.setenv('RADAR_HOME', str(home))
    monkeypatch.setenv('RADAR_DEPLOYMENT', 'hosting')
    monkeypatch.setenv('RADAR_REQUIRE_COLLECTION', '1')
    monkeypatch.delenv('RADAR_REQUIRE_SANDBOX', raising=False)
    with pytest.raises(ValueError, match='disabled'):
        module('entrypoint').validate(require_preparation=False)
    (home / '.collection-enabled').write_text('collection-enabled-v1')
    module('entrypoint').validate(require_preparation=False)


def test_task_and_scheduled_backup_share_environment_policy_and_restore(tmp_path, monkeypatch):
    home = source(tmp_path / 'home')
    dest = tmp_path / 'private-backups'
    dest.mkdir()
    legacy = dest / 'radar-2026-09-01.db'
    legacy.write_bytes(b'legacy snapshot preserved')
    monkeypatch.setenv('RADAR_HOME', str(home))
    monkeypatch.setenv('RADAR_BACKUP_DIRECTORY', str(dest))
    monkeypatch.setenv('RADAR_BACKUP_KEEP', '2')
    with Store(home / 'data/radar.db') as store:
        first = Path(tasks.backup_snapshot(store)['path'])
        second = recovery.configured_backup(home)
        third = Path(tasks.backup_snapshot(store)['path'])
    assert not first.exists() and second.exists() and third.exists() and legacy.exists()
    assert recovery.restore(third, tmp_path / 'restored')['counts']['target'] == 1
    with pytest.raises(ValueError, match='positive'):
        recovery.configured_backup(home, keep=0)


def test_explicit_database_backup_does_not_silently_copy_workspace_default(tmp_path, monkeypatch):
    home = source(tmp_path / 'home')
    monkeypatch.setenv('RADAR_HOME', str(home))
    with Store(tmp_path / 'custom.db') as store:
        store.upsert_target(id='custom', company='Custom', industry='retail', urls=['https://example.org'])
        result = tasks.backup_snapshot(store)
    with Store(Path(result['path']) / 'data/radar.db') as restored:
        assert [r['id'] for r in restored.targets()] == ['custom']


def test_external_worker_cli_rejects_ignored_collector_overrides(tmp_path):
    from radar.cli import main
    try:
        assert main(['--home', str(tmp_path), 'batch', '--external-worker', '--no-evidence']) == 2
        assert not (tmp_path / 'data/radar.db').exists()
    finally:
        workspace.set_home(None)


def test_backup_cli_never_creates_an_empty_source_database(tmp_path):
    from radar.cli import main
    try:
        assert main(['--home', str(tmp_path), 'backup']) == 2
        assert not (tmp_path / 'data/radar.db').exists()
    finally:
        workspace.set_home(None)
