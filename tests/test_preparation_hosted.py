import hashlib
import json
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient
import pytest

from radar.config import PreparationSettings, ConfigError, load
from web import preparation as prep, preparation_api as api, preparation_gateway as gateway
from web import preparation_research as research
from web.preparation_hosted import prepare_bundle


@pytest.fixture
def settings(tmp_path):
    path = tmp_path / 'secret'
    path.write_text('test-api-secret-' * 4)
    return PreparationSettings(backend='anthropic', api_model='test-model', api_key_file=str(path))


@pytest.fixture
def provider(monkeypatch):
    state = {'calls': [], 'status': 200, 'result': {'stop_reason': 'end_turn', 'content': []}}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            state['calls'].append((dict(self.headers), json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            self.send_response(state['status'])
            if state['status'] == 302:
                self.send_header('Location', '/credential-leak')
            self.end_headers()
            self.wfile.write(json.dumps(state['result']).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(api, 'MESSAGES_URL', f'http://127.0.0.1:{server.server_port}/messages')
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_hosted_model_no_cli_and_full_validation(settings, provider, monkeypatch):
    monkeypatch.setattr(prep, 'command', lambda *a, **k: pytest.fail('CLI must not run'))
    output = {'text': 'validated output', 'source_ids': ['M1']}
    provider['result']['content'] = [{'type': 'text', 'text': json.dumps(output)}]
    assert prep.model('proposal', {'safe': 'payload'}, settings) == output
    headers, body = provider['calls'][0]
    assert {k.lower(): v for k, v in headers.items()}['x-api-key'] == Path(settings.api_key_file).read_text()
    assert body['model'] == 'test-model' and 'tools' not in body
    assert body['output_config']['format']['schema'] == prep.schema('proposal')
    provider['result']['content'] = [{'type': 'text', 'text': '{"usecases": []}'}]
    with pytest.raises(prep.PrepError):
        prep.model('usecases', {}, settings)
    assert api.api_schema(prep.schema('usecases'))['properties']['usecases']['minItems'] == 1


@pytest.mark.parametrize('status', [302, 401, 429, 500])
def test_api_failure_no_retry_no_secret(settings, provider, status):
    provider['status'] = status
    provider['result'] = {'error': Path(settings.api_key_file).read_text()}
    with pytest.raises(prep.PrepError) as exc:
        prep.model('email', {}, settings)
    assert 'test-api-secret' not in str(exc.value)
    assert len(provider['calls']) == 1


@pytest.mark.parametrize('stop', ['max_tokens', 'pause_turn', 'refusal', 'tool_use'])
def test_incomplete_api_response_not_saved(settings, provider, stop):
    provider['result']['stop_reason'] = stop
    with pytest.raises(prep.PrepError):
        prep.model('proposal', {}, settings)


def tool_result():
    url = 'https://example.com/news'
    text = 'Our membership program helps customers discover relevant products and return for new releases.'
    return {'content': [
        {'type': 'web_search_tool_result', 'tool_use_id': 's1', 'content': [
            {'type': 'web_search_result', 'url': url, 'title': 'News'}]},
        {'type': 'web_fetch_tool_result', 'tool_use_id': 'f1', 'content': {
            'type': 'web_fetch_result', 'url': url,
            'content': {'source': {'type': 'text', 'data': text}}}},
    ]}, {'sources': [{'url': url, 'title': 'News', 'summary': '공개 멤버십 프로그램 안내',
                     'excerpt': text[:60], 'published_at': ''}], 'queries': ['loyalty', 'retention']}


def test_research_api_provenance_and_public_context(settings, monkeypatch):
    result, output = tool_result()
    calls = []
    def request(*args, **kwargs):
        calls.append((args, kwargs))
        return result
    monkeypatch.setattr(api, 'request', request)
    monkeypatch.setattr(api, 'generate', lambda *a, **k: output)
    actual = research.research({'company': 'Example', 'industry': 'retail', 'urls': [],
                                'notes': 'PRIVATE NOTE', 'contact': 'private@example.com'}, settings)
    assert actual['state'] == 'found' and len(actual['sources']) == 1
    serialized = json.dumps(calls[0][0][2])
    assert 'PRIVATE NOTE' not in serialized and 'private@example.com' not in serialized
    assert calls[0][1]['tools'][0]['max_uses'] == 3
    assert calls[0][1]['tools'][1]['max_uses'] == 4
    assert 'output_schema' not in calls[0][1]  # citations cannot combine with strict JSON
    output['sources'][0]['excerpt'] = 'a made up quote with no page evidence'
    assert research.research({'company': 'Example', 'urls': []}, settings)['state'] == 'insufficient'


def test_no_search_or_failed_fetch_never_becomes_company_fact():
    result, output = tool_result()
    for content in (result['content'][1:], result['content'][:1], []):
        events = api.research_events({'content': content})
        assert research.verified_research(events + [{'type': 'result', 'structured_output': output}])['sources'] == []


def test_hosted_health_no_paid_call(settings, tmp_path, monkeypatch):
    token = tmp_path / 'token'
    token.write_text('x' * 40)
    monkeypatch.setattr(api, 'request', lambda *a, **k: pytest.fail('paid call'))
    monkeypatch.setattr(gateway, 'kb_search', lambda *a: {'hits': [{'id': 'M1'}]})
    app = TestClient(gateway.create_app(settings, str(token), str(tmp_path / 'lock')))
    response = app.get('/v1/health', headers={'Authorization': 'Bearer ' + 'x' * 40})
    assert response.status_code == 200 and response.json()['model_auth'] == 'not_checked'
    Path(settings.api_key_file).unlink()
    assert app.get('/v1/health', headers={'Authorization': 'Bearer ' + 'x' * 40}).status_code == 503


def test_config_rejects_incomplete_api_backend(tmp_path):
    path = tmp_path / 'radar.toml'
    path.write_text('[preparation]\nbackend="anthropic"\n')
    with pytest.raises(ConfigError):
        load(path)


def test_bundle_rejects_tampering_before_import(tmp_path):
    file = tmp_path / 'model'
    file.write_text('verified')
    manifest = {'format': 'radar-kb-v1', 'files': {'model': hashlib.sha256(b'verified').hexdigest()}}
    (tmp_path / 'BUNDLE.json').write_text(json.dumps(manifest))
    file.write_text('tampered')
    with pytest.raises(ValueError, match='checksum'):
        prepare_bundle(tmp_path)
    manifest['files'] = {'../outside': 'digest'}
    (tmp_path / 'BUNDLE.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='Invalid bundle file'):
        prepare_bundle(tmp_path)


def test_export_preserves_committed_wal_and_is_self_contained(tmp_path):
    import sqlite3
    import runpy
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "deploy/export_preparation_kb.py"))
    export, digest = module["export"], module["digest"]
    root, cache, destination = tmp_path/'source', tmp_path/'models--intfloat--multilingual-e5-base', tmp_path/'release'
    build = root/'kb/builds/one'
    build.mkdir(parents=True)
    (root/'kb/current').symlink_to(build, target_is_directory=True)
    (root/'mkb').mkdir()
    (root/'mkb/__init__.py').write_text('')
    (root/'taxonomy').mkdir()
    (root/'taxonomy/industry.yaml').write_text('version: 1')
    (root/'.env').write_text('SECRET=must-not-copy')
    (build/'MANIFEST.json').write_text(json.dumps({'embed_model':'intfloat/multilingual-e5-base','n_canonical':1}))
    revision = 'a' * 40
    model = cache/'snapshots'/revision
    model.mkdir(parents=True)
    (model/'config.json').write_text('{}')
    (cache/'refs').mkdir()
    (cache/'refs/main').write_text(revision)
    con = sqlite3.connect(build/'catalog.sqlite')
    try:
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('CREATE TABLE records(value)')
        con.execute('INSERT INTO records VALUES (42)')
        con.commit()
        result = export(root, cache, destination)
        assert result['canonical'] == 1 and not (destination/'.env').exists()
        manifest = json.loads((destination/'BUNDLE.json').read_text())
        assert all(digest(destination/name) == sha for name,sha in manifest['files'].items())
        with sqlite3.connect(destination/'kb/current/catalog.sqlite') as restored:
            assert restored.execute('SELECT value FROM records').fetchone()[0] == 42
        assert not any(p.is_symlink() for p in destination.rglob('*'))
        with pytest.raises(ValueError, match='NEW'):
            export(root, cache, destination)
    finally:
        con.close()


def test_gateway_records_actual_api_model(settings, tmp_path, monkeypatch):
    token = tmp_path / 'token'
    token.write_text('x' * 40)
    monkeypatch.setattr(api, 'generate', lambda *a, **k: {'usecases': []})
    client = TestClient(gateway.create_app(settings, str(token), str(tmp_path / 'lock')))
    result = client.post('/v1/model', headers={'Authorization': 'Bearer ' + 'x' * 40},
                         json={'step': 'usecases', 'payload': {}})
    assert result.json()['_generation'] == {'backend': 'anthropic', 'model': 'test-model'}
