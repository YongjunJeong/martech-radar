"""Recommendation, provenance, atomic document generation and collector isolation."""

from dataclasses import replace
import copy
import json
from pathlib import Path
import threading
import time
from fastapi.testclient import TestClient
import pytest
from radar import config
from radar.store import Store
from web.app import create_app
from web import preparation as prep, preparation_research as research

SOURCES = [
    {
        "id": "M1",
        "kind": "Marketing KB",
        "title": "마케팅 패턴",
        "text": "공개된 시나리오",
        "links": [{"title": "참고", "url": "https://example.com/case"}],
        "market_note": "",
    },
    {
        "id": "M2",
        "kind": "Marketing KB",
        "title": "고객 여정 패턴",
        "text": "공개된 구현 근거",
        "links": [{"title": "가이드", "url": "https://example.net/pattern"}],
        "market_note": "",
    },
]


def output(step):
    item = {
        "title": "상품 탐색 지원",
        "text": "구매 전환을 위한 가설입니다. 필요 데이터와 수신 동의를 확인하세요. [M1] [M2]",
        "source_ids": ["M1", "M2"],
        "reason": "유사 업종에서 상품 탐색 경험을 돕는 시나리오로 검토할 수 있습니다.",
        "basis": "industry_hypothesis",
    }
    if step == "usecases":
        return {
            "usecases": [
                copy.deepcopy(item),
                {**copy.deepcopy(item), "title": "첫 방문 지원"},
                {**copy.deepcopy(item), "title": "재방문 지원"},
            ]
        }
    if step == "email":
        return {
            "subject": "웹사이트 경험 개선 논의",
            "text": "안녕하세요. 상품 탐색 경험을 개선할 수 있는 시나리오를 제안드립니다. 관련 논의가 가능하실까요?",
            "source_ids": ["M1", "M2"],
        }
    if step == "package":
        return {"proposal": output("proposal"), "email": output("email")}
    return {"text": item["text"], "source_ids": item["source_ids"]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "radar.db"
    with Store(db) as s:
        s.upsert_industry("beauty_fashion", "뷰티·패션")
        s.upsert_target(
            id="demo",
            company="테스트몰",
            industry="beauty_fashion",
            urls=["https://example.com"],
            note="PRIVATE_NOTE_DO_NOT_SEND",
        )
        s.upsert_target(
            id="second",
            company="다른 기업",
            industry="beauty_fashion",
            urls=["https://example.net"],
        )
    cfg = replace(config.Config(), preparation=config.PreparationSettings(enabled=True))
    monkeypatch.setattr(config, "load", lambda *args: cfg)
    monkeypatch.setattr(prep, "retrieve", lambda *args: copy.deepcopy(SOURCES))
    monkeypatch.setattr(research, "research", lambda *args: research.fallback("insufficient"))
    monkeypatch.setattr(prep, "model", lambda step, payload, settings: output(step))
    app = create_app(str(db), language="ko")
    return TestClient(app), app.state.drafts, db


def wait_job(drafts):
    deadline = time.monotonic() + 5
    while drafts.get("demo")["job"]["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.01)
    return drafts.get("demo")


def generate(client, drafts, step, selected=None):
    data = {"step": step, "revision": drafts.get("demo")["revision"]}
    if selected is not None:
        data["selected"] = selected
    response = client.post("/companies/demo/prepare/generate", data=data)
    assert response.status_code == 200, response.text
    saved = wait_job(drafts)
    assert saved["job"]["state"] == "done", saved["job"]
    return saved


def test_login_required_on_new_routes(tmp_path, monkeypatch):
    cfg = replace(
        config.Config(),
        server=config.ServerSettings(token="test-token"),
        preparation=config.PreparationSettings(enabled=True),
    )
    monkeypatch.setattr(config, "load", lambda *args: cfg)
    with TestClient(create_app(str(tmp_path / "auth.db")), follow_redirects=False) as client:
        assert client.get("/companies/demo/prepare").status_code == 303
        assert client.get("/preparation/notifications").status_code == 303
        assert (
            client.post(
                "/companies/demo/prepare/generate", data={"step": "email", "revision": 1}
            ).status_code
            == 303
        )


@pytest.mark.parametrize("change", ["unknown", "legacy_source", "uncited", "empty", "wrong_type"])
def test_missing_or_fabricated_source_rejected(change):
    result = output("proposal")
    if change == "unknown":
        result["source_ids"] += ["M99"]
    elif change == "legacy_source":
        result["source_ids"] = ["M1", "I1"]
    elif change == "uncited":
        result["text"] = result["text"].replace("[M2]", "")
    elif change == "empty":
        result["text"] = ""
    else:
        result["source_ids"] = {"M1": "I1"}
    with pytest.raises(prep.PrepError):
        prep.validate_output("proposal", result, SOURCES)


def test_retrieval_uses_only_neutral_kb_without_implicit_model_calls(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return json.dumps(
                {
                    "hits": [
                        {"id": "excluded", "tier": "excluded", "similarity": 0.99},
                        {"id": "weak", "tier": "other", "similarity": 0.6},
                        {
                            "id": "eligible",
                            "title": "public",
                            "tier": "exact",
                            "similarity": 0.9,
                            "market_note": "Insider native SMS unavailable",
                            "evidence": [{"title": "source", "url": "https://example.com/public"}],
                        },
                    ]
                }
            )

    def urlopen(request, **kwargs):
        body = json.loads(request.data)
        assert body["market"] == "kr"
        assert body["translate"] is False
        assert body["channels"] == ["ch:web_onsite"]
        assert "vendor" not in body
        return Response()

    monkeypatch.setattr(prep, "urlopen", urlopen)
    monkeypatch.setattr(
        prep, "command", lambda *args, **kwargs: pytest.fail("must not call a vendor KB CLI")
    )
    sources = prep.retrieve(
        {"industry": "retail"}, prep.empty_document()["inputs"], config.PreparationSettings()
    )
    assert [s["id"] for s in sources] == ["M1"]
    assert "native SMS unavailable" not in json.dumps(sources)


def test_model_uses_no_tools_or_memory_and_accepts_cli_array(monkeypatch):
    def command(args, *other, **kwargs):
        assert args[args.index("--tools") + 1] == ""
        assert "--disable-slash-commands" in args
        assert "--safe-mode" in args
        assert "--strict-mcp-config" in args
        assert "--no-session-persistence" in args
        assert json.loads(args[args.index("--settings") + 1])["disableAllHooks"] is True
        assert kwargs["prompt"]
        assert kwargs["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
        return [{"type": "system"}, {"type": "result", "structured_output": output("email")}]

    monkeypatch.setattr(prep, "command", command)
    assert prep.model("email", {}, config.PreparationSettings()) == output("email")


def test_timeout_terminates_process_group():
    import sys

    with pytest.raises(prep.PrepError, match="제한 시간"):
        prep.command([sys.executable, "-c", "import time; time.sleep(10)"], 0.05)


def test_dashboard_only_enable_switch(monkeypatch):
    monkeypatch.setenv("RADAR_PREPARATION_ENABLED", "1")
    assert config.PreparationSettings().enabled
    monkeypatch.delenv("RADAR_PREPARATION_ENABLED")
    assert not config.PreparationSettings().enabled


def test_footer_guidance_is_only_once(env):
    client, _, _ = env
    html = client.get("/").text
    assert html.count("실제 사용 여부와 운영 의도는 고객에게 확인하세요") == 1
    assert "실제 계약·사용 중단·교체 여부는 고객에게 확인하세요" not in html


@pytest.mark.parametrize(
    "brand", ["Insider", "Architect", "Braze Canvas", "Salesforce", "인사이더", "Smart Recommender"]
)
def test_generated_copy_cannot_choose_a_vendor(brand):
    result = output("proposal")
    result["text"] += f" {brand}로 구현합니다."
    with pytest.raises(prep.PrepError, match="선택한 제안 기준"):
        prep.validate_output("proposal", result, SOURCES)


def test_one_canonical_pattern_is_valid_without_vendor_implementation_citation():
    result = {
        "text": "고객 행동에 따라 후속 메시지를 분기하는 여정 자동화 시나리오를 검토합니다. [M1]",
        "source_ids": ["M1"],
    }
    prep.validate_output("proposal", result, SOURCES)


def test_selected_function_drives_retrieval_and_requires_matching_kb_capability(monkeypatch):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return json.dumps(
                {
                    "hits": [
                        {
                            "id": "uc:journey",
                            "title": "journey",
                            "similarity": 0.9,
                            "axes": {"requires_capabilities": ["cap:journey_orchestration"]},
                            "evidence": [
                                {
                                    "title": "vendor origin",
                                    "vendor": "any-provider",
                                    "url": "https://example.com/pattern",
                                }
                            ],
                        }
                    ]
                }
            )

    def urlopen(request, **kwargs):
        calls.append(json.loads(request.data))
        return Response()

    monkeypatch.setattr(prep, "urlopen", urlopen)
    inputs = {
        **prep.empty_document()["inputs"],
        "capabilities": ["journey_orchestration"],
        "description": "공개 설명",
    }
    sources = prep.retrieve({"industry": "retail"}, inputs, config.PreparationSettings())
    assert len(calls) == 2
    assert "journey orchestration" in calls[1]["query"]
    assert "공개 설명" in calls[0]["query"]
    assert sources[0]["capabilities"] == ["cap:journey_orchestration"]
    inputs["capabilities"] = ["recommendation"]
    with pytest.raises(prep.PrepError, match="추천"):
        prep.retrieve({"industry": "retail"}, inputs, config.PreparationSettings())


def test_sms_is_not_globally_excluded_by_old_provider_market_policy(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return json.dumps(
                {
                    "hits": [
                        {
                            "id": "uc:sms",
                            "title": "SMS reminder",
                            "similarity": 0.9,
                            "tier": "excluded",
                            "market_note": "native SMS unavailable",
                            "axes": {"channels": [{"id": "ch:sms"}]},
                            "evidence": [
                                {"title": "Public pattern", "url": "https://example.com/sms"}
                            ],
                        }
                    ]
                }
            )

    monkeypatch.setattr(prep, "urlopen", lambda *args, **kwargs: Response())
    sources = prep.retrieve(
        {"industry": "retail"},
        {**prep.empty_document()["inputs"], "channels": ["sms"]},
        config.PreparationSettings(),
    )
    assert sources[0]["channels"] == ["ch:sms"]
    assert "unavailable" not in sources[0]["market_note"]
    with pytest.raises(prep.PrepError, match="근거를 찾지"):
        prep.retrieve(
            {"industry": "retail"}, prep.empty_document()["inputs"], config.PreparationSettings()
        )


def test_new_company_needs_no_manual_settings_and_pair_exports(env):
    client, drafts, db = env
    with Store(db) as s:
        before = s.data_state()
    page = client.get("/companies/demo/prepare")
    assert page.status_code == 200 and "회사 조사·추천받기" in page.text
    assert 'name="sender_context"' in page.text and 'name="vendor"' in page.text
    saved = generate(client, drafts, "usecases")
    assert saved["revision"] == 1 and saved["document"]["selected_indices"] == []
    assert "PRIVATE_NOTE" not in json.dumps(saved)
    assert "다른 기업" not in json.dumps(saved, ensure_ascii=False)
    assert "유사 업종" in client.get("/companies/demo/prepare").text
    saved = generate(client, drafts, "package", [0, 2])
    assert saved["document"]["selected_indices"] == [0, 2]
    assert client.get("/companies/demo/prepare/print").status_code == 200
    assert "관련 논의" in client.get("/companies/demo/prepare/export/email").text
    assert "https://example.com/case" in client.get("/companies/demo/prepare/export/proposal").text
    assert client.get("/companies/demo/prepare/history/1").status_code == 200
    assert client.get("/companies/second/prepare/history/1").status_code == 404
    html = client.get("/companies/demo/prepare").text
    assert 'name="proposal"' not in html and "수정 저장" not in html
    assert "상품 탐색 지원" not in client.get("/companies/second/prepare").text
    with Store(db) as s:
        assert s.data_state() == before
        assert s.conn.execute("SELECT COUNT(*) FROM scan").fetchone()[0] == 0
        backup = s.backup(Path(db).with_name("backup.db"))
    assert prep.Drafts(backup).get("demo")["document"] == saved["document"]


def test_regeneration_blocks_old_exports_and_keeps_history(env):
    client, drafts, _ = env
    generate(client, drafts, "usecases")
    before = generate(client, drafts, "package", [0])
    after = generate(client, drafts, "usecases")
    assert after["document"]["proposal"] == before["document"]["proposal"]
    assert set(after["document"]["stale"]) == {"proposal", "email"}
    assert client.get("/companies/demo/prepare/export/proposal").status_code == 409
    assert client.get("/companies/demo/prepare/export/email").status_code == 409
    assert client.get("/companies/demo/prepare/print").status_code == 409
    assert "웹사이트 경험 개선" in client.get("/companies/demo/prepare/history/2").text


@pytest.mark.parametrize("selected", [[], [-1], [3], [0, 0], [0, 1, 2, 3], ["invalid"]])
def test_selection_must_belong_to_current_recommendations(env, selected):
    client, drafts, _ = env
    before = generate(client, drafts, "usecases")
    response = client.post(
        "/companies/demo/prepare/generate",
        data={"step": "package", "revision": before["revision"], "selected": selected},
    )
    assert response.status_code == 409
    assert drafts.get("demo")["revision"] == before["revision"]


def test_old_edit_and_single_document_routes_are_disabled(env):
    client, drafts, _ = env
    generate(client, drafts, "usecases")
    before = generate(client, drafts, "package", [1])
    for step in ("proposal", "email", "inputs", "usecases"):
        assert (
            client.post(
                "/companies/demo/prepare/save",
                data={"stage": step, "revision": before["revision"], "text": "overwrite"},
            ).status_code
            == 409
        )
    for step in ("proposal", "email"):
        assert (
            client.post(
                "/companies/demo/prepare/generate",
                data={"step": step, "revision": before["revision"]},
            ).status_code
            == 409
        )
    assert drafts.get("demo")["document"] == before["document"]
    assert (
        client.post(
            "/companies/demo/prepare/generate",
            headers={"Origin": "https://evil.test"},
            data={"step": "usecases", "revision": 2},
        ).status_code
        == 403
    )
    assert client.get("/companies/absent/prepare").status_code == 404
    assert client.get("/companies/absent/prepare/status").status_code == 404


def test_pair_is_atomic_on_invalid_email_and_concurrent_revision(env, monkeypatch):
    client, drafts, _ = env
    generate(client, drafts, "usecases")
    before = generate(client, drafts, "package", [0])

    def invalid(step, payload, settings):
        result = output(step)
        result["proposal"]["text"] += " 새 제안"
        result["email"]["source_ids"] = ["M99"]
        return result

    monkeypatch.setattr(prep, "model", invalid)
    response = client.post(
        "/companies/demo/prepare/generate",
        data={"step": "package", "revision": before["revision"], "selected": 1},
    )
    assert response.status_code == 200
    after = wait_job(drafts)
    assert after["job"]["state"] == "failed"
    assert after["document"] == before["document"] and after["revision"] == before["revision"]
    assert (
        client.post(
            "/companies/demo/prepare/generate", data={"step": "usecases", "revision": 0}
        ).status_code
        == 409
    )


def test_generation_lock_and_failure_keep_saved_pair(env, monkeypatch):
    client, drafts, db = env
    generate(client, drafts, "usecases")
    before = generate(client, drafts, "package", [0])
    entered, release = threading.Event(), threading.Event()

    def slow(*args):
        entered.set()
        release.wait(3)
        raise RuntimeError("PRIVATE_ERROR")

    monkeypatch.setattr(prep, "generate", slow)
    drafts.submit(
        "demo",
        before["revision"],
        "package",
        {},
        config.PreparationSettings(),
        selected_indices=[1],
    )
    assert entered.wait(2)
    try:
        with pytest.raises(prep.PrepError, match="다른 초안"):
            prep.Drafts(db).acquire()
        with pytest.raises(prep.PrepError, match="생성 중"):
            drafts.save("demo", before["revision"], before["document"])
        prep.Drafts(db).initialize()
        assert drafts.get("demo")["job"]["state"] == "running"
        assert client.get("/companies/demo/prepare/export/email").status_code == 409
        assert client.get("/health").status_code == 200
    finally:
        release.set()
    after = wait_job(drafts)
    assert after["job"]["state"] == "failed"
    assert "PRIVATE_ERROR" not in json.dumps(after)
    assert after["document"] == before["document"]
    drafts.acquire().close()


def test_restart_marks_orphan_failed_without_paid_retry(env):
    _, drafts, db = env
    with drafts.connect() as conn:
        conn.execute(
            "INSERT INTO preparation_job(target_id,step,state,revision,started_at) VALUES ('demo','usecases','running',0,?)",
            (prep.now(),),
        )
    prep.Drafts(db).initialize()
    assert drafts.get("demo")["job"]["state"] == "failed"
    assert drafts.get("demo")["revision"] == 0


def test_old_documents_preserved_but_must_be_recommended_again(env):
    client, drafts, _ = env
    old = prep.empty_document()
    old.update(
        generation_policy="vendor_neutral_v2",
        usecases=output("usecases")["usecases"],
        sources=SOURCES,
        proposal="기존에 직접 수정한 제안",
    )
    drafts.save("demo", 0, old)
    assert drafts.get("demo")["document"]["legacy_policy"]
    assert "이전 기준" in client.get("/companies/demo/prepare").text
    assert client.get("/companies/demo/prepare/print").status_code == 409
    generate(client, drafts, "usecases")
    assert "기존에 직접 수정한 제안" in client.get("/companies/demo/prepare/history/1").text
    assert not drafts.get("demo")["document"]["legacy_policy"]


def test_model_receives_only_selected_usecases_and_their_sources(env, monkeypatch):
    client, drafts, _ = env
    generate(client, drafts, "usecases")
    doc = drafts.get("demo")["document"]
    doc["usecases"][1]["source_ids"] = ["M1"]
    drafts.save("demo", 1, doc)

    def model(step, payload, settings):
        assert step == "package"
        assert [u["title"] for u in payload["selected_usecases"]] == ["첫 방문 지원"]
        assert [s["id"] for s in payload["sources"]] == ["M1"]
        assert payload["proposed_solution"] is None
        assert all("links" not in s for s in payload["sources"])
        r = output(step)
        for part in r.values():
            part["source_ids"] = ["M1"]
            part["text"] = part["text"].replace(" [M2]", "")
        return r

    monkeypatch.setattr(prep, "model", model)
    generate(client, drafts, "package", [1])


def test_company_based_recommendation_requires_web_and_kb_citations():
    result = output("usecases")
    result["usecases"][0]["basis"] = "company_research"
    with pytest.raises(prep.PrepError, match="회사 조사"):
        prep.validate_output("usecases", result, SOURCES)
    result["usecases"][0]["source_ids"].append("W1")
    result["usecases"][0]["reason"] += " 회사 공개 자료를 참고했습니다. [W1]"
    prep.validate_output("usecases", result, SOURCES + [{"id": "W1", "kind": "기업 공개 자료"}])


def research_events():
    excerpt = "회원의 첫 구매와 재방문을 돕는 멤버십 프로그램을 운영합니다."
    return [
        {
            "message": {
                "content": [
                    {"type": "tool_use", "id": "s", "name": "WebSearch", "input": {"query": "test"}}
                ]
            }
        },
        {
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "s", "content": "search"}]
            },
            "tool_use_result": {"results": [{"content": [{"url": "https://example.com/news"}]}]},
        },
        {
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "f",
                        "name": "WebFetch",
                        "input": {"url": "https://example.com/news"},
                    }
                ]
            }
        },
        {
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "f",
                        "content": excerpt + " 운영 세부 사항은 고객 문의로 확인해야 합니다. " * 4,
                    }
                ]
            },
            "tool_use_result": {"code": 200},
        },
        {
            "type": "result",
            "structured_output": {
                "sources": [
                    {
                        "title": "공식 멤버십",
                        "url": "https://example.com/news",
                        "summary": "회원 프로그램이 공개되어 있습니다.",
                        "excerpt": excerpt,
                        "published_at": "2026-10-01",
                    }
                ],
                "queries": ["membership onboarding", "customer retention"],
            },
        },
    ]


