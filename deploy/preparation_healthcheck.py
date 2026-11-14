"""KB search + local credential presence only; never makes a paid model call."""
import os
from radar.config import PreparationSettings
from web.preparation_transport import request

settings = PreparationSettings(gateway_url='http://127.0.0.1:8612',
    gateway_token_file=os.environ['RADAR_GATEWAY_TOKEN_FILE'])
raise SystemExit(0 if request(settings, 'health').get('ready') else 1)
