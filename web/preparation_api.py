"""Hosted Messages API backend. No CLI, login directory, redirects or retries."""
import json
import time
from pathlib import Path
from urllib.request import Request, build_opener

from .preparation_transport import MAX_BYTES, NoRedirect

MESSAGES_URL = "https://api.anthropic.com/v1/messages"


def credentials(settings):
    key = Path(settings.api_key_file).read_text().strip()
    if not settings.api_model.strip() or len(key) < 32 or not key.isascii() or any(c.isspace() for c in key):
        raise ValueError("API model and a valid secret file are required")
    return key


def request(settings, system, messages, *, output_schema=None, tools=None, timeout=None):
    from .preparation import PrepError
    try:
        body = {"model": settings.api_model, "max_tokens": 8000, "system": system, "messages": messages}
        if tools:
            body["tools"] = tools
        if output_schema:
            body["output_config"] = {"format": {"type": "json_schema", "schema": api_schema(output_schema)}}
        raw = json.dumps(body, ensure_ascii=False).encode()
        if len(raw) > MAX_BYTES:
            raise ValueError("request too large")
        req = Request(MESSAGES_URL, data=raw, headers={"x-api-key": credentials(settings),
                      "anthropic-version": "2023-06-01", "Content-Type": "application/json"})
        # A failed paid request is ambiguous: never retry it automatically.
        with build_opener(NoRedirect()).open(req, timeout=timeout or settings.timeout_seconds) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("response too large")
        result = json.loads(raw)
        if not isinstance(result, dict) or result.get("stop_reason") != "end_turn" or not isinstance(result.get("content"), list):
            raise ValueError("incomplete model response")
        return result
    except (OSError, ValueError):
        raise PrepError("제안 생성 API 응답을 확인하지 못했습니다. 연결·인증 상태를 확인해 주세요.") from None


def api_schema(schema):
    """Provider supports only minItems 0/1. Full constraints checked locally."""
    if isinstance(schema, list):
        return [api_schema(x) for x in schema]
    if not isinstance(schema, dict):
        return schema
    return {k: (min(1, v) if k == "minItems" else api_schema(v)) for k, v in schema.items()
            if k not in ("maxItems", "minLength", "maxLength", "minimum", "maximum")}


def validate_shape(value, schema):
    """Validate the small, closed JSON-schema subset used by preparation."""
    kind = schema["type"]
    expected = {"object": dict, "array": list, "string": str}[kind]
    if not isinstance(value, expected) or ("enum" in schema and value not in schema["enum"]):
        raise ValueError("invalid output type")
    if kind == "object":
        if set(value) != set(schema["required"]):
            raise ValueError("invalid output fields")
        for k, v in value.items():
            validate_shape(v, schema["properties"][k])
    elif kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 10000):
            raise ValueError("invalid output length")
        for v in value:
            validate_shape(v, schema["items"])


def generate(settings, system, prompt, schema, *, timeout=None):
    from .preparation import PrepError
    result = request(settings, system, [{"role": "user", "content": prompt}], output_schema=schema, timeout=timeout)
    try:
        output = json.loads("".join(x.get("text", "") for x in result["content"] if x.get("type") == "text"))
        validate_shape(output, schema)
        return output
    except (ValueError, TypeError, KeyError):
        raise PrepError("제안 생성 API가 올바른 문서 형식을 반환하지 않았습니다.") from None


def research_events(result):
    """Translate provider-returned search/read evidence, never model-authored URLs."""
    events = []
    for block in result["content"]:
        kind, data = block.get("type"), block.get("content")
        ident = block.get("tool_use_id", "")
        if kind == "web_search_tool_result" and isinstance(data, list):
            links = [x for x in data if isinstance(x, dict) and x.get("type") == "web_search_result"]
            events.extend([
                {"message": {"content": [{"type": "tool_use", "id": ident, "name": "WebSearch", "input": {}}]}},
                {"message": {"content": [{"type": "tool_result", "tool_use_id": ident}]},
                 "tool_use_result": {"results": [{"content": links}]}},
            ])
        elif kind == "web_fetch_tool_result" and isinstance(data, dict) and data.get("type") == "web_fetch_result":
            source = data.get("content", {}).get("source", {})
            # PDFs/base64 cannot be checked against a literal text excerpt.
            if source.get("type") != "text" or not isinstance(source.get("data"), str):
                continue
            events.extend([
                {"message": {"content": [{"type": "tool_use", "id": ident, "name": "WebFetch", "input": {"url": data.get("url")}}]}},
                {"message": {"content": [{"type": "tool_result", "tool_use_id": ident, "content": source["data"]}]},
                 "tool_use_result": {"code": 200}},
            ])
    return events


def investigate(settings, public):
    from . import preparation_research as research
    start = time.monotonic()
    # Separate tool research from JSON extraction: search citations and strict
    # JSON output cannot be enabled together. Only public company context here.
    result = request(settings, research.RESEARCH_SYSTEM.replace("WebSearch", "web_search").replace("WebFetch", "web_fetch"),
                     [{"role": "user", "content": json.dumps(public, ensure_ascii=False)}], tools=[
                         {"type": "web_search_20250305", "name": "web_search", "max_uses": 3},
                         {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 4,
                          "max_content_tokens": 6000, "citations": {"enabled": False}},
                     ])
    events = research_events(result)
    remaining = settings.timeout_seconds - (time.monotonic() - start)
    if remaining < 5 or not any(e.get("tool_use_result", {}).get("code") == 200 for e in events):
        return research.fallback("insufficient")
    # No server tool call in this second, bounded request. Source verification
    # below still requires a search result AND successful fetch AND exact excerpt.
    output = generate(settings, research.RESEARCH_SYSTEM,
                      json.dumps({"company_context": public, "tool_evidence": events}, ensure_ascii=False),
                      research.research_schema(), timeout=remaining)
    return research.verified_research(events + [{"type": "result", "structured_output": output}], public.get("urls") or [])
