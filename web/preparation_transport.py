"""Bounded gateway calls. No redirect, retry, or credential-bearing error text."""

import json
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_BYTES = 2 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def token(path):
    value = Path(path).read_text().strip()
    if len(value) < 32 or any(c.isspace() for c in value) or not value.isascii():
        raise ValueError(
            "Gateway token must contain at least 32 ASCII characters without whitespace."
        )
    return value


def validate(settings):
    url = urlsplit(settings.gateway_url)
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("Invalid preparation gateway URL.")
    token(settings.gateway_token_file)


def request(settings, operation, payload=None):
    from .preparation import PrepError

    try:
        validate(settings)
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        if body is not None and len(body) > MAX_BYTES:
            raise ValueError("request too large")
        req = Request(
            settings.gateway_url.rstrip("/") + "/v1/" + operation,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + token(settings.gateway_token_file),
            },
        )
        # Paid generation is never automatically retried after an ambiguous failure.
        with build_opener(NoRedirect()).open(
            req, timeout=settings.timeout_seconds + 15
        ) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("response too large")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("response must be an object")
        return result
    except (OSError, ValueError) as exc:
        raise PrepError(
            "제안 서비스에 연결하지 못했습니다. 연결·인증 상태를 확인한 뒤 다시 시도해 주세요."
        ) from exc
