"""Tests for the Open WebUI adapter (offline): message conversion, the OpenAI API, targets."""

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

GATEWAY_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GATEWAY_DIR / "webui"))
sys.path.insert(0, str(GATEWAY_DIR))

import run_webui  # noqa: E402
from agw_webui import convert  # noqa: E402
from agw_webui.app import GatewayTarget, LocalTarget, create_app  # noqa: E402

APIM = "https://apim.example.net"
RUN = {
    "run_id": "r-1",
    "agent": "cve-triage",
    "output": "CVE-2024-3400 is P1.",
    "stop_reason": "completed",
    "usage": {
        "prompt_tokens": 5681,
        "completion_tokens": 1793,
        "total_tokens": 7474,
        "cached_tokens": 3456,
        "model_calls": 4,
    },
    "tool_calls": [
        {
            "name": "intel_cve_priority",
            "operation": "intel.cve_priority",
            "ok": True,
            "error_code": None,
            "ms": 866,
        },
        {
            "name": "nvd_cve",
            "operation": "nvd.cve",
            "ok": False,
            "error_code": "not_found",
            "ms": 40,
        },
    ],
    "limits": {"user_minute": 12526, "user_daily": 292526, "team_monthly": 59990000},
}


# ----- conversion ---------------------------------------------------------------------------


def test_split_messages_takes_last_user_turn_and_drops_system_and_reasoning():
    messages = [
        {"role": "system", "content": "You are helpful"},
        {"role": "user", "content": "Prioritise CVE-2024-3400"},
        {
            "role": "assistant",
            "content": '<details type="reasoning" done="true">\n- `kev.lookup` ✓\n</details>\nIt is P1.',
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "And CVE-2023-4966?"},
                {"type": "image_url", "image_url": {"url": "data:..."}},
            ],
        },
    ]
    user_input, history = convert.split_messages(messages)
    assert user_input == "And CVE-2023-4966?"
    assert history == [
        {"role": "user", "content": "Prioritise CVE-2024-3400"},
        {"role": "assistant", "content": "It is P1."},
    ]


def test_split_messages_caps_history_and_text():
    messages = [{"role": "user", "content": f"q{i}"} for i in range(60)]
    messages[-1] = {"role": "user", "content": "x" * 30_000}
    user_input, history = convert.split_messages(messages)
    assert len(user_input) == convert.MAX_TEXT
    assert len(history) == convert.MAX_HISTORY and history[-1]["content"] == "q58"


def test_split_messages_needs_a_final_user_message():
    with pytest.raises(ValueError):
        convert.split_messages([{"role": "assistant", "content": "hi"}])
    with pytest.raises(ValueError):
        convert.split_messages([])


def test_session_id_only_accepts_gateway_safe_ids():
    assert convert.session_id("0b6c1d2e-aaaa-4bbb-8ccc-123456789abc")
    assert convert.session_id("local:abc") is None
    assert convert.session_id(None) is None


def test_run_summary_lists_tools_tokens_and_limits():
    text = convert.run_summary(RUN)
    assert "`intel.cve_priority` ✓ · 866 ms" in text
    assert "`nvd.cve` ✗ not_found" in text
    assert "**7,474 tokens** (5,681 in, 1,793 out, 3,456 cached) in 4 model calls" in text
    assert "you, this minute: 12,526" in text and "your team, this month: 59,990,000" in text


def test_run_summary_without_limits_says_local():
    assert "No gateway limits" in convert.run_summary({**RUN, "limits": {}, "tool_calls": []})


def test_error_text_by_kind():
    limit = {"error": {"code": "rate_limited", "message": "You've reached your limit."}}
    assert convert.error_text(429, limit) == "⏳ You've reached your limit."
    assert convert.error_text(403, {"error": {"code": "no_tier", "message": "No tier."}}) == (
        "🔒 No tier. (no_tier)"
    )
    apim = {"statusCode": 500, "message": "Internal server error"}
    assert "Internal server error" in convert.error_text(500, apim)


# ----- the API ------------------------------------------------------------------------------


class FakeCredential:
    def __init__(self):
        self.calls = 0

    def get_token(self, scope):
        self.calls += 1
        return SimpleNamespace(token=f"tok-{self.calls}", expires_on=time.time() + 3600)


@pytest.fixture
def gateway():
    cred = FakeCredential()
    target = GatewayTarget(APIM, "app-id", credential=cred)
    return target, cred


def test_models_lists_agents(gateway):
    target, _ = gateway
    with respx.mock:
        route = respx.get(f"{APIM}/agents/").respond(
            json=[{"name": "cve-triage", "description": "Prioritise CVEs"}]
        )
        res = TestClient(create_app(target, api_key="k")).get(
            "/v1/models", headers={"Authorization": "Bearer k"}
        )
    assert res.status_code == 200
    assert res.json()["data"][0]["id"] == "cve-triage"
    assert route.calls[0].request.headers["authorization"] == "Bearer tok-1"