@pytest.mark.parametrize(
    "defect", ["none", "unsearched", "unread", "failed", "fake_excerpt", "bad_result", "no_search"]
)
def test_research_rejects_unverified_provenance(defect):
    events = research_events()
    if defect == "unsearched":
        events[1]["tool_use_result"]["results"] = []
    if defect == "unread":
        events[2]["message"]["content"][0]["input"]["url"] = "https://example.com/other"
    if defect == "failed":
        events[3]["tool_use_result"]["code"] = 403
    if defect == "fake_excerpt":
        events[-1]["structured_output"]["sources"][0]["excerpt"] = (
            "확인된 적 없는 마케팅 계획이 존재합니다."
        )
    if defect == "bad_result":
        events[-1]["is_error"] = True
    if defect == "no_search":
        events = events[2:]
    result = research.verified_research(events)
    assert (result["state"] == "found") == (defect == "none")
    assert bool(result["sources"]) == (defect == "none")


def test_known_company_url_can_be_opened_directly_after_search():
    events = research_events()
    events[1]["tool_use_result"]["results"] = []
    assert research.verified_research(events, ["https://example.com/news"])["state"] == "found"


def test_research_failure_is_labeled_and_never_fabricated(monkeypatch):
    def fail(*args, **kwargs):
        raise prep.PrepError("connection failed")

    monkeypatch.setattr(prep, "command", fail)
    result = research.research(
        {"company": "test", "industry": "retail", "urls": ["https://example.com"]},
        config.PreparationSettings(),
    )
    assert result["state"] == "unavailable" and not result["sources"]


