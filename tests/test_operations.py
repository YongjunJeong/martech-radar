"""Unattended execution must surface failures and honour paused targets."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from radar.store import Store
from radar.targets import Target, TargetSet
from radar.batch import Job


@pytest.mark.parametrize('batch_code,digest_code,expected', [(0,0,0),(2,0,2),(0,3,3),(1,3,1)])
@pytest.mark.skipif(shutil.which("flock") is None, reason="requires util-linux flock")
def test_scheduled_script_preserves_exit_status(tmp_path, batch_code, digest_code, expected):
    fake = tmp_path / 'radar'
    fake.write_text(f'#!/bin/sh\ncase "$1" in batch) exit {batch_code};; digest) exit {digest_code};; esac\n')
    fake.chmod(0o755)
    script = Path(__file__).resolve().parents[1] / 'bin/weekly-scan'
    run = subprocess.run([str(script), '--due'], env={**os.environ,
                         'RADAR_HOME': str(tmp_path), 'RADAR_BIN': str(fake)}, capture_output=True)
    assert run.returncode == expected


def test_pausing_after_enqueue_prevents_claim_and_allows_run_to_close(tmp_path):
    with Store(tmp_path / 'radar.db') as s:
        s.import_watchlist(TargetSet((Target('shop','shop','test',('https://shop.example/',)),),
                                    {'test':'test'}, 'test'))
        run = s.queue_run([Job('shop','shop','https://shop.example/')])
        s.set_target_enabled('shop', False)
        assert s.claim_job('worker') is None
        assert s.run_progress(run)['complete']
        assert s.conn.execute('select error from job').fetchone()[0] == 'target disabled'


def test_due_noop_is_success(tmp_path):
    from radar.cli import main
    from unittest.mock import patch
    with Store(tmp_path / 'radar.db') as s:
        s.import_watchlist(TargetSet((Target('shop','shop','test',('https://shop.example/',)),),
                                    {'test':'test'}, 'test'))
    with patch('radar.batch.due_split', return_value=([], ['shop'])):
        assert main(['batch','--db',str(tmp_path / 'radar.db'),'--due']) == 0


def test_inline_batch_waits_for_job_owned_by_another_worker(tmp_path, monkeypatch):
    import asyncio
    from radar import worker, registry
    from radar.collector import ScanConfig

    class NoBrowser:
        def __init__(self, config):
            self.config = config

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def remember_robots_in(self, store):
            pass

    monkeypatch.setattr(worker, 'Collector', NoBrowser)

    async def run():
        with Store(tmp_path / 'radar.db') as s:
            s.import_watchlist(TargetSet((Target('shop','shop','test',('https://shop.example/',)),),
                                        {'test':'test'}, 'test'))
            run_id = s.queue_run([Job('shop','shop','https://shop.example/')])
            job = s.claim_job('other-worker')

            async def other_worker():
                await asyncio.sleep(0.05)
                s.fail_job(job['id'], 'simulated other-worker completion')

            finisher = asyncio.create_task(other_worker())
            await worker.drain_queue(s, registry.load(), ScanConfig(), run_id=run_id)
            assert finisher.done()
            assert s.run_progress(run_id)['complete']

    asyncio.run(run())


def test_missing_flock_is_an_error(tmp_path):
    script = Path(__file__).resolve().parents[1] / 'bin/weekly-scan'
    tools = tmp_path / 'bin'
    tools.mkdir()
    for name in ('bash', 'dirname', 'mkdir'):
        (tools / name).symlink_to(shutil.which(name))
    result = subprocess.run([str(script)], env={**os.environ,
        'PATH': str(tools), 'RADAR_HOME': str(tmp_path)}, capture_output=True, text=True)
    assert result.returncode == 127
    assert 'requires flock' in result.stderr
