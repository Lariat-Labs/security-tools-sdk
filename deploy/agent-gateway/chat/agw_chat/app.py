"""Web chat backend (FastAPI). Run with: uvicorn agw_chat.app:app

    GET  /                 the chat page (MSAL.js sign-in, redirect flow)
    GET  /api/config       public sign-in settings for the page
    GET  /api/whoami       your token for this app vs the OBO token for the gateway (claims only)
    GET  /api/agents       agents you may use (via the gateway, as you)
    POST /api/chat         {"agent": "...", "input": "...", "history": [...]}
    GET  /healthz

Environment:

    AGW_TENANT_ID          Entra tenant id
    AGW_CHAT_APP_ID        this app's registration (client id); users sign in to it
    AGW_GATEWAY_URL        https://<apim>.azure-api.net
    AGW_GATEWAY_APP_ID     the gateway app registration (OBO target)
    AZURE_CLIENT_ID        managed identity used as the federated client credential
    AGW_CHAT_CLIENT_SECRET optional, local development only (instead of the managed identity)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .auth import (
    AuthError,
    OboTokenProvider,
    TokenValidator,
    managed_identity_assertion,
    peek_claims,
)

STATIC = Path(__file__).resolve().parent / "static"
Text = Annotated[str, Field(min_length=1, max_length=20_000)]
NAME = r"^[a-z0-9][a-z0-9-]{0,62}$"


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: Text


class ChatRequest(BaseModel):
    agent: str = Field(pattern=NAME)
    input: Text
    history: list[Turn] = Field(default=[], max_length=40)


class Settings(BaseModel):
    tenant_id: str
    chat_app_id: str
    gateway_url: str
    gateway_app_id: str

    @classmethod
    def from_env(cls) -> Settings:
        try:
            return cls(
                tenant_id=os.environ["AGW_TENANT_ID"],
                chat_app_id=os.environ["AGW_CHAT_APP_ID"],
                gateway_url=os.environ["AGW_GATEWAY_URL"].rstrip("/"),
                gateway_app_id=os.environ["AGW_GATEWAY_APP_ID"],
            )
        except KeyError as exc:
            raise RuntimeError(f"Missing environment variable {exc}") from exc


def _error(
    status: int, code: str, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message}}, headers=headers
    )


def _trace_headers() -> dict[str, str]:
    """W3C traceparent so one trace covers chat -> APIM -> runtime (when telemetry is on)."""
    headers: dict[str, str] = {}
    try:
        from opentelemetry.propagate import inject

        inject(headers)
    except ImportError:
        pass
    return headers


def create_app(
    settings: Settings | None = None,
    validator: TokenValidator | None = None,
    obo: OboTokenProvider | None = None,
    http: httpx.Client | None = None,
) -> FastAPI:
    cfg = settings or Settings.from_env()
    validator = validator or TokenValidator(cfg.tenant_id, cfg.chat_app_id)
    if obo is None:
        secret = os.environ.get("AGW_CHAT_CLIENT_SECRET")
        obo = OboTokenProvider(
            cfg.tenant_id,
            cfg.chat_app_id,
            f"api://{cfg.gateway_app_id}/.default",
            client_secret=secret,
            client_assertion_func=None
            if secret
            else managed_identity_assertion(os.environ.get("AZURE_CLIENT_ID")),
        )
    client = http or httpx.Client(timeout=300)
    app = FastAPI(title="Agent gateway chat", version=__version__)

    def user_token(request: Request) -> tuple[str, dict[str, Any]]:
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            raise AuthError(401, "not_signed_in", "Sign in first.")
        token = header[7:].strip()
        return token, validator.validate(token)

    def gateway(method: str, path: str, token: str, body: Any = None) -> JSONResponse:
        obo_token = obo.token_for(token)
        try:
            res = client.request(
                method,
                f"{cfg.gateway_url}/agents{path}",
                json=body,
                headers={"Authorization": f"Bearer {obo_token}", **_trace_headers()},
            )
        except httpx.HTTPError as exc:
            return _error(502, "gateway_unreachable", str(exc))
        try:
            payload = res.json()
        except ValueError:
            return _error(502, "gateway_error", f"Gateway returned HTTP {res.status_code}.")
        if isinstance(payload, dict) and "statusCode" in payload and "message" in payload:
            # APIM's own error shape -> ours
            payload = {"error": {"code": f"http_{res.status_code}", "message": payload["message"]}}
        headers = {}
        if res.headers.get("retry-after"):
            headers["Retry-After"] = res.headers["retry-after"]
        return JSONResponse(status_code=res.status_code, content=payload, headers=headers)

    @app.exception_handler(AuthError)
    def _auth_error(_request: Request, exc: AuthError) -> JSONResponse:
        return _error(exc.status, exc.code, exc.message)

    @app.middleware("http")
    async def _headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/api/config")
    def config() -> dict[str, str]:
        return {
            "tenantId": cfg.tenant_id,
            "clientId": cfg.chat_app_id,
            "scope": f"api://{cfg.chat_app_id}/access_as_user",
        }

    @app.get("/api/whoami")
    def whoami(request: Request) -> dict[str, Any]:
        token, claims = user_token(request)
        gateway_claims = peek_claims(obo.token_for(token))

        def pick(c: dict[str, Any]) -> dict[str, Any]:
            return {
                "aud": c.get("aud"),
                "scp": c.get("scp"),
                "azp": c.get("azp"),
                "oid": c.get("oid"),
                "groups": len(c.get("groups") or []),
                "roles": c.get("roles"),
            }

        return {
            "name": claims.get("name") or claims.get("preferred_username"),
            "token_for_chat_app": pick(claims),
            "obo_token_for_gateway": pick(gateway_claims),
        }

    @app.get("/api/agents")
    def agents(request: Request) -> JSONResponse:
        token, _ = user_token(request)
        return gateway("GET", "/", token)

    @app.post("/api/chat")
    def chat(body: ChatRequest, request: Request) -> JSONResponse:
        token, _ = user_token(request)
        return gateway(
            "POST",
            f"/{body.agent}/runs",
            token,
            {"input": body.input, "history": [t.model_dump() for t in body.history]},
        )

    return app


def _app_from_env() -> FastAPI:
    if os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        from secsdk import telemetry

        telemetry.configure(service_name=os.environ.get("OTEL_SERVICE_NAME", "agent-gateway-chat"))
    return create_app()


app = _app_from_env() if os.environ.get("AGW_CHAT_APP_ID") else FastAPI()