def test_web_research_tools_are_isolated_and_input_allowlisted(monkeypatch):
    def command(args, *other, **kwargs):
        assert args[args.index("--tools") + 1] == "WebSearch,WebFetch"
        assert args[args.index("--allowedTools") + 1] == "WebSearch,WebFetch"
        assert "--safe-mode" in args and "--strict-mcp-config" in args
        payload = json.loads(kwargs["prompt"])
        assert set(payload) == {"company", "industry", "urls", "as_of"}
        assert "PRIVATE" not in kwargs["prompt"]
        return research_events()

    monkeypatch.setattr(prep, "command", command)
    result = research.research(
        {
            "company": "test",
            "industry": "retail",
            "urls": ["https://example.com"],
            "note": "PRIVATE",
        },
        config.PreparationSettings(),
    )
    assert result["state"] == "found"


def test_automatic_retrieval_balances_multiple_directions(monkeypatch):
    calls = []

    class Response:
        def __init__(self, n):
            self.n = n

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return json.dumps(
                {
                    "hits": [
                        {
                            "id": f"q{self.n}-{i}",
                            "title": f"Pattern {self.n}-{i}",
                            "similarity": 0.9,
                            "evidence": [
                                {
                                    "title": "Public source",
                                    "url": f"https://example.com/q{self.n}/{i}",
                                }
                            ],
                        }
                        for i in range(10)
                    ]
                }
            )

    def lookup(request, **kwargs):
        calls.append(json.loads(request.data))
        return Response(len(calls))

    monkeypatch.setattr(prep, "urlopen", lookup)
    sources = prep.retrieve(
        {"industry": "retail", "research_queries": ["retention", "onboarding", "recommendation"]},
        {**prep.empty_document()["inputs"], "automatic": True, "channels": []},
        config.PreparationSettings(),
    )
    assert len(calls) == 3
    assert len(sources) == 10
    assert [s["key"] for s in sources[:6]] == ["q1-0", "q2-0", "q3-0", "q1-1", "q2-1", "q3-1"]


