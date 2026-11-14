"""Optional ChatGPT-authenticated Codex CLI adapter; never falls back to API keys.

JSONL is used for tool provenance; structured output comes from the last-message
file. No account files, prompts, model output or subprocess stderr are logged.
"""
from contextlib import suppress
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile

from . import preparation as prep

DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "apps", "plugins", "hooks", "memories",
    "view_image", "image_generation", "browser_use", "computer_use",
    "multi_agent", "multi_agent_v2", "shell_snapshot",
)
ERROR = "Codex 응답을 확인하지 못했습니다. CLI 버전·ChatGPT 로그인·사용량을 확인해 주세요."


def complete(settings, system, prompt, schema, *, research=False):
    with tempfile.TemporaryDirectory(prefix="radar-codex-") as cwd:
        root = Path(cwd)
        instructions, contract, output = (root / name for name in ("instructions.txt", "schema.json", "result.json"))
        instructions.write_text(system, encoding="utf-8")
        contract.write_text(json.dumps(schema), encoding="utf-8")
        args = [settings.codex_binary, "exec", "--ignore-user-config", "--ignore-rules",
                "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only", "--json",
                "--output-schema", str(contract), "--output-last-message", str(output), "-C", cwd]
        overrides = {
            "approval_policy": "never", "forced_login_method": "chatgpt", "model_provider": "openai",
            "project_doc_max_bytes": 0, "skills.include_instructions": False, "skills.bundled.enabled": False,
            "model_instructions_file": str(instructions), "web_search": "live" if research else "disabled",
            "history.persistence": "none", "mcp_servers": {},
            **{f"features.{name}": False for name in DISABLED_FEATURES},
        }
        for name, value in overrides.items():
            args += ["-c", name + "=" + ("{}" if isinstance(value, dict) else json.dumps(value))]
        if settings.codex_model.strip():
            args += ["--model", settings.codex_model.strip()]
        args += ["-"]  # Private input is never placed in argv.
        env = {k: v for k, v in os.environ.items() if k not in
               {"OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "CODEX_THREAD_ID"}}
        try:
            process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, cwd=cwd, env=env,
                                       start_new_session=True)
            try:
                stdout, _ = process.communicate(prompt, timeout=settings.timeout_seconds)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                raise prep.PrepError("Codex 응답 시간이 초과되었습니다. 자동 재시도하지 않았습니다.") from None
            if process.returncode != 0 or len(stdout) > 8_000_000:
                raise prep.PrepError(ERROR)
            events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
            if not events or any(not isinstance(e, dict) for e in events):
                raise prep.PrepError(ERROR)
            allowed = {"agent_message", "reasoning", "web_search"} if research else {"agent_message", "reasoning"}
            for event in events:
                if event.get("type") in ("error", "turn.failed"):
                    raise prep.PrepError(ERROR)
                if (event.get("type") in ("item.started", "item.updated", "item.completed")
                        and event.get("item", {}).get("type") not in allowed):
                    raise prep.PrepError(ERROR)
            if not any(e.get("type") == "turn.completed" for e in events) or output.stat().st_size > 1_000_000:
                raise prep.PrepError(ERROR)
            result = json.loads(output.read_text(encoding="utf-8"))
            if not isinstance(result, dict):
                raise prep.PrepError(ERROR)
            return result, events
        except (OSError, ValueError, TypeError, AttributeError):
            raise prep.PrepError(ERROR) from None


def verified_sources(result, events, public_urls=()):
    """A CLI search/open event alone is not proof of a successful page read.

    Verify the quote with bounded HTTP retrieval, sharing the policy collector's
    public-address, redirect, robots, byte and time limits. No browser fallback.
    """
    from radar.policy_references import Document, Fetcher, PolicyUnavailable
    from .preparation_research import fallback

    searched, opened = set(), set()
    for event in events:
        item = event.get("item", {})
        if event.get("type") != "item.completed" or item.get("type") != "web_search":
            continue
        action = item.get("action", {})
        urls = {r["url"] for r in item.get("results", [])
                if isinstance(r, dict) and prep.public_url(r.get("url"))}
        if action.get("type") == "search":
            searched.update(urls)
        elif action.get("type") == "open_page":
            opened.update(urls)
    known = {u for u in public_urls if prep.public_url(u)}
    eligible = opened & (searched | known)
    if not searched or not eligible or not isinstance(result.get("sources"), list):
        return fallback("insufficient")
    fetcher = Fetcher(sorted(eligible))
    sources, seen = [], set()
    def compact(text):
        return re.sub(r"[\s*`#]+", "", text)

    for item in result["sources"][:4]:
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in
                                               ("url", "title", "summary", "excerpt", "published_at")):
            continue
        url, excerpt = item["url"], item["excerpt"].strip()
        if url not in eligible or url in seen or not 12 <= len(excerpt) <= 200 or not item["summary"].strip():
            continue
        seen.add(url)
        try:
            _, code, _, html = fetcher.fetch(url)
            text = " ".join(text for _, text in Document(html, include_navigation=True).blocks)
            if code != 200 or compact(excerpt) not in compact(text):
                continue
        except (OSError, ValueError, PolicyUnavailable):
            continue
        published = item["published_at"]
        if published and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", published):
            published = ""
        sources.append({"id": f"W{len(sources)+1}", "kind": "기업 공개 자료", "title": item["title"][:200],
                        "text": item["summary"][:800], "excerpt": excerpt, "published_at": published,
                        "retrieved_at": prep.now(), "links": [{"title": item["title"][:200], "url": url}]})
    queries = result.get("queries", [])
    queries = [q[:240] for q in queries if isinstance(q, str) and q.strip()][:3] if isinstance(queries, list) else []
    return {"state": "found" if sources else "insufficient", "checked_at": prep.now(),
            "sources": sources, "queries": queries if sources else []}


def investigate(settings, public):
    from .preparation_research import RESEARCH_SYSTEM, research_schema
    system = RESEARCH_SYSTEM.replace("WebSearch", "web search").replace("WebFetch", "open_page")
    result, events = complete(settings, system, json.dumps(public, ensure_ascii=False),
                              research_schema(), research=True)
    return verified_sources(result, events, public.get("urls", []))
