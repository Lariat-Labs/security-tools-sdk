"""HTTP API for the agent runtime (FastAPI). Run with: uvicorn agw_runtime.app:app

Only APIM can call this service: Container Apps authentication accepts tokens from
the gateway's managed identity alone. That is why the ``x-agw-*`` headers, which
APIM sets from the user's validated token, can be trusted here.

    GET  /agents                 agents the caller may use
    POST /agents/{agent}/a2a     Agent2Agent JSON-RPC (message/send), for Copilot Studio
    GET  /agents/{agent}/a2a/.well-known/agent.json   A2A agent card (public)
    POST /agents/{agent}/runs    {"input": "...", "history": [{"role": "user", "content": "..."}]}
    GET  /healthz

Environment:

    AGW_MODEL_BASE_URL   APIM models API, e.g. https://<apim>.azure-api.net/models/openai/v1
    AGW_GATEWAY_SCOPE    api://<gateway-app-id>/.default
    AZURE_CLIENT_ID      client id of the runtime's user-assigned managed identity
    AGW_AGENTS_DIR       optional; defaults to the bundled agents/ directory
    AGW_PUBLIC_BASE_URL  the gateway's public URL, for A2A agent cards (https://<apim>.azure-api.net)
"""

from __future__ import annotations

import dataclasses
import os
from typing import Annotated, Any, Literal

import anyio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from secsdk import telemetry

from . import __version__, a2a
from .agents import AgentDefinition, load_agents
from .loop import AgentRunner, GatewayError, GatewayLimitError, GatewayModelClient, UserContext

Text = Annotated[str, Field(min_length=1, max_length=20_000)]


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: Text


class RunRequest(BaseModel):
    input: Text = Field(description="The user's request")
    history: list[HistoryMessage] = Field(
        default=[], max_length=40, description="Earlier turns; the runtime keeps no state"
    )


def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    headers = {}
    if extra.get("retry_after"):
        headers["Retry-After"] = str(extra["retry_after"])
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, **extra}},
        headers=headers,
    )


def _context(request: Request) -> UserContext | None:
    h = request.headers
    user, tier, team = h.get("x-agw-user", ""), h.get("x-agw-tier", ""), h.get("x-agw-team", "")
    if not (user and tier and team):
        return None
    allowed = tuple(a for a in h.get("x-agw-allowed-agents", "").split(",") if a)
    client = h.get("x-agw-client", "")[:64]
    return UserContext(
        user=user,
        tier=tier,
        team=team,
        run_id=h.get("x-agw-run", ""),
        allowed_agents=allowed,
        session=h.get("x-agw-session", "")[:64],
        cost_center=h.get("x-agw-cost-center", "")[:12],
        user_name=h.get("x-agw-user-name", "")[:200],
        user_upn=h.get("x-agw-user-upn", "")[:200],
        client_app=client,
        auth_flow="obo" if client and client.lower() in _obo_clients() else "delegated",
    )


def _obo_clients() -> set[str]:
    """Client ids of the middle tiers (web chat, Teams bot) that call the gateway on-behalf-of users."""
    return {
        c.strip().lower() for c in os.environ.get("AGW_OBO_CLIENT_IDS", "").split(",") if c.strip()
    }


def _runner_from_env() -> AgentRunner:
    base_url = os.environ.get("AGW_MODEL_BASE_URL")
    scope = os.environ.get("AGW_GATEWAY_SCOPE")
    if not base_url or not scope:
        raise RuntimeError("AGW_MODEL_BASE_URL and AGW_GATEWAY_SCOPE must be set")
    return AgentRunner(GatewayModelClient(base_url, scope))