def test_failed_research_is_visible_and_email_subject_not_duplicated(env, monkeypatch):
    client, drafts, _ = env
    monkeypatch.setattr(research, "research", lambda *args: research.fallback("unavailable"))
    generate(client, drafts, "usecases")
    assert "웹 조사를 완료하지 못해" in client.get("/companies/demo/prepare").text

    def model(*args):
        result = output("package")
        result["email"]["text"] = "제목: 중복 제목\n\n" + result["email"]["text"]
        return result

    monkeypatch.setattr(prep, "model", model)
    doc = generate(client, drafts, "package", [0])["document"]
    assert doc["email"]["text"].startswith("안녕하세요.")


@pytest.mark.parametrize('vendor,name,context', [('unknown','',''), ('custom','',''), ('custom','a\nb',''), ('braze','','x'*4001)])
def test_invalid_sender_rejected(env, vendor, name, context):
    client, drafts, _ = env
    response = client.post('/companies/demo/prepare/generate', data={
        'step':'usecases', 'revision':0, 'vendor':vendor, 'sender_name':name, 'sender_context':context})
    assert response.status_code == 409
    assert drafts.get('demo')['job'] is None


def test_sender_is_persisted_isolated_and_used_for_both_steps(env, monkeypatch):
    client, drafts, _ = env
    payloads, searches = [], []
    def model(step, payload, settings):
        payloads.append(copy.deepcopy(payload))
        result = output(step)
        if step == 'package':
            result['email']['text'] += ' Braze로 적용 가능성을 확인하고 싶습니다.'
        return result
    monkeypatch.setattr(prep, 'model', model)
    monkeypatch.setattr(research, 'research', lambda context, settings: searches.append(context) or research.fallback('insufficient'))
    assert client.post('/companies/demo/prepare/generate', data={
        'step':'usecases', 'revision':0, 'vendor':'braze', 'sender_context':'이메일만 제안'}).status_code == 200
    saved = wait_job(drafts)
    assert saved['job']['state'] == 'done'
    assert payloads[0]['proposed_solution'] == 'Braze'
    assert payloads[0]['sender']['context'] == '이메일만 제안'
    assert 'sender' not in searches[0]
    assert drafts.get('second')['document']['sender']['vendor'] == 'neutral'
    assert client.post('/companies/demo/prepare/generate', data={
        'step':'package', 'revision':saved['revision'], 'selected':[0], 'vendor':'insider'}).status_code == 200
    saved = wait_job(drafts)
    assert saved['job']['state'] == 'done'
    assert payloads[-1]['proposed_solution'] == 'Braze'
    assert 'Braze' in saved['document']['email']['text']


