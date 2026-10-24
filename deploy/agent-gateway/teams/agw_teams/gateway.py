"""Async client for the gateway's /agents front door (called with the user's OBO token)."""

from __future__ import annotations

from typing import Any

import httpx


class GatewayClient:
    def __init__(self, base_url: str, http: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = http or httpx.AsyncClient(timeout=300)

    async def _call(
        self, method: str, path: str, token: str, body: Any = None, session: str | None = None
    ) -> tuple[int, Any]:
        headers = {"Authorization": f"Bearer {token}"}
        if session:
            headers["x-agw-session"] = session
        try:
            res = await self.http.request(
                method, f"{self.base_url}/agents{path}", json=body, headers=headers
            )
        except httpx.HTTPError as exc:
            return 502, {"error": {"code": "gateway_unreachable", "message": str(exc)}}
        try:
            payload = res.json()
        except ValueError:
            return res.status_code, {
                "error": {
                    "code": f"http_{res.status_code}",
                    "message": "Unexpected gateway response.",
                }
            }
        if isinstance(payload, dict) and "statusCode" in payload and "message" in payload:
            payload = {"error": {"code": f"http_{res.status_code}", "message": payload["message"]}}
        return res.status_code, payload

    async def agents(self, token: str) -> tuple[int, Any]:
        return await self._call("GET", "/", token)

    async def me(self, token: str) -> tuple[int, Any]:
        """The caller's gateway profile: tier, team, cost center, agents, limits."""
        return await self._call("GET", "/me", token)

    async def run(
        self,
        token: str,
        agent: str,
        text: str,
        history: list[dict[str, str]],
        session: str,
    ) -> tuple[int, Any]:
        return await self._call(
            "POST", f"/{agent}/runs", token, {"input": text, "history": history}, session
        )
