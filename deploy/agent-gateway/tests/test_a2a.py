"""Tests for the Agent2Agent (A2A) adapter used by Copilot Studio (offline)."""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GATEWAY_DIR / "runtime"))
sys.path.insert(0, str(GATEWAY_DIR))

import render_policies as rp  # noqa: E402
from agw_runtime import a2a  # noqa: E402
from agw_runtime.agents import AgentDefinition, load_agents  # noqa: E402
from agw_runtime.loop import AgentRunner, GatewayLimitError, ModelReply  # noqa: E402

# Shape of a real Copilot Studio request (from Microsoft's CopilotStudioSamples A2A sample).
COPILOT_STUDIO_REQUEST = {
    "jsonrpc": "2.0",
    "id": "2d774031-fdac-4d1b-8b46-bfd7544d1b8e",
    "method": "message/send",
    "params": {
        "message": {
            "contextId": "ee1e68ee-75fc-42bb-83d7-25fd26e559c3",
            "kind": "message",
            "messageId": "573c985e-62b2-cdf3-493a-9f6932b3976d",
            "metadata": {
                "copilotstudio.microsoft.com/a2a/chathistory": [
                    {
                        "HasValue": True,
                        "Value": [
                            {
                                "From": "copilots_header_cr4b0_agent1",
                                "Text": "Hello, I'm an agent.",
                            },
                            {"From": "", "Text": "What is CVE-2023-4966?"},
                            {"From": "copilots_header_cr4b0_agent1", "Text": "It's Citrix Bleed."},
                            {"From": "", "Text": "Prioritise CVE-2024-3400 too\n\n"},
                        ],
                    }
                ]
            },
            "parts": [{"kind": "text", "text": "Prioritise CVE-2024-3400 too"}],
            "role": "user",
        }
    },
}


def test_parse_copilot_studio_request():
    rid, text, history, context = a2a.parse_request(COPILOT_STUDIO_REQUEST)
    assert rid == "2d774031-fdac-4d1b-8b46-bfd7544d1b8e"
    assert text == "Prioritise CVE-2024-3400 too"
    assert context == "ee1e68ee-75fc-42bb-83d7-25fd26e559c3"
    # greeting dropped, current message not duplicated, roles mapped
    assert history == [
        {"role": "user", "content": "What is CVE-2023-4966?"},
        {"role": "assistant", "content": "It's Citrix Bleed."},
    ]


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"hello": 1}, a2a.INVALID_REQUEST),
        ({"jsonrpc": "2.0", "id": 1, "method": "tasks/cancel", "params": {}}, a2a.METHOD_NOT_FOUND),
        ({"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {}}, a2a.INVALID_PARAMS),
        (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "message/send",
                "params": {"message": {"parts": []}},
            },
            a2a.INVALID_PARAMS,
        ),
    ],
)
def test_bad_requests(body, code):
    with pytest.raises(a2a.A2AError) as info:
        a2a.parse_request(body)
    assert info.value.code == code


def test_session_from_context_is_gateway_safe():
    assert (
        a2a.session_from_context("ee1e68ee-75fc-42bb-83d7-25fd26e559c3")
        == "ee1e68ee-75fc-42bb-83d7-25fd26e559c3"
    )
    assert a2a.session_from_context("a b/c") == "abc"
    assert a2a.session_from_context("") == ""


def test_agent_card_for_every_agent():
    for definition in load_agents().values():
        card = a2a.agent_card(definition, f"https://apim.example/agents/{definition.name}/a2a")
        assert card["url"].endswith(f"/agents/{definition.name}/a2a")
        assert (
            card["preferredTransport"] == "JSONRPC" and card["capabilities"]["streaming"] is False
        )
        assert "kev_lookup" in card["skills"][0]["description"]
        assert definition.instructions[:40] not in str(card)  # the prompt is not published