def test_sender_change_failure_blocks_old_documents_and_preserves_history(env, monkeypatch):
    client, drafts, _ = env
    generate(client, drafts, 'usecases')
    previous = generate(client, drafts, 'package', [0])
    def fail(*args):
        raise prep.PrepError('생성 실패')
    monkeypatch.setattr(prep, 'model', fail)
    client.post('/companies/demo/prepare/generate', data={
        'step':'usecases', 'revision':previous['revision'], 'vendor':'insider'})
    saved = wait_job(drafts)
    assert saved['job']['state'] == 'failed'
    assert saved['document']['sender']['name'] == 'Insider'
    assert set(saved['document']['stale']) == {'usecases','proposal','email'}
    assert client.get('/companies/demo/prepare/export/proposal').status_code == 409
    assert drafts.history('demo', previous['revision'])['sender']['vendor'] == 'neutral'
    assert client.post('/companies/demo/prepare/generate', data={
        'step':'package','revision':saved['revision'],'selected':[0]}).status_code == 409


def test_sender_allows_only_selected_brand():
    from web.preparation_sender import parse_sender
    response = output('email')
    response['text'] += ' Braze Canvas'
    prep.validate_output('email', response, SOURCES, parse_sender('braze'))
    with pytest.raises(prep.PrepError):
        prep.validate_output('email', response, SOURCES, parse_sender('insider'))
    with pytest.raises(prep.PrepError):
        prep.validate_output('email', response, SOURCES)


