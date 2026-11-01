"""OpenAI-compatible API over the agent gateway, for Open WebUI. Started by run_webui.py.

    GET  /v1/models             agents you may use, as models
    POST /v1/chat/completions   runs the agent named by "model" (streaming or not)
    GET  /healthz

Each agent is a model in Open WebUI. A chat turn becomes one agent run: the last user
message is the input and earlier turns are the history (the runtime keeps no state).
Open WebUI's chat id becomes the gateway session, so a conversation's runs group together
in the run explorer workbook.

Two targets:

* ``gateway`` (default): the deployed agents API in APIM, as you (Azure CLI sign-in), so
  your tier's token limits, metrics and usage events apply exactly as in Teams or Copilot
  Studio. Everyone using this Open WebUI acts as you.
* ``local``: a runtime container started by run_local.py, with the x-agw-* headers APIM
  would set. Model calls go straight to Foundry: no gateway limits.

Environment:

    AGW_WEBUI_TARGET     gateway | local
    AGW_GATEWAY_URL      https://<apim>.azure-api.net            (gateway)
    AGW_GATEWAY_APP_ID   gateway app registration client id      (gateway)
    AGW_LOCAL_URL        http://127.0.0.1:8000                   (local)
    AGW_WEBUI_KEY        optional; callers must send it as the bearer token
"""

from __future__ import annotations

import getpass
import hmac
import json
import os
import threading
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import anyio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from secsdk import telemetry

from . import __version__, convert

TOKEN_REFRESH_MARGIN = 300  # seconds before expiry


class GatewayTarget:
    """The deployed agents API (APIM), called with your Azure CLI token."""

    name = "gateway"

    def __init__(self, gateway_url: str, app_id: str, credential: Any = None) -> None:
        self.base = gateway_url.rstrip("/") + "/agents"
        self.scope = f"api://{app_id}/.default"
        if credential is None:
            from azure.identity import AzureCliCredential

            credential = AzureCliCredential()
        self._credential = credential
        self._token: Any = None
        self._lock = threading.Lock()

    def headers(self) -> dict[str, str]:
        with self._lock:  # the Azure CLI credential doesn't cache; reuse until near expiry
            if self._token is None or self._token.expires_on - time.time() < TOKEN_REFRESH_MARGIN:
                self._token = self._credential.get_token(self.scope)
            return {"Authorization": f"Bearer {self._token.token}"}


class LocalTarget:
    """A local runtime container: sends the user context APIM would set, every agent allowed."""

    name = "local"

    def __init__(self, url: str, user: str | None = None) -> None:
        self.url = url.rstrip("/")
        self.base = self.url + "/agents"
        self.user = user or getpass.getuser()
        self._agents: list[str] | None = None

    def headers(self) -> dict[str, str]:
        if self._agents is None:
            self._agents = httpx.get(self.url + "/healthz", timeout=10).json().get("agents", [])
        return {
            "x-agw-user": f"local:{self.user}",
            "x-agw-user-name": self.user,
            "x-agw-tier": "local",
            "x-agw-team": "local",
            "x-agw-allowed-agents": ",".join(self._agents),
            "x-agw-run": str(uuid.uuid4()),  # APIM sets a new one per run
            "x-agw-client": "open-webui-local",
        }


def target_from_env() -> GatewayTarget | LocalTarget:
    if os.environ.get("AGW_WEBUI_TARGET", "gateway") == "local":
        return LocalTarget(os.environ.get("AGW_LOCAL_URL", "http://127.0.0.1:8000"))
    url, app_id = os.environ.get("AGW_GATEWAY_URL"), os.environ.get("AGW_GATEWAY_APP_ID")
    if not url or not app_id:
        raise RuntimeError("Set AGW_GATEWAY_URL and AGW_GATEWAY_APP_ID (or AGW_WEBUI_TARGET=local)")
    return GatewayTarget(url, app_id)


def _openai_error(status: int, message: str, code: str = "invalid_request") -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"message": message, "type": code, "code": code}}
    )


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _span(name: str, attrs: dict[str, Any]) -> Any:
    if not telemetry.enabled():
        return None
    from opentelemetry import trace

    span = trace.get_tracer("agent-gateway-webui").start_span(name)
    for key, value in attrs.items():
        if value is not None:
            span.set_attribute(key, value)
    return span


def _trace_headers(span: Any) -> dict[str, str]:
    """W3C traceparent, so APIM and the runtime join this trace."""
    if span is None:
        return {}
    from opentelemetry import trace
    from opentelemetry.propagate import inject

    headers: dict[str, str] = {}
    inject(headers, context=trace.set_span_in_context(span))
    return headers


