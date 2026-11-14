"""Explicit KB/CLI readiness probe. Does not invoke a paid model or expose tokens."""
import json

from radar.config import load
from web.preparation import PrepError
from web.preparation_transport import request


def main():
    settings = load().preparation
    try:
        if not settings.enabled or not settings.gateway_url:
            result = {'ready': False, 'reason': 'preparation_gateway_not_configured'}
        else:
            result = request(settings, 'health')
    except PrepError:
        result = {'ready': False, 'reason': 'gateway_unavailable_or_unauthorized'}
    print(json.dumps(result))
    raise SystemExit(0 if result.get('ready') else 2)


if __name__ == '__main__':
    main()