def test_notifications_latest_per_company_no_private_text(env):
    client, drafts, _ = env
    generate(client, drafts, 'usecases')
    generate(client, drafts, 'package', [0])
    with drafts.connect() as db:
        db.execute("INSERT INTO preparation_job(target_id,step,state,revision,started_at,error) VALUES ('second','usecases','failed',0,?,'PRIVATE_ERROR')", (prep.now(),))
    response = client.get('/preparation/notifications')
    items = response.json()['items']
    assert response.headers['cache-control'] == 'no-store'
    assert len(items) == 2
    assert items[0]['label'] == '회사 조사·추천 실패'
    assert items[1]['url'] == '/companies/demo/prepare#documents'
    assert 'PRIVATE' not in response.text and 'document' not in items[0]
    assert set(items[0]) == {'id','company','state','label','at','url'}


def test_custom_solution_may_include_brand_name():
    from web.preparation_sender import parse_sender
    response = output('email')
    response['text'] += ' HubSpot Marketing Hub를 제안합니다.'
    prep.validate_output('email', response, SOURCES, parse_sender('custom', 'HubSpot Marketing Hub'))
    with pytest.raises(prep.PrepError):
        prep.validate_output('email', response, SOURCES, parse_sender('custom', 'MyHubSpot'))


def test_selected_vendor_evidence_is_prioritized_without_excluding_neutral_patterns(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self):
            return json.dumps({'hits': [
                {'id':f'pattern-{i}', 'title':'Pattern', 'similarity':0.9,
                 'evidence':[{'title':'Case','url':'https://example.com/case', 'vendor':'braze' if i == 11 else 'adobe'}]}
                for i in range(12)]})
    monkeypatch.setattr(prep,'urlopen',lambda *args, **kwargs:Response())
    from web.preparation_sender import parse_sender
    sources = prep.retrieve({'industry':'retail', 'research_queries':['retention'], 'sender':parse_sender('braze')},
        {**prep.empty_document()['inputs'], 'automatic':True, 'channels':[]}, config.PreparationSettings())
    assert sources[0]['key'] == 'pattern-11'
    assert len(sources) == 10 and sources[1]['links'][0]['vendor'] == 'adobe'