def test_api_key_is_required(gateway):
    target, _ = gateway
    res = TestClient(create_app(target, api_key="k")).get(
        "/v1/models", headers={"Authorization": "Bearer wrong"}
    )
    assert res.status_code == 401


def test_chat_runs_agent_with_history_session_and_reused_token(gateway):
    target, cred = gateway
    chat_id = "0b6c1d2e-aaaa-4bbb-8ccc-123456789abc"
    body = {
        "model": "cve-triage",
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "Prioritise CVE-2024-3400"},
        ],
    }
    with respx.mock:
        route = respx.post(f"{APIM}/agents/cve-triage/runs").respond(json=RUN)
        client = TestClient(create_app(target, api_key=""))
        first = client.post(
            "/v1/chat/completions", json=body, headers={"X-OpenWebUI-Chat-Id": chat_id}
        )
        client.post("/v1/chat/completions", json=body)
    assert first.status_code == 200
    out = first.json()
    assert out["choices"][0]["message"]["content"] == "CVE-2024-3400 is P1."
    assert out["usage"] == {"prompt_tokens": 5681, "completion_tokens": 1793, "total_tokens": 7474}
    sent = route.calls[0].request
    assert json.loads(sent.content) == {
        "input": "Prioritise CVE-2024-3400",
        "history": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
    }
    assert sent.headers["x-agw-session"] == chat_id
    assert "x-agw-session" not in route.calls[1].request.headers
    assert cred.calls == 1  # token reused until near expiry


def test_chat_streams_summary_then_answer(gateway):
    target, _ = gateway
    with respx.mock:
        respx.post(f"{APIM}/agents/cve-triage/runs").respond(json=RUN)
        res = TestClient(create_app(target, api_key="")).post(
            "/v1/chat/completions",
            json={
                "model": "cve-triage",
                "stream": True,
                "messages": [{"role": "user", "content": "CVE-2024-3400"}],
            },
        )
    events = [line[6:] for line in res.text.splitlines() if line.startswith("data: ")]
    assert events[-1] == "[DONE]"
    deltas = [json.loads(e)["choices"][0]["delta"] for e in events[:-1]]
    assert deltas[0]["reasoning_content"].startswith("Running **cve-triage**")
    assert "7,474 tokens" in deltas[1]["reasoning_content"]
    assert deltas[2] == {"content": "CVE-2024-3400 is P1."}
    assert json.loads(events[-2])["usage"]["total_tokens"] == 7474


def test_chat_turns_gateway_limit_into_a_reply(gateway):
    target, _ = gateway
    limit = {
        "error": {
            "code": "rate_limited",
            "message": "Try again in 20 seconds.",
            "limit": "user",
            "retry_after": 20,
        }
    }
    with respx.mock:
        respx.post(f"{APIM}/agents/cve-triage/runs").respond(429, json=limit)
        res = TestClient(create_app(target, api_key="")).post(
            "/v1/chat/completions",
            json={"model": "cve-triage", "messages": [{"role": "user", "content": "x"}]},
        )
    assert res.status_code == 200
    assert res.json()["choices"][0]["message"]["content"] == "⏳ Try again in 20 seconds."


def test_chat_reports_unreachable_gateway(gateway):
    target, _ = gateway
    with respx.mock:
        respx.post(f"{APIM}/agents/cve-triage/runs").mock(side_effect=httpx.ConnectError("down"))
        res = TestClient(create_app(target, api_key="")).post(
            "/v1/chat/completions",
            json={"model": "cve-triage", "messages": [{"role": "user", "content": "x"}]},
        )
    assert "gateway_unreachable" in res.json()["choices"][0]["message"]["content"]


def test_chat_rejects_bad_requests(gateway):
    target, _ = gateway
    res = TestClient(create_app(target, api_key="")).post(
        "/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}]}
    )
    assert res.status_code == 400


def test_local_target_sends_apim_style_context():
    with respx.mock:
        respx.get("http://127.0.0.1:8000/healthz").respond(json={"ok": True, "agents": ["a", "b"]})
        target = LocalTarget("http://127.0.0.1:8000", user="dev")
        first, second = target.headers(), target.headers()
    assert first["x-agw-user"] == "local:dev" and first["x-agw-allowed-agents"] == "a,b"
    assert first["x-agw-tier"] == "local" and first["x-agw-team"] == "local"
    assert first["x-agw-run"] != second["x-agw-run"]  # a new run id per run


def test_webui_env_turns_off_extra_model_calls():
    env = run_webui.webui_env("http://host.docker.internal:8200/v1", "k", 8080)
    assert env["OPENAI_API_BASE_URL"].endswith("/v1") and env["OPENAI_API_KEY"] == "k"
    assert env["ENABLE_FORWARD_USER_INFO_HEADERS"] == "true"
    for name in (
        "ENABLE_TITLE_GENERATION",
        "ENABLE_TAGS_GENERATION",
        "ENABLE_FOLLOW_UP_GENERATION",
        "ENABLE_AUTOCOMPLETE_GENERATION",
    ):
        assert env[name] == "false"