def test_front_door_exposes_only_the_card_anonymously():
    root = ET.fromstring(rp.render_agents_api(rp.load_config(GATEWAY_DIR / "gateway.example.yaml")))
    choose = root.find("./inbound/choose")
    card, otherwise = choose.find("when"), choose.find("otherwise")
    assert "a2a-card" in card.attrib["condition"]
    assert card.find(".//validate-azure-ad-token") is None
    assert card.find("authentication-managed-identity") is not None
    assert otherwise.find("validate-azure-ad-token") is not None  # runs and A2A messages


# ----- the HTTP route -------------------------------------------------------------------------

HEADERS = {
    "x-agw-user": "11111111-2222-3333-4444-555555555555",
    "x-agw-tier": "standard",
    "x-agw-team": "secops",
    "x-agw-run": "run-a2a",
    "x-agw-allowed-agents": "cve-triage",
}


class FakeModel:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def complete(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})  # snapshot
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def answer(text):
    return ModelReply(
        message={"role": "assistant", "content": text},
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        headers={},
    )


def client(model):
    from fastapi.testclient import TestClient

    from agw_runtime.app import create_app

    agents = {
        name: AgentDefinition.model_validate(
            {
                "name": name,
                "description": "d",
                "deployment": "gpt-5-mini",
                "providers": ["kev"],
                "instructions": "Use the tools.",
            }
        )
        for name in ("cve-triage", "threat-brief")
    }
    return TestClient(create_app(AgentRunner(model), agents))


def test_a2a_message_runs_the_agent_with_history_and_session(monkeypatch):
    monkeypatch.setenv("AGW_PUBLIC_BASE_URL", "https://apim.example")
    model = FakeModel(answer("Both are P1."))
    res = client(model).post("/agents/cve-triage/a2a", json=COPILOT_STUDIO_REQUEST, headers=HEADERS)
    body = res.json()
    assert res.status_code == 200 and body["id"] == COPILOT_STUDIO_REQUEST["id"]
    msg = body["result"]
    assert msg["kind"] == "message" and msg["role"] == "agent"
    assert msg["parts"] == [{"kind": "text", "text": "Both are P1."}]
    assert msg["contextId"] == "ee1e68ee-75fc-42bb-83d7-25fd26e559c3"
    assert msg["metadata"]["agw/run_id"] == "run-a2a"
    sent = model.calls[0]
    assert sent["headers"]["x-agw-session"] == "ee1e68ee-75fc-42bb-83d7-25fd26e559c3"  # contextId
    roles = [m["role"] for m in sent["messages"]]
    assert roles == ["system", "user", "assistant", "user"]  # history + current question


def test_a2a_card_is_served(monkeypatch):
    monkeypatch.setenv("AGW_PUBLIC_BASE_URL", "https://apim.example/")
    c = client(FakeModel())
    for path in ("agent.json", "agent-card.json"):
        card = c.get(f"/agents/cve-triage/a2a/.well-known/{path}").json()
        assert (
            card["name"] == "cve-triage"
            and card["url"] == "https://apim.example/agents/cve-triage/a2a"
        )
    assert c.get("/agents/nope/a2a/.well-known/agent.json").status_code == 404


def test_a2a_limits_and_entitlement_answer_in_the_conversation():
    c = client(FakeModel(GatewayLimitError(403, "user", 3600)))
    limited = c.post("/agents/cve-triage/a2a", json=COPILOT_STUDIO_REQUEST, headers=HEADERS).json()
    assert (
        limited["result"]["parts"][0]["text"].startswith("⏳")
        and "3600" in limited["result"]["parts"][0]["text"]
    )
    denied = c.post("/agents/threat-brief/a2a", json=COPILOT_STUDIO_REQUEST, headers=HEADERS).json()
    assert denied["result"]["parts"][0]["text"].startswith("🔒")


def test_a2a_protocol_errors_and_missing_context():
    c = client(FakeModel())
    bad = c.post(
        "/agents/cve-triage/a2a",
        json={"jsonrpc": "2.0", "id": 7, "method": "nope", "params": {}},
        headers=HEADERS,
    ).json()
    assert bad["error"]["code"] == a2a.METHOD_NOT_FOUND and bad["id"] == 7
    assert c.post("/agents/cve-triage/a2a", json=COPILOT_STUDIO_REQUEST).status_code == 401
