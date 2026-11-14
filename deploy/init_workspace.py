"""Create only a new, empty staging workspace; never import live data or credentials."""
import argparse
import os
from pathlib import Path
import secrets
import json
from urllib.parse import urlsplit


def initialize(path: Path, gateway_url: str = '', *, mode='staging', public_origin=''):
    from radar.config import validate_public_origin
    if mode not in ('staging', 'hosting'):
        raise ValueError('Unknown deployment mode')
    validate_public_origin(public_origin)
    if mode == 'hosting' and not public_origin:
        raise ValueError('Hosting requires an HTTPS public origin')
    if gateway_url:
        parsed = urlsplit(gateway_url)
        if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
            raise ValueError('Invalid gateway URL')
    path = path.expanduser().resolve()
    # mkdir without exist_ok deliberately rejects every pre-existing directory.
    path.mkdir(mode=0o700)
    (path / 'data').mkdir(mode=0o700)
    (path / f'.radar-{mode}').write_text('isolated-staging\n' if mode == 'staging' else 'hosting-v1\n')
    token = secrets.token_urlsafe(32)
    with (path / 'radar.toml').open('x') as stream:
        os.chmod(stream.name, 0o600)
        stream.write(f'''[server]
token = "{token}"
allow_private_targets = false
public_origin = {json.dumps(public_origin)}
[scan]
locale = "ko-KR"
timezone = "Asia/Seoul"
concurrency = {3 if mode == 'hosting' else 1}
chromium_sandbox = true
[cadence]
default = "1d"
daily_limit = {0 if mode == 'hosting' else 3}
spread_weekly = false
[ui]
language = "ko"
[analysis]
since = "2026-10-15"
timezone = "Asia/Seoul"
[preparation]
enabled = {str(bool(gateway_url)).lower()}
gateway_url = {json.dumps(gateway_url)}
gateway_token_file = {json.dumps('/run/secrets/preparation_gateway_token' if gateway_url else '')}
[notify]
webhook = ""
''')
    return path


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--gateway-url', default='')
    parser.add_argument('--mode', choices=('staging','hosting'), default='staging')
    parser.add_argument('--public-origin', default='')
    args = parser.parse_args()
    print('Created collection-disabled workspace:', initialize(args.directory, args.gateway_url,
          mode=args.mode, public_origin=args.public_origin))
    print('Login token is in its private radar.toml; no targets have been registered.')
