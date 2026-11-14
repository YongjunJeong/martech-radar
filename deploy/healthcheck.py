"""Check an authenticated DB-backed page, without printing credentials."""
from urllib.request import Request, urlopen
from urllib.parse import urlparse
import os

from radar import config, security


def main():
    settings = config.load().server
    headers = {'Cookie': security.COOKIE_NAME + '=' + security.session_value(settings.token)}
    if settings.public_origin:
        headers['Host'] = urlparse(settings.public_origin).netloc
    port = int(os.environ.get('RADAR_HEALTH_PORT', '8848'))
    request = Request(f'http://127.0.0.1:{port}/', headers=headers)
    with urlopen(request, timeout=8) as response:
        if (response.status != 200 or urlparse(response.geturl()).path != '/'
                or '<main' not in response.read().decode()):
            raise RuntimeError('Dashboard health check failed')


if __name__ == '__main__':
    main()
