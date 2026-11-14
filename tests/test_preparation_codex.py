"""Codex isolation, failure handling and independently checked web evidence."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess

from fastapi.testclient import TestClient
import pytest

from radar import config
from radar.policy_references import Fetcher, PolicyUnavailable
from web import preparation as prep, preparation_codex as codex, preparation_research as research
from web import preparation_gateway as gateway


@pytest.fixture
def settings():
    return config.PreparationSettings(backend="codex")


def process_mock(monkeypatch, *, result=None, events=None, code=0, timeout=False):
    captured = {}

    class Process:
        pid = 123456789
        returncode = code

        def __init__(self, args, **kwargs):
            captured.update(args=args, **kwargs)
            Path(args[args.index("--output-last-message") + 1]).write_text(json.dumps(result or {"ok": True}))

        def communicate(self, prompt=None, **kwargs):
            if prompt is not None:
                captured["prompt"] = prompt
                if timeout:
                    raise subprocess.TimeoutExpired("codex", 1)
            return "\n".join(json.dumps(e) for e in (events if events is not None else [
                {"type": "item.completed", "item": {"type": "agent_message"}},
                {"type": "turn.completed"},
            ])), "PRIVATE_STDERR"

    monkeypatch.setattr(codex.subprocess, "Popen", Process)
    return captured


def test_chatgpt_only_isolated_prompt_and_model(monkeypatch, settings):
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "CODEX_THREAD_ID"):
        monkeypatch.setenv(key, "PRIVATE")
    captured = process_mock(monkeypatch)
    result, _ = codex.complete(settings, "fixed system", "PRIVATE_INPUT", {"type": "object"})
    assert result == {"ok": True}
    args = captured["args"]
    assert "--ignore-user-config" in args and "--ephemeral" in args
    assert 'forced_login_method="chatgpt"' in args and 'web_search="disabled"' in args
    assert "features.shell_tool=false" in args and "mcp_servers={}" in args
    assert "--model" not in args and "sonnet" not in args and "PRIVATE_INPUT" not in str(args)
    assert captured["prompt"] == "PRIVATE_INPUT" and captured["start_new_session"]
    assert not Path(captured["cwd"]).exists()
    assert not {"OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "CODEX_THREAD_ID"} & captured["env"].keys()
    captured = process_mock(monkeypatch)
    codex.complete(replace(settings, codex_model="chosen-model"), "fixed", "p", {}, research=True)
    assert captured["args"][captured["args"].index("--model") + 1] == "chosen-model"
    assert 'web_search="live"' in captured["args"]


@pytest.mark.parametrize("events,code", [
    ([{"type": "turn.failed", "error": "PRIVATE"}], 0),
    ([{"type": "item.completed", "item": {"type": "command_execution"}}, {"type": "turn.completed"}], 0),
    ([{"type": "item.completed", "item": {"type": "web_search"}}, {"type": "turn.completed"}], 0),
    ([], 0),
    ([{"type": "turn.completed"}], 1),
])
def test_failures_do_not_leak_or_retry(monkeypatch, settings, events, code):
    process_mock(monkeypatch, events=events, code=code)
    with pytest.raises(prep.PrepError) as error:
        codex.complete(settings, "s", "p", {})
    assert "PRIVATE" not in str(error.value)


def test_timeout_kills_process_group(monkeypatch, settings):
    process_mock(monkeypatch, timeout=True)
    killed = []
    monkeypatch.setattr(codex.os, "killpg", lambda *args: killed.append(args))
    with pytest.raises(prep.PrepError, match="시간이 초과"):
        codex.complete(settings, "s", "p", {})
    assert len(killed) == 1 and killed[0][0] == 123456789


def research_fixture():
    url = "https://example.com/about"
    quote = "고객이 정기적으로 꽃을 받아볼 수 있는 구독 서비스입니다."
    result = {"sources": [{"url": url, "title": "공식 소개", "summary": "정기구독 서비스를 소개한다.",
                           "excerpt": quote, "published_at": ""}], "queries": ["subscription retention", "welcome journey"]}
    events = [{"type": "item.completed", "item": {"type": "web_search", "action": {"type": action},
               "results": [{"url": url}]}} for action in ("search", "open_page")]
    return result, events, quote


def test_research_requires_search_open_and_actual_quote(monkeypatch):
    result, events, quote = research_fixture()
    monkeypatch.setattr(Fetcher, "fetch", lambda self, url: (url, 200, {}, f"<p>{quote}</p>"))
    verified = codex.verified_sources(result, events)
    assert verified["state"] == "found" and verified["sources"][0]["id"] == "W1"
    assert codex.verified_sources(result, events[:1])["state"] == "insufficient"
    assert codex.verified_sources(result, events[1:])["state"] == "insufficient"
    monkeypatch.setattr(Fetcher, "fetch", lambda self, url: (url, 200, {}, "<p>Different content</p>"))
    assert codex.verified_sources(result, events)["sources"] == []
    monkeypatch.setattr(Fetcher, "fetch", lambda self, url: (url, 403, {}, f"<p>{quote}</p>"))
    assert codex.verified_sources(result, events)["sources"] == []


def test_unverified_urls_never_fetched_and_refusal_not_bypassed(monkeypatch):
    result, events, _ = research_fixture()
    calls = []

    def refused(self, url):
        calls.append(url)
        raise PolicyUnavailable("robots_disallowed")

    monkeypatch.setattr(Fetcher, "fetch", refused)
    assert codex.verified_sources(result, events)["sources"] == []
    assert len(calls) == 1
    result["sources"][0]["url"] = "http://127.0.0.1/private"
    assert codex.verified_sources(result, events)["sources"] == []
    assert len(calls) == 1


def test_routing_research_privacy_and_failure_fallback(monkeypatch, settings):
    seen = []
    monkeypatch.setattr(codex, "investigate", lambda cfg, public: seen.append(public) or research.fallback("insufficient"))
    research.research({"company": "Demo", "industry": "retail", "urls": [], "notes": "PRIVATE", "sender": "PRIVATE"}, settings)
    assert set(seen[0]) == {"company", "industry", "urls", "as_of"}
    monkeypatch.setattr(codex, "complete", lambda *args, **kw: ({"usecases": []}, []))
    assert prep.model("usecases", {}, settings) == {"usecases": []}

    def fail(*args):
        raise prep.PrepError("PRIVATE")

    monkeypatch.setattr(codex, "investigate", fail)
    assert research.research({"company": "Demo"}, settings)["state"] == "unavailable"


def test_config_accepts_codex_and_preserves_default(tmp_path):
    assert config.PreparationSettings().backend == "cli"
    (tmp_path / "radar.toml").write_text('[preparation]\nbackend="codex"\ncodex_model=""\n')
    assert config.load(tmp_path / "radar.toml").preparation.backend == "codex"


def test_gateway_codex_health_and_generation_metadata(tmp_path, monkeypatch, settings):
    secret = tmp_path / "token"
    secret.write_text("x" * 40)
    monkeypatch.setattr(gateway, "kb_search", lambda *args: {"hits": [{}]})
    monkeypatch.setattr(gateway.shutil, "which", lambda binary: "/bin/codex" if binary == "codex" else None)
    monkeypatch.setattr(codex, "complete", lambda *args, **kw: pytest.fail("Health must not invoke model"))
    client = TestClient(gateway.create_app(settings, str(secret), str(tmp_path / "lock")))
    headers = {"Authorization": "Bearer " + "x" * 40}
    assert client.get("/v1/health", headers=headers).json()["ready"]
    monkeypatch.setattr(codex, "complete", lambda *args, **kw: ({"usecases": []}, []))
    result = client.post("/v1/model", headers=headers, json={"step": "usecases", "payload": {}})
    assert result.json()["_generation"] == {"backend": "codex", "model": "codex-default"}
