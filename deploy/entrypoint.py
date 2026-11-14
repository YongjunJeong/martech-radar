"""Fail closed on incomplete container configuration, then replace PID 1's child."""
import os
import sys
import subprocess

from radar import config, workspace


def validate(*, require_preparation=True):
    path = workspace.path('radar.toml')
    if not path.is_file():
        raise ValueError('Mount a staging workspace with radar.toml; run deploy/init_workspace.py first.')
    mode = os.environ.get('RADAR_DEPLOYMENT', 'staging')
    if mode not in ('staging', 'hosting'):
        raise ValueError('Unknown deployment mode.')
    marker = workspace.path('.radar-' + mode)
    expected = 'isolated-staging' if mode == 'staging' else 'hosting-v1'
    if not marker.is_file() or marker.read_text().strip() != expected:
        raise ValueError(f'Not an initialized {mode} workspace; refusing to open its database.')
    settings = config.load()
    if not settings.server.requires_login:
        raise ValueError('A nonempty server.token is required in the mounted configuration.')
    if mode == 'hosting':
        if (len(settings.server.token.strip()) < 32 or not settings.server.public_origin
                or settings.server.allow_private_targets):
            raise ValueError('Hosting requires a strong token, HTTPS public_origin and public targets only.')
        if (os.environ.get('RADAR_REQUIRE_SANDBOX') == '1'
                or os.environ.get('RADAR_REQUIRE_COLLECTION') == '1'):
            activation = workspace.path('.collection-enabled')
            if not activation.is_file() or activation.read_text().strip() != 'collection-enabled-v1':
                raise ValueError('Hosting collection is disabled until explicit cutover activation.')
    if settings.preparation.enabled and require_preparation:
        from web.preparation_transport import validate as validate_gateway
        if not settings.preparation.gateway_url:
            raise ValueError('Container preparation requires a private gateway_url and gateway_token_file.')
        validate_gateway(settings.preparation)
    if os.environ.get('RADAR_REQUIRE_SANDBOX') == '1' and (
            os.geteuid() == 0 or not settings.scan.chromium_sandbox):
        raise ValueError('Collector staging requires a non-root user and scan.chromium_sandbox=true.')
    # Check actual volume ownership without changing existing permissions.
    workspace.path('data').mkdir(exist_ok=True)


def main():
    try:
        validate(require_preparation=sys.argv[1:3] == ['radar', 'serve'])
        if len(sys.argv) < 2:
            raise ValueError('A command is required.')
    except (ValueError, OSError, config.ConfigError) as exc:
        sys.exit(str(exc))
    role = os.environ.get('RADAR_TELEMETRY_ROLE')
    if role:
        # PID remains the same after exec. Sampler exits with this container.
        try:
            subprocess.Popen([sys.executable, '-m', 'radar.telemetry',
                '--home', str(workspace.home()), '--parent', str(os.getpid()),
                '--output-dir', str(workspace.path('data/logs')), '--label', role],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        except OSError:
            print('Telemetry could not start; application will continue.', file=sys.stderr)
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == '__main__':
    main()
