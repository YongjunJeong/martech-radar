from dataclasses import replace
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient
import pytest

from radar.config import PreparationSettings
from web import (
    preparation as prep,
    preparation_gateway as gateway,
    preparation_research as research,
)
from web.preparation_transport import request


@pytest.fixture
def client(tmp_path, monkeypatch):
    secret = tmp_path / "token"
    secret.write_text("x" * 40)
    settings = PreparationSettings()
    app = gateway.create_app(settings, str(secret), str(tmp_path / "lock"))
    monkeypatch.setattr(gateway, "kb_search", lambda *args: {"hits": [{"id": "example"}]})
    monkeypatch.setattr(gateway.shutil, "which", lambda *args: "/safe/claude")
    return TestClient(app, raise_server_exceptions=False), secret, tmp_path / "lock"


def test_unauthenticated_all_routes_and_validation_redaction(client):
    http, secret, lock = client
    for route in ("health", "search", "research", "model"):
        assert http.post("/v1/" + route, json={}).status_code == 401
    result = http.post(
        "/v1/research",
        headers={"Authorization": "Bearer " + "x" * 40},
        json={"private": "SENSITIVE"},
    )
    assert result.status_code == 422 and "SENSITIVE" not in result.text


def test_health_does_not_call_paid_model(client, monkeypatch):
    http, _, _ = client
    monkeypatch.setattr(prep, "model", lambda *args: pytest.fail("paid invocation"))
    result = http.get("/v1/health", headers={"Authorization": "Bearer " + "x" * 40})
    assert result.json() == {
        "ready": True,
        "kb_search": True,
        "cli_present": True,
        "model_auth": "not_checked",
    }


def test_generation_auth_lock_no_recursion_and_research_privacy(client, monkeypatch):
    http, _, lock = client
    headers = {"Authorization": "Bearer " + "x" * 40}

    def model(step, payload, settings):
        assert not settings.gateway_url
        return {"usecases": []}

    monkeypatch.setattr(prep, "model", model)
    body = {"step": "usecases", "payload": {"company": "Demo"}}
    with gateway.generation_slot(lock):
        assert http.post("/v1/model", headers=headers, json=body).status_code == 429
    assert http.post("/v1/model", headers=headers, json=body).json() == {"usecases": []}
    body["step"] = "shell"
    assert http.post("/v1/model", headers=headers, json=body).status_code == 422
    assert (
        http.post(
            "/v1/research",
            headers=headers,
            json={"company": "Demo", "industry": "retail", "urls": [], "notes": "private"},
        ).status_code
        == 422
    )


def test_gateway_masks_failures_and_enforces_kb_policy(client, monkeypatch):
    http, _, _ = client
    headers = {"Authorization": "Bearer " + "x" * 40}

    def search(settings, body):
        assert body["market"] == "kr" and not body["translate"] and body["include_evidence"]
        raise OSError("SECRET")

    monkeypatch.setattr(gateway, "kb_search", search)
    result = http.post(
        "/v1/search", headers=headers, json={"query": "loyalty", "translate": True, "market": "us"}
    )
    assert result.status_code == 503 and "SECRET" not in result.text


def test_oversized_request_rejected_before_generation(client, monkeypatch):
    http, _, _ = client
    monkeypatch.setattr(prep, "model", lambda *args: pytest.fail("oversized request invoked model"))
    result = http.post(
        "/v1/model",
        headers={"Authorization": "Bearer " + "x" * 40},
        content=b" " * (gateway.MAX_BYTES + 1),
    )
    assert result.status_code == 413


def test_transport_http_and_no_redirect_or_retry(tmp_path):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            calls.append(self.path)
            assert self.headers["Authorization"] == "Bearer " + "x" * 40
            if self.path.endswith("redirect"):
                self.send_response(302)
                self.send_header("Location", "/leak")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"usecases": []}).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    secret = tmp_path / "secret"
    secret.write_text("x" * 40)
    settings = PreparationSettings(
        gateway_url=f"http://127.0.0.1:{server.server_port}", gateway_token_file=str(secret)
    )
    try:
        assert request(settings, "model", {"step": "usecases"}) == {"usecases": []}
        with pytest.raises(prep.PrepError):
            request(settings, "redirect", {})
        assert calls == ["/v1/model", "/v1/redirect"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_routing_and_public_research_only(monkeypatch):
    from web import preparation_transport as transport

    calls = []

    def remote(settings, operation, payload):
        calls.append((operation, payload))
        return {"state": "insufficient", "sources": [], "queries": []}

    monkeypatch.setattr(transport, "request", remote)
    settings = replace(PreparationSettings(), gateway_url="http://private")
    prep.model("usecases", {}, settings)
    research.research(
        {"company": "Demo", "industry": "retail", "urls": [], "secret": "never send"}, settings
    )
    assert calls[0][0] == "model"
    assert set(calls[1][1]) == {"company", "industry", "urls"}