def create_app(
    runner: AgentRunner | None = None, agents: dict[str, AgentDefinition] | None = None
) -> FastAPI:
    app = FastAPI(title="Agent gateway runtime", version=__version__)
    catalog = agents if agents is not None else load_agents()
    state: dict[str, AgentRunner] = {}
    if runner is not None:
        state["runner"] = runner

    def get_runner() -> AgentRunner:
        if "runner" not in state:  # built on first use so the module imports without Azure
            state["runner"] = _runner_from_env()
        return state["runner"]

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "agents": sorted(catalog)}

    @app.get("/agents")
    @app.get("/agents/", include_in_schema=False)
    def list_agents(request: Request) -> Any:
        ctx = _context(request)
        if ctx is None:
            return _error(401, "missing_context", "Call this service through the agent gateway.")
        return [
            {"name": a.name, "description": a.description}
            for name, a in sorted(catalog.items())
            if name in ctx.allowed_agents
        ]

    @app.post("/agents/{agent}/runs")
    def run_agent(agent: str, body: RunRequest, request: Request) -> Any:
        ctx = _context(request)
        if ctx is None or not ctx.run_id:
            return _error(401, "missing_context", "Call this service through the agent gateway.")
        definition = catalog.get(agent)
        if definition is None:
            return _error(404, "unknown_agent", f"No agent named '{agent}'.")
        if agent not in ctx.allowed_agents:  # APIM checks this too
            return _error(403, "not_entitled", "You don't have access to this agent.")
        try:
            result = get_runner().run(
                definition, ctx, body.input, [m.model_dump() for m in body.history]
            )
        except GatewayLimitError as exc:
            return _error(
                exc.status, exc.code, exc.message, limit=exc.limit, retry_after=exc.retry_after
            )
        except GatewayError as exc:
            status = exc.status if 400 <= exc.status < 500 else 502
            return _error(status, exc.code, exc.message)
        return result.to_dict()

    # ----- Agent2Agent (Copilot Studio) --------------------------------------------------------

    def public_endpoint(agent: str) -> str:
        base = os.environ.get("AGW_PUBLIC_BASE_URL", "").rstrip("/")
        return f"{base}/agents/{agent}/a2a"

    @app.get("/agents/{agent}/a2a/.well-known/agent.json")
    @app.get("/agents/{agent}/a2a/.well-known/agent-card.json")
    def a2a_card(agent: str) -> Any:
        definition = catalog.get(agent)
        if definition is None:
            return _error(404, "unknown_agent", f"No agent named '{agent}'.")
        return a2a.agent_card(definition, public_endpoint(agent))

    @app.post("/agents/{agent}/a2a")
    async def a2a_message(agent: str, request: Request) -> Any:
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(a2a.error(None, a2a.PARSE_ERROR, "Body is not JSON."))
        try:
            request_id, text, history, context_id = a2a.parse_request(body)
        except a2a.A2AError as exc:
            rid = body.get("id") if isinstance(body, dict) else None
            return JSONResponse(a2a.error(rid, exc.code, exc.message))

        ctx = _context(request)
        if ctx is None or not ctx.run_id:
            return _error(401, "missing_context", "Call this service through the agent gateway.")
        definition = catalog.get(agent)
        if definition is None:
            return JSONResponse(
                a2a.error(request_id, a2a.INVALID_PARAMS, f"No agent named '{agent}'.")
            )
        if agent not in ctx.allowed_agents:  # APIM checks this too
            return JSONResponse(
                a2a.reply(request_id, "🔒 You don't have access to this agent.", context_id)
            )
        # One A2A conversation = one gateway session, so the run explorer groups its turns.
        session = a2a.session_from_context(context_id)
        if session:
            ctx = dataclasses.replace(ctx, session=session)

        def run() -> Any:
            return get_runner().run(definition, ctx, text, history)

        try:
            result = await anyio.to_thread.run_sync(run)
        except GatewayLimitError as exc:
            # Answer in the conversation so the user sees why, instead of a protocol error.
            return JSONResponse(a2a.reply(request_id, f"⏳ {exc.message}", context_id))
        except GatewayError as exc:
            return JSONResponse(a2a.error(request_id, a2a.INTERNAL_ERROR, exc.message))
        usage = result.usage
        return JSONResponse(
            a2a.reply(
                request_id,
                result.output,
                context_id,
                metadata={
                    "agw/run_id": result.run_id,
                    "agw/stop_reason": result.stop_reason,
                    "agw/total_tokens": usage.get("total_tokens", 0),
                    "agw/tools": [t.operation or t.name for t in result.tool_calls],
                },
            )
        )

    return app


if os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING") or os.environ.get(
    "OTEL_EXPORTER_OTLP_ENDPOINT"
):
    telemetry.configure(service_name=os.environ.get("OTEL_SERVICE_NAME", "agent-gateway-runtime"))

app = create_app()
