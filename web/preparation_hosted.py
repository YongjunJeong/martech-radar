"""Standalone hosted gateway with a verified, offline Marketing KB bundle."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading

KB_LOCK = threading.Lock()


def prepare_bundle(bundle):
    bundle = Path(bundle).resolve()
    manifest = json.loads((bundle / 'BUNDLE.json').read_text())
    if manifest.get('format') != 'radar-kb-v1' or not manifest.get('files'):
        raise ValueError('Invalid KB bundle')
    for name, expected in manifest['files'].items():
        path = bundle / name
        if not path.resolve().is_relative_to(bundle) or not path.is_file() or path.is_symlink():
            raise ValueError('Invalid bundle file')
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                raise ValueError('KB bundle checksum mismatch')
    actual = {str(p.relative_to(bundle)) for p in bundle.rglob('*') if p.is_file() and p.name != 'BUNDLE.json'}
    if actual != set(manifest['files']):
        raise ValueError('Unexpected KB bundle files')
    # Chroma opens a writable database even for search. Work only on an ephemeral
    # copy, so the release bundle remains read-only and is also the rollback unit.
    runtime = Path(tempfile.mkdtemp(prefix='radar-kb-'))
    for name in ('kb', 'taxonomy'):
        shutil.copytree(bundle / name, runtime / name)
    os.environ.update(MKT_KB_ROOT=str(runtime), HF_HUB_CACHE=str(bundle / 'models'),
                      HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', ANONYMIZED_TELEMETRY='False',
                      TOKENIZERS_PARALLELISM='false', OMP_NUM_THREADS='2', MKL_NUM_THREADS='2')
    sys.path.insert(0, str(bundle))
    return runtime


def search(body):
    from mkb.api import search as kb_search
    # Model work is serialized and cannot trigger the KB's optional Claude translator.
    with KB_LOCK:
        data = dict(body)
        data.update(market='kr', include_evidence=True, translate=False)
        return {'hits': kb_search(**data)}


def settings_from_env():
    from radar.config import PreparationSettings
    from .preparation_api import credentials
    settings = PreparationSettings(enabled=True, backend='anthropic',
        api_model=os.environ['RADAR_API_MODEL'], api_key_file=os.environ['RADAR_API_KEY_FILE'],
        mkb_url='embedded', timeout_seconds=int(os.environ.get('RADAR_GENERATION_TIMEOUT', '240')))
    if not 30 <= settings.timeout_seconds <= 600:
        raise ValueError('Generation timeout must be 30..600 seconds')
    credentials(settings)
    return settings


def main():
    import uvicorn
    from .preparation_gateway import create_app
    settings = settings_from_env()
    runtime = prepare_bundle(os.environ['RADAR_KB_BUNDLE'])
    try:
        if not search({'query': 'retention customer journey', 'top_k': 1})['hits']:
            raise ValueError('KB search produced no results')
        uvicorn.run(create_app(settings), host='0.0.0.0', port=8612, access_log=False)
    finally:
        shutil.rmtree(runtime)


if __name__ == '__main__':
    main()
