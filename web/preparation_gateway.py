"""Private authenticated KB/generation gateway (hosted API or legacy CLI).

No DB access, arbitrary commands, email sending, or automatic model retries.
Run separately on the host that owns the KB/CLI; never publish it unauthenticated.
"""

import argparse
from contextlib import contextmanager
from dataclasses import replace
import fcntl
import hmac
import json
import os
from pathlib import Path
import shutil
from urllib.request import Request as URLRequest, urlopen

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field

from radar import config
from . import preparation as prep, preparation_research as research
from .preparation_transport import MAX_BYTES, token


class Search(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=600)
    industry: str | None = Field(default=None, max_length=80)
    channels: list[str] = Field(default_factory=list, max_length=12)
    market: str = "kr"
    top_k: int = Field(default=10, ge=1, le=10)
    include_evidence: bool = True
    translate: bool = False


class ModelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step: str
    payload: dict


class ResearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: str = Field(max_length=200)
    industry: str = Field(max_length=100)
    urls: list[str] = Field(default_factory=list, max_length=10)


@contextmanager
def generation_slot(path):
    # File lock also bounds concurrent CLI calls across server processes.
    with Path(path).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HTTPException(429, "Generation is busy; no request was queued.") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def kb_search(settings, body):
    if settings.mkb_url == "embedded":
        from .preparation_hosted import search
        return search(body)
    req = URLRequest(
        settings.mkb_url.rstrip("/") + "/search",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(req, timeout=25) as response:
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("KB response too large")
    result = json.loads(raw)
    if not isinstance(result, dict) or not isinstance(result.get("hits"), list):
        raise ValueError("Invalid KB response")
    return result


def create_app(settings=None, token_file=None, lock_file=None):
    settings = replace(settings or config.load().preparation, gateway_url="", gateway_token_file="")
    secret = token(token_file or os.environ["RADAR_GATEWAY_TOKEN_FILE"])
    lock_file = lock_file or os.environ.get(
        "RADAR_GATEWAY_LOCK_FILE", "/tmp/radar-preparation-gateway.lock"
    )
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def authorize(authorization: str = Header(default="")):
        if not hmac.compare_digest(authorization.encode(), ("Bearer " + secret).encode()):
            raise HTTPException(401, "Unauthorized")

    @app.middleware("http")
    async def bound_body(request: Request, call_next):
        # Authenticate before reading/parsing even a chunked body.
        if not hmac.compare_digest(
            request.headers.get("authorization", "").encode(), ("Bearer " + secret).encode()
        ):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BYTES:
                return JSONResponse({"detail": "Request too large"}, status_code=413)
        request._body = bytes(body)
        return await call_next(request)

    @app.exception_handler(Exception)
    async def failure(request, exc):
        return JSONResponse(
            {"detail": "Dependency failed; request was not retried."}, status_code=503
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        # Validation details may include private prompt contents.
        return JSONResponse({"detail": "Invalid request"}, status_code=422)

    @app.get("/v1/health", dependencies=[Depends(authorize)])
    def health():
        try:
            result = kb_search(
                settings,
                {
                    "query": "retention customer journey",
                    "market": "kr",
                    "top_k": 1,
                    "include_evidence": True,
                    "translate": False,
                },
            )
            kb = bool(result["hits"])
        except (OSError, ValueError):
            kb = False
        if settings.backend == "anthropic":
            from .preparation_api import credentials
            try:
                credentials(settings)
                configured = True
            except (OSError, ValueError):
                configured = False
            return JSONResponse(
                {"ready": kb and configured, "kb_search": kb, "backend": "anthropic",
                 "api_configured": configured, "model_auth": "not_checked"},
                status_code=200 if kb and configured else 503,
            )
        cli = bool(shutil.which(settings.codex_binary if settings.backend == "codex" else settings.claude_binary))
        # Presence is not a paid authentication/model invocation.
        return JSONResponse(
            {"ready": kb and cli, "kb_search": kb, "cli_present": cli, "model_auth": "not_checked"},
            status_code=200 if kb and cli else 503,
        )

    @app.post("/v1/search", dependencies=[Depends(authorize)])
    def search(body: Search):
        data = body.model_dump()
        data.update(market="kr", translate=False, include_evidence=True)
        try:
            return kb_search(settings, data)
        except (OSError, ValueError):
            raise HTTPException(503, "KB unavailable") from None

    @app.post("/v1/research", dependencies=[Depends(authorize)])
    def investigate(body: ResearchInput):
        with generation_slot(lock_file):
            # Fixed system prompt, search tools and provenance validation.
            return research.research(body.model_dump(), settings)

    @app.post("/v1/model", dependencies=[Depends(authorize)])
    def generate(body: ModelInput):
        if body.step not in prep.STEPS:
            raise HTTPException(422, "Unsupported generation step")
        with generation_slot(lock_file):
            try:
                result = prep.model(body.step, body.payload, settings)
                if settings.backend == "anthropic":
                    result["_generation"] = {"backend": "anthropic", "model": settings.api_model}
                elif settings.backend == "codex":
                    result["_generation"] = {"backend": "codex", "model": settings.codex_model or "codex-default"}
                return result
            except (prep.PrepError, OSError, ValueError):
                raise HTTPException(503, "Generation failed; request was not retried.") from None

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8612)
    args = parser.parse_args()
    import uvicorn

    uvicorn.run(create_app(), host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
