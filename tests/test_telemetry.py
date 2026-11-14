"""Telemetry must remain read-only and end with its scheduled wrapper."""
import json
import os
import shutil
from pathlib import Path
import subprocess

import pytest

from radar.telemetry import snapshot


def test_missing_database_not_created(tmp_path):
    (tmp_path / 'data').mkdir()
    row = snapshot(tmp_path, {})
    assert row['jobs_error'] == 'OperationalError'
    assert not (tmp_path / 'data/radar.db').exists()
    assert 'loadavg' in row and 'pressure/memory' in row


@pytest.mark.skipif(shutil.which("flock") is None, reason="requires util-linux flock")
def test_wrapper_records_samples_and_stops_on_failed_batch(tmp_path):
    fake = tmp_path / 'radar'
    fake.write_text('#!/bin/sh\nif [ "$1" = batch ]; then sleep 1; exit 2; fi\nexit 0\n')
    fake.chmod(0o755)
    script = Path(__file__).resolve().parents[1] / 'bin/weekly-scan'
    result = subprocess.run([str(script), '--due'], env={**os.environ,
        'RADAR_HOME': str(tmp_path), 'RADAR_BIN': str(fake)}, timeout=15)
    assert result.returncode == 2
    files = list((tmp_path / 'data/logs').glob('load-*.jsonl'))
    assert len(files) == 1
    rows = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert rows[0]['event'] == 'sample'
    assert rows[-1]['event'] == 'stop'
    assert 'stat' in rows[0] and 'diskstats' in rows[0]
