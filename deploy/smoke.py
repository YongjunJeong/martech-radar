"""Exercise the installed package in a disposable workspace; no live sites or DB."""
import argparse
import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import Request, urlopen

import uvicorn

import radar
from radar import registry, security, workspace, worker
from radar.batch import Job
from radar.collector import ScanConfig
from radar.store import Store
from radar.targets import Target, TargetSet
from web.app import create_app


def run(home, browser=False):
    os.environ['RADAR_HOME'] = str(home)
    os.environ.pop('RADAR_CONFIG', None)
    os.environ.pop('RADAR_PREPARATION_ENABLED', None)
    workspace.set_home(None)
    token = secrets.token_urlsafe(32)
    (home/'radar.toml').write_text(f'''[server]
token = "{token}"
[scan]
chromium_sandbox = true
per_site_delay_ms = 0
[ui]
language = "ko"
[preparation]
enabled = false
''')
    db = home/'data/radar.db'
    url = 'https://fixture.invalid/'
    result = {'scan': {'url':url, 'page_host':'fixture.invalid', 'status':'OK',
                      'started_at':'2026-10-15T03:00:00+09:00'}, 'evidence':{}, 'counts':{}}
    with Store(db) as store:
        store.import_watchlist(TargetSet((Target('fixture','검증 기업','retail',(url,)),), {'retail':'쇼핑'}, 'smoke'))
        for day, fp, category in [('15','braze','engagement'), ('16','criteo','advertising')]:
            result['scan']['started_at'] = f'2026-10-{day}T03:00:00+09:00'
            run_id = store.start_run(1)
            sid = store.record_scan(run_id,'fixture',result)
            store.record_detections(sid, {'detections':[dict(id=fp,name=fp,category=category,
                verdict='DETECTED',score=90,direct_layers=['network'],indirect_layers=[],
                indirect_only=False,match_count=1,matches=[])]})
    # Bound ephemeral port and a real HTTP server; no httpx/pytest dependency.
    sock = socket.socket()
    sock.bind(('127.0.0.1',0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(str(db)), log_level='error'))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic()+15
        while not server.started:
            if time.monotonic()>deadline:
                raise RuntimeError('web startup timed out')
            time.sleep(.05)
        base = f'http://127.0.0.1:{port}'
        with urlopen(base) as response:
            assert '/login' in response.geturl(), 'authentication must be enforced'
        paths = ['/', '/companies/fixture',
                 '/?industry=retail&solution_type=engagement&solution_type=advertising&observation=missing&observation=appeared',
                 '/static/desktop.css', '/static/sales_filters.js']
        for path in paths:
            request = Request(base+path, headers={'Cookie':security.COOKIE_NAME+'='+security.session_value(token)})
            with urlopen(request,timeout=10) as response:
                body = response.read().decode()
                assert response.status == 200
                if not path.startswith('/static/'):
                    assert '검증 기업' in body
        # The image must contain fingerprint YAML and the collector JS probe.
        assert len(registry.load().fingerprints) > 0
        assert (Path(radar.__file__).parent/'probe.js').is_file()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
    assert not thread.is_alive()
    with Store(db) as store:
        run_id = store.queue_run([Job('fixture','검증 기업',url)])
        old = store.claim_job('old-process',run_id)
        store.conn.execute("UPDATE job SET claimed_at='2000-01-01T00:00:00+00:00' WHERE id=?", (old['id'],))
        store.conn.commit()
    # A separate process/connection must recover a stale lease on restart.
    code = "from radar.store import Store; import sys;\nwith Store(sys.argv[1]) as s:\n j=s.claim_job('new-process',int(sys.argv[2])); assert j and j['claimed_by']=='new-process'; print(j['id'])"
    claimed = subprocess.check_output([sys.executable,'-c',code,str(db),str(run_id)], text=True)
    assert int(claimed) == old['id']
    with Store(db) as store:
        sid = store.record_scan(run_id,'fixture',result)
        store.finish_job(old['id'],sid)
        worker.finish_runs(store)
    from radar import recovery
    # Tiny synthetic fixture lives in a bounded tmpfs, not the production
    # backup volume. Exercise the same engine with a 10MiB test headroom.
    backup = recovery.configured_backup(home, home/'verified-backups', min_free_gib=.01)
    recovery.restore(backup, home/'restored')
    with Store(home/'restored/data/radar.db') as restored:
        assert restored.conn.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert restored.conn.execute('SELECT COUNT(*) FROM scan').fetchone()[0] == 3
        assert not restored.has_unfinished_jobs()
    if browser:
        page = home/'pages'
        page.mkdir()
        (page/'robots.txt').write_text('User-agent: *\nAllow: /\n')
        (page/'index.html').write_text('<html><body><h1>Local smoke fixture</h1>'+('<p>Local product information</p>'*30)+
            '<script>window.braze={};localStorage.setItem("ab.storage.fixture","test");</script></body></html>')
        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self,*args):
                pass
        fixture = ThreadingHTTPServer(('127.0.0.1',0), partial(QuietHandler,directory=str(page)))
        fixture_thread = threading.Thread(target=fixture.serve_forever,daemon=True)
        fixture_thread.start()
        try:
            local_url = f'http://127.0.0.1:{fixture.server_port}/'
            with Store(db) as store:
                store.import_watchlist(TargetSet((Target('browser','Browser fixture','retail',(local_url,)),), {'retail':'쇼핑'}, 'smoke'))
                run_id = store.queue_run([Job('browser','Browser fixture',local_url)])
                report = asyncio.run(worker.drain_queue(store,registry.load(),
                    scan_config=ScanConfig(settle_ms=500,interaction_ms=300,chromium_sandbox=True),run_id=run_id))
                assert report.done == 1 and report.failed == 0
                assert store.conn.execute("SELECT 1 FROM detection d JOIN scan s ON s.id=d.scan_id WHERE s.run_id=? AND d.fingerprint_id='braze' AND verdict='DETECTED'", (run_id,)).fetchone()
        finally:
            fixture.shutdown()
            fixture.server_close()
            fixture_thread.join(timeout=5)
    return {'web':'passed','packaged_assets':'passed','queue_restart':'passed','backup_restore':'passed',
            'sandboxed_browser':'passed' if browser else 'not_run'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser',action='store_true')
    parser.add_argument('--require-installed',action='store_true')
    args = parser.parse_args()
    if args.require_installed:
        assert 'site-packages' in Path(radar.__file__).parts, 'must run installed wheel, not source tree'
    with tempfile.TemporaryDirectory(prefix='radar-package-smoke-') as folder:
        print(json.dumps(run(Path(folder),args.browser)))
