"""Export an immutable KB runtime bundle. Never copy logins, raw inputs or caches."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def snapshot_sqlite(source, destination):
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src, closing(sqlite3.connect(destination)) as dst, dst:
        src.backup(dst)
        if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Invalid KB database')


def export(root, model_cache, destination):
    root, model_cache, destination = map(lambda p: Path(p).resolve(), (root, model_cache, destination))
    build = (root / 'kb/current').resolve(strict=True)
    manifest = json.loads((build / 'MANIFEST.json').read_text())
    if manifest.get('embed_model') != 'intfloat/multilingual-e5-base':
        raise ValueError('Unsupported embedding model')
    if destination.exists() or destination.is_relative_to(root) or destination.is_relative_to(model_cache):
        raise ValueError('Use a NEW bundle directory outside the KB and model cache')
    revision = (model_cache / 'refs/main').read_text().strip()
    if len(revision) != 40 or any(c not in '0123456789abcdef' for c in revision):
        raise ValueError('Invalid model revision')
    model = model_cache / 'snapshots' / revision
    if not (model / 'config.json').is_file():
        raise ValueError('Cached embedding model missing')
    destination.mkdir(parents=True, mode=0o700)
    # Allowlist code and taxonomy only; no credentials, prompts, raw customer inputs.
    for name in ('mkb', 'taxonomy'):
        for source in sorted((root / name).rglob('*')):
            if source.is_file() and source.suffix in ('.py', '.yaml') and '__pycache__' not in source.parts:
                target = destination / source.relative_to(root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
    target_build = destination / 'kb/current'
    target_build.mkdir(parents=True)
    # A resolved build is immutable. SQLite backup includes committed WAL contents;
    # index binaries are checked before/after copying to reject an in-place rebuild.
    binaries = [p for p in build.rglob('*') if p.is_file() and p.suffix not in ('.sqlite', '.sqlite3')
                and not p.name.endswith(('-wal', '-shm'))]
    before = {str(p.relative_to(build)): digest(p) for p in binaries}
    for source in binaries:
        target = target_build / source.relative_to(build)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    for source in build.rglob('*'):
        if source.is_file() and source.suffix in ('.sqlite', '.sqlite3'):
            target = target_build / source.relative_to(build)
            target.parent.mkdir(parents=True, exist_ok=True)
            snapshot_sqlite(source, target)
    if before != {str(p.relative_to(build)): digest(p) for p in binaries}:
        raise ValueError('KB build changed during export; discard this incomplete bundle')
    # Resolve HF symlinks into self-contained files, retaining the fixed revision.
    hf = destination / 'models' / model_cache.name
    shutil.copytree(model, hf / 'snapshots' / revision, symlinks=False)
    (hf / 'refs').mkdir()
    (hf / 'refs/main').write_text(revision)
    files = {str(p.relative_to(destination)): digest(p) for p in sorted(destination.rglob('*')) if p.is_file()}
    report = {'format': 'radar-kb-v1', 'kb': manifest, 'model_revision': revision, 'files': files}
    (destination / 'BUNDLE.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return {'canonical': manifest['n_canonical'], 'files': len(files), 'model_revision': revision}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kb-root', required=True, type=Path)
    parser.add_argument('--model-cache', required=True, type=Path)
    parser.add_argument('--destination', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(export(args.kb_root, args.model_cache, args.destination)))
