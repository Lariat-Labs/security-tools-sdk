"""HTTP API for the agent runtime (FastAPI). Run with: uvicorn agw_runtime.app:app

Only APIM can call this service: Container Apps authentication accepts tokens from
the gateway's managed identity alone. That is why the ``x-agw-*`` headers, which
APIM sets from the user's validated token, can be trusted here.

    GET  /agents                 agents the caller may use
    POST /agents/{agent}/runs    {"input": "...", "history": [{"role": "user", "content": "..."}]}
    GET  /healthz

Environment:

    AGW_MODEL_BASE_URL   APIM models API, e.g. https://<apim>.azure-api.net/models/openai/v1
    AGW_GATEWAY_SCOPE    api://<gateway-app-id>/.default
    AZURE_CLIENT_ID      client id of the runtime's user-assigned managed identity
    AGW_AGENTS_DIR       optional; defaults to the bundled agents/ directory
"""

from __future__ import annotations

import os
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from secsdk import telemetry

from . import __version__
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
    )


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

    return app


if os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING") or os.environ.get(
    "OTEL_EXPORTER_OTLP_ENDPOINT"
):
    telemetry.configure(service_name=os.environ.get("OTEL_SERVICE_NAME", "agent-gateway-runtime"))

app = create_app()