def create_app(
    target: GatewayTarget | LocalTarget | None = None,
    http: httpx.AsyncClient | None = None,
    api_key: str | None = None,
) -> FastAPI:
    tgt = target or target_from_env()
    key = api_key if api_key is not None else os.environ.get("AGW_WEBUI_KEY", "")
    client = http or httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10))
    app = FastAPI(title="Agent gateway for Open WebUI", version=__version__)

    def authorized(request: Request) -> bool:
        if not key:
            return True
        sent = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        return hmac.compare_digest(sent, key)

    async def target_headers() -> dict[str, str]:
        return await anyio.to_thread.run_sync(tgt.headers)

    async def run_agent(
        agent: str, user_input: str, history: list[dict[str, str]], request: Request
    ) -> tuple[int, Any]:
        """(status, body) of POST /agents/{agent}/runs."""
        h = request.headers
        span = _span(
            f"open-webui chat {agent}",
            {
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": agent,
                "agw.webui.target": tgt.name,
                "agw.webui.user": h.get("x-openwebui-user-email") or h.get("x-openwebui-user-name"),
                "session.id": convert.session_id(h.get("x-openwebui-chat-id")),
                "agw.history_turns": len(history),
            },
        )
        try:
            headers = {**await target_headers(), **_trace_headers(span)}
            session = convert.session_id(h.get("x-openwebui-chat-id"))
            if session:
                headers["x-agw-session"] = session
            try:
                res = await client.post(
                    f"{tgt.base}/{agent}/runs",
                    json={"input": user_input, "history": history},
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                return 502, {"error": {"code": "gateway_unreachable", "message": str(exc)}}
            try:
                body = res.json()
            except ValueError:
                body = {"error": {"code": f"http_{res.status_code}", "message": res.text[:300]}}
            if span is not None:
                span.set_attribute("http.response.status_code", res.status_code)
                if res.status_code == 200 and isinstance(body, dict):
                    usage = body.get("usage") or {}
                    span.set_attribute("agw.run_id", body.get("run_id", ""))
                    span.set_attribute("gen_ai.usage.input_tokens", usage.get("prompt_tokens", 0))
                    span.set_attribute(
                        "gen_ai.usage.output_tokens", usage.get("completion_tokens", 0)
                    )
                    for name, left in (body.get("limits") or {}).items():
                        span.set_attribute(f"agw.limit.remaining.{name}", left)
                else:
                    span.set_attribute("error.type", f"http_{res.status_code}")
            return res.status_code, body
        except Exception as exc:  # token or network failure: answer in the chat
            if span is not None:
                span.set_attribute("error.type", type(exc).__name__)
            return 502, {"error": {"code": "adapter_error", "message": str(exc)[:500]}}
        finally:
            if span is not None:
                span.end()

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "target": tgt.name, "agents_api": tgt.base}

    @app.get("/v1/models")
    async def models(request: Request) -> Any:
        if not authorized(request):
            return _openai_error(401, "Bad API key.", "invalid_api_key")
        try:
            res = await client.get(tgt.base + "/", headers=await target_headers())
            agents = res.json()
        except Exception as exc:
            return _openai_error(502, f"Couldn't list agents: {exc}", "gateway_error")
        if res.status_code != 200 or not isinstance(agents, list):
            return _openai_error(
                res.status_code if res.status_code >= 400 else 502,
                convert.error_text(res.status_code, agents),
                "gateway_error",
            )
        return convert.model_list(agents)

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Any:
        if not authorized(request):
            return _openai_error(401, "Bad API key.", "invalid_api_key")
        try:
            body = await request.json()
            agent = str(body["model"])
            user_input, history = convert.split_messages(body.get("messages") or [])
        except (ValueError, KeyError, TypeError) as exc:
            return _openai_error(400, str(exc) or "Bad request.")

        cid, created = f"chatcmpl-{uuid.uuid4().hex}", int(time.time())

        async def answer() -> tuple[str, str, dict[str, int]]:
            status, run = await run_agent(agent, user_input, history, request)
            if status == 200 and isinstance(run, dict):
                return run.get("output", ""), convert.run_summary(run), convert.openai_usage(run)
            return convert.error_text(status, run), "", convert.openai_usage(None)

        if not body.get("stream"):
            content, _summary, usage = await answer()
            return convert.completion(cid, agent, created, content, usage)

        async def events() -> AsyncIterator[str]:
            # Shown at once in the thinking panel, so the user knows the run started.
            yield _sse(
                convert.chunk(
                    cid,
                    agent,
                    created,
                    {"role": "assistant", "reasoning_content": f"Running **{agent}**…\n\n"},
                )
            )
            content, summary, usage = await answer()
            if summary:
                yield _sse(convert.chunk(cid, agent, created, {"reasoning_content": summary}))
            yield _sse(convert.chunk(cid, agent, created, {"content": content}))
            yield _sse(convert.chunk(cid, agent, created, {}, finish_reason="stop", usage=usage))
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    return app


def _app_from_env() -> FastAPI:
    if os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING") or os.environ.get(
        "OTEL_EXPORTER_OTLP_ENDPOINT"
    ):
        telemetry.configure(service_name=os.environ.get("OTEL_SERVICE_NAME", "agent-gateway-webui"))
    return create_app()


app = _app_from_env() if os.environ.get("AGW_WEBUI_TARGET") else FastAPI()
