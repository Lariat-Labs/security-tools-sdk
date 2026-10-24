"""Tests for the Teams bot: commands, conversation state, gateway client, server (offline)."""

import asyncio
import importlib
import sys
from pathlib import Path

import httpx
import pytest
import respx

GATEWAY_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GATEWAY_DIR / "teams"))

from agw_teams.conversation import (  # noqa: E402
    MAX_HISTORY_TURNS,
    Conversation,
    ConversationStore,
    format_answer,
    format_error,
    parse,
)
from agw_teams.gateway import GatewayClient  # noqa: E402


@pytest.mark.parametrize(
    ("text", "name", "arg"),
    [
        ("help", "help", ""),
        ("<at>Agent Gateway</at> agents", "agents", ""),
        ("use threat-brief", "use", "threat-brief"),
        ("/use CVE-Triage", "use", "cve-triage"),
        ("new", "new", ""),
        ("me", "me", ""),
        ("signout", "signout", ""),
        ("Prioritise CVE-2024-3400", "run", "Prioritise CVE-2024-3400"),
        ("use the force", "run", "use the force"),  # not a valid agent name -> a question
    ],
)
def test_parse(text, name, arg):
    command = parse(text)
    assert (command.name, command.arg) == (name, arg)


def test_conversation_history_session_and_reset():
    convo = ConversationStore().get("19:abc@thread.v2")
    first = convo.session_id
    assert first.startswith("teams-") and len(first) <= 64
    for i in range(MAX_HISTORY_TURNS + 3):
        convo.remember(f"q{i}", f"a{i}")
    assert (
        len(convo.history) == 2 * MAX_HISTORY_TURNS
        and convo.history[-1]["content"] == f"a{MAX_HISTORY_TURNS + 2}"
    )
    convo.reset()
    assert convo.history == [] and convo.session_id != first


def test_session_ids_pass_the_gateway_pattern():
    import re

    sid = Conversation("a:1Xy... weird/chars?").session_id
    assert re.fullmatch(r"[A-Za-z0-9-]{1,64}", sid)


def test_format_answer_and_errors():
    text = format_answer(
        "cve-triage",
        {
            "output": "P1",
            "usage": {"total_tokens": 12580},
            "tool_calls": [{"operation": "kev.lookup"}],
            "stop_reason": "completed",
        },
    )
    assert text.startswith("P1") and "12,580 tokens" in text and "kev.lookup" in text
    quota = format_error(
        403,
        {
            "error": {
                "code": "quota_exceeded",
                "message": "Daily limit.",
                "retry_after": 14042,
                "limit": "user",
            }
        },
    )
    assert "about 4 hours" in quota and "limit: user" in quota
    assert format_error(403, {"error": {"code": "not_entitled", "message": "No."}}).startswith("🔒")


@respx.mock
def test_gateway_client_sends_token_and_session():
    route = respx.post("https://apim.example/agents/cve-triage/runs").mock(
        return_value=httpx.Response(200, json={"output": "ok"})
    )
    respx.get("https://apim.example/agents/").mock(
        return_value=httpx.Response(401, json={"statusCode": 401, "message": "Sign in"})
    )

    async def go():
        client = GatewayClient("https://apim.example/", httpx.AsyncClient())
        ran = await client.run("obo-token", "cve-triage", "hi", [], "teams-123")
        listed = await client.agents("obo-token")
        return ran, listed

    (status, body), (lstatus, lbody) = asyncio.run(go())
    assert status == 200 and body["output"] == "ok"
    sent = route.calls[0].request
    assert sent.headers["authorization"] == "Bearer obo-token"
    assert sent.headers["x-agw-session"] == "teams-123"
    assert lstatus == 401 and lbody["error"]["message"] == "Sign in"  # APIM shape normalised


def test_bot_server_routes_and_health_check(monkeypatch):
    """The real Agents SDK wiring imports, and only /healthz is reachable without a token."""
    pytest.importorskip("microsoft_agents.hosting.aiohttp")
    from aiohttp.test_utils import TestClient, TestServer

    env = {
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__AUTHTYPE": "FederatedCredentials",
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID": "11111111-1111-1111-1111-111111111111",
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID": "22222222-2222-2222-2222-222222222222",
        "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__FEDERATEDCLIENTID": "33333333-3333-3333-3333-333333333333",
        "AGENTAPPLICATION__USERAUTHORIZATION__HANDLERS__GATEWAY__SETTINGS__AZUREBOTOAUTHCONNECTIONNAME": "teams-sso",
        "AGENTAPPLICATION__USERAUTHORIZATION__HANDLERS__GATEWAY__SETTINGS__OBOCONNECTIONNAME": "SERVICE_CONNECTION",
        "AGW_GATEWAY_URL": "https://apim.example",
        "AGW_GATEWAY_APP_ID": "44444444-4444-4444-4444-444444444444",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import agw_teams.bot as bot

    bot = importlib.reload(bot)
    assert bot.GATEWAY_SCOPE == "api://44444444-4444-4444-4444-444444444444/.default"

    async def go():
        async with TestClient(TestServer(bot.create_server())) as client:
            health = await client.get("/healthz")
            anonymous = await client.post("/api/messages", json={"type": "message", "text": "hi"})
            return health.status, await health.json(), anonymous.status

    status, body, anonymous = asyncio.run(go())
    assert status == 200 and body == {"ok": True}
    assert anonymous == 401  # Bot Service must present a token


def test_format_profile():
    from agw_teams.conversation import format_profile

    text = format_profile(
        {
            "user": {"name": "Ada"},
            "tier": "standard",
            "team": "secops",
            "costCenter": "4100",
            "agents": ["cve-triage"],
            "limits": {
                "user": {
                    "tokensPerMinute": 30000,
                    "tokensPerDay": 500000,
                    "tokensPerMonth": 500000,
                },
                "teamTokensPerMonth": 2000000,
            },
        }
    )
    assert "cost center **4100**" in text and "30,000/min" in text and "cve-triage" in text


def test_teams_package(tmp_path):
    import json
    import zipfile

    sys.path.insert(0, str(GATEWAY_DIR / "teams"))
    import package

    app = "55555555-5555-5555-5555-555555555555"
    out = package.build(app, "bot.example.azurecontainerapps.io", tmp_path / "app.zip")
    with zipfile.ZipFile(out) as z:
        assert set(z.namelist()) == {"manifest.json", "color.png", "outline.png"}
        m = json.loads(z.read("manifest.json"))
        assert z.read("color.png").startswith(b"\x89PNG")
    assert m["bots"][0]["botId"] == app == m["id"]
    assert m["webApplicationInfo"] == {"id": app, "resource": f"api://botid-{app}"}
    assert "token.botframework.com" in m["validDomains"]
