"""Tests for the agent runtime: loop, gateway error handling, HTTP API (offline)."""

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import yaml

from secsdk.result import Result

GATEWAY_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GATEWAY_DIR / "runtime"))
sys.path.insert(0, str(GATEWAY_DIR))

from agw_runtime.agents import AgentDefinition, load_agents  # noqa: E402
from agw_runtime.loop import (  # noqa: E402
    AgentRunner,
    GatewayError,
    GatewayLimitError,
    ModelReply,
    UserContext,
    classify_gateway_error,
)

KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
KEV = {
    "vulnerabilities": [
        {
            "cveID": "CVE-2023-4966",
            "vendorProject": "Citrix",
            "product": "NetScaler",
            "dateAdded": "2023-10-18",
            "knownRansomwareCampaignUse": "Known",
        }
    ]
}
CTX = UserContext(
    user="11111111-2222-3333-4444-555555555555",
    tier="standard",
    team="secops",
    run_id="run-1",
    allowed_agents=("cve-triage",),
)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setenv("SECSDK_CACHE_ENABLED", "false")
    monkeypatch.setenv("SECSDK_RATE_LIMIT", "false")


def agent(**overrides: Any) -> AgentDefinition:
    base = {
        "name": "test-agent",
        "description": "test",
        "deployment": "gpt-5-mini",
        "providers": ["kev"],
        "instructions": "Use the tools.",
    }
    return AgentDefinition.model_validate({**base, **overrides})


def tool_call(name: str, args: dict, call_id: str = "call_1") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


class FakeModel:
    """Returns scripted replies (or raises scripted errors) and records every call."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    def complete(self, **kwargs: Any) -> ModelReply:
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def reply(content=None, calls=None, prompt=100, completion=20, headers=None) -> ModelReply:
    message: dict = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return ModelReply(
        message=message,
        usage={
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
        headers=headers or {},
    )


# ----- the loop -------------------------------------------------------------------------------


@respx.mock
def test_run_calls_tools_and_reports_usage_and_limits():
    respx.get(KEV_URL).mock(return_value=httpx.Response(200, json=KEV))
    model = FakeModel(
        reply(calls=[tool_call("kev_lookup", {"cve_id": "CVE-2023-4966"})]),
        reply(
            "CVE-2023-4966 is in KEV.",
            headers={"x-agw-user-daily-remaining": "299000", "x-agw-run-remaining": "299760"},
        ),
    )
    result = AgentRunner(model).run(agent(), CTX, "Is CVE-2023-4966 exploited?")

    assert result.stop_reason == "completed"
    assert result.output == "CVE-2023-4966 is in KEV."
    assert result.usage == {
        "prompt_tokens": 200,
        "completion_tokens": 40,
        "total_tokens": 240,
        "cached_tokens": 0,
        "model_calls": 2,
    }
    assert result.limits == {"user_daily": 299000, "run": 299760}
    assert [(t.name, t.ok) for t in result.tool_calls] == [("kev_lookup", True)]

    first = model.calls[0]
    assert first["model"] == "gpt-5-mini"
    assert first["headers"] == {
        "x-agw-user": CTX.user,
        "x-agw-tier": "standard",
        "x-agw-team": "secops",
        "x-agw-agent": "test-agent",
        "x-agw-run": "run-1",
        "x-agw-session": "run-1",  # no client session: falls back to the run id
    }
    assert {t["function"]["name"] for t in first["tools"]} == {
        "kev_lookup",
        "kev_search",
        "kev_stats",
    }
    tool_msg = model.calls[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "call_1"
    assert json.loads(tool_msg["content"])["data"]["ransomware"] is True


def test_tools_outside_the_agents_providers_are_refused():
    model = FakeModel(
        reply(calls=[tool_call("azure_query", {"query": "Resources"})]), reply("done")
    )
    result = AgentRunner(model).run(agent(), CTX, "query azure")
    assert result.tool_calls[0].error_code == "unknown_operation"
    assert json.loads(model.calls[1]["messages"][-1]["content"])["ok"] is False


def test_bad_tool_arguments_become_an_error_envelope():
    bad = {"id": "c", "type": "function", "function": {"name": "kev_lookup", "arguments": "{nope"}}
    model = FakeModel(reply(calls=[bad]), reply("ok"))
    result = AgentRunner(model).run(agent(), CTX, "x")
    assert result.tool_calls[0].error_code == "invalid_input"


@respx.mock
def test_budget_stops_the_run_before_the_gateway_would():
    respx.get(KEV_URL).mock(return_value=httpx.Response(200, json=KEV))
    call = tool_call("kev_lookup", {"cve_id": "CVE-2023-4966"})
    model = FakeModel(*[reply(calls=[call], prompt=900, completion=50)] * 5)
    result = AgentRunner(model).run(agent(budget_tokens=2000), CTX, "loop forever")
    assert result.stop_reason == "budget_exhausted"
    assert result.usage["total_tokens"] <= 2000
    assert "stopped" in result.output


def test_max_turns():
    call = tool_call("kev_stats", {})
    model = FakeModel(*[reply(calls=[call], prompt=10, completion=5)] * 2)
    runner = AgentRunner(model)
    runner.sdk.execute = lambda op, params: Result(ok=True, operation=op, data={})  # type: ignore[method-assign]
    result = runner.run(agent(max_turns=2), CTX, "x")
    assert result.stop_reason == "max_turns" and len(model.calls) == 2


def test_short_rate_limits_are_waited_out_once():
    slept: list[float] = []
    model = FakeModel(GatewayLimitError(429, "user", 3), reply("done"))
    result = AgentRunner(model, sleep=slept.append).run(agent(), CTX, "x")
    assert result.output == "done" and slept == [3]


@pytest.mark.parametrize(
    "error",
    [
        GatewayLimitError(429, "user", 45),  # too long to wait
        GatewayLimitError(403, "team", 3600),  # quotas are never waited out
    ],
)
def test_long_waits_and_quotas_go_back_to_the_user(error):
    model = FakeModel(error)
    with pytest.raises(GatewayLimitError):
        AgentRunner(model, sleep=lambda s: None).run(agent(), CTX, "x")


def test_classify_gateway_errors():
    err = classify_gateway_error(403, {"X-AGW-Retry-After-Team": "86400"}, None)
    assert isinstance(err, GatewayLimitError)
    assert (err.code, err.limit, err.retry_after) == ("quota_exceeded", "team", 86400)

    err = classify_gateway_error(429, {"x-agw-retry-after-user": "7"}, None)
    assert (err.code, err.limit, err.retry_after) == ("rate_limited", "user", 7)

    err = classify_gateway_error(429, {"retry-after": "2"}, None)
    assert (err.limit, err.retry_after) == ("deployment", 2)

    err = classify_gateway_error(403, {}, {"error": {"code": "model_not_allowed", "message": "no"}})
    assert type(err) is GatewayError and err.code == "model_not_allowed"

    assert classify_gateway_error(500, {}, "boom") is None


# ----- the model client against a fake gateway -------------------------------------------------


class _FakeCredential:
    def get_token(self, *scopes, **kwargs):
        from azure.core.credentials import AccessToken

        return AccessToken("runtime-token", 9999999999)


def _client(handler):
    import httpx2

    from agw_runtime.loop import GatewayModelClient

    return GatewayModelClient(
        "https://apim.example/models/openai/v1",
        "api://gateway/.default",
        credential=_FakeCredential(),
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )


def test_gateway_model_client_sends_identity_and_context():
    seen = {}

    def handler(request):
        import httpx2

        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["agent"] = request.headers["x-agw-agent"]
        seen["body"] = json.loads(request.content)
        return httpx2.Response(
            200,
            headers={"x-agw-user-daily-remaining": "1000"},
            json={
                "id": "c1",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-5-mini",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [tool_call("kev_lookup", {"cve_id": "CVE-1"})],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    out = _client(handler).complete(
        model="gpt-5-mini",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        max_completion_tokens=100,
        headers=CTX.headers("cve-triage"),
    )
    assert seen["url"] == "https://apim.example/models/openai/v1/chat/completions"
    assert seen["auth"] == "Bearer runtime-token"
    assert seen["agent"] == "cve-triage"
    assert seen["body"]["max_completion_tokens"] == 100 and "stream" not in seen["body"]
    assert out.usage["total_tokens"] == 15
    assert out.usage["cached_tokens"] == 0  # absent prompt_tokens_details -> 0
    assert out.message["tool_calls"][0]["function"]["name"] == "kev_lookup"
    assert out.headers["x-agw-user-daily-remaining"] == "1000"


def test_gateway_model_client_maps_quota_errors():
    def handler(request):
        import httpx2

        return httpx2.Response(
            403,
            headers={"x-agw-retry-after-user-month": "120000"},
            json={"statusCode": 403, "message": "Token quota exceeded."},
        )

    with pytest.raises(GatewayLimitError) as info:
        _client(handler).complete(
            model="gpt-5-mini", messages=[], tools=[], max_completion_tokens=1, headers={}
        )
    assert info.value.limit == "user-month" and info.value.code == "quota_exceeded"


# ----- agent definitions vs gateway config ---------------------------------------------------


def test_bundled_agents_match_the_gateway_config():
    agents = load_agents()
    gateway = yaml.safe_load((GATEWAY_DIR / "gateway.example.yaml").read_text())
    assert set(agents) == {a["name"] for a in gateway["agents"]}
    for entry in gateway["agents"]:
        definition = agents[entry["name"]]
        assert definition.deployment in entry["deployments"]
        # The runtime must stop before the gateway's hard per-run limit.
        assert definition.budget_tokens <= entry["tokens_per_run"]
        assert definition.tool_names(), entry["name"]


def test_agent_file_name_must_match(tmp_path):
    (tmp_path / "other.yaml").write_text(
        yaml.safe_dump(agent().model_dump())  # name is test-agent
    )
    with pytest.raises(ValueError, match="must match"):
        load_agents(tmp_path)


# ----- HTTP API -------------------------------------------------------------------------------

HEADERS = {
    "x-agw-user": CTX.user,
    "x-agw-tier": "standard",
    "x-agw-team": "secops",
    "x-agw-run": "run-9",
    "x-agw-allowed-agents": "cve-triage",
}


def _app(model):
    from fastapi.testclient import TestClient

    from agw_runtime.app import create_app

    agents = {"cve-triage": agent(name="cve-triage"), "threat-brief": agent(name="threat-brief")}
    return TestClient(create_app(AgentRunner(model), agents))


def test_api_requires_gateway_context():
    client = _app(FakeModel())
    assert client.get("/agents").status_code == 401
    assert client.post("/agents/cve-triage/runs", json={"input": "x"}).status_code == 401
    assert client.get("/healthz").json()["ok"] is True


def test_api_lists_only_entitled_agents():
    assert [a["name"] for a in _app(FakeModel()).get("/agents", headers=HEADERS).json()] == [
        "cve-triage"
    ]


def test_api_run_and_entitlement():
    model = FakeModel(reply("all good"))
    client = _app(model)
    ok = client.post("/agents/cve-triage/runs", json={"input": "hi"}, headers=HEADERS)
    assert ok.status_code == 200
    body = ok.json()
    assert body["output"] == "all good" and body["run_id"] == "run-9"
    assert model.calls[0]["headers"]["x-agw-agent"] == "cve-triage"

    denied = client.post("/agents/threat-brief/runs", json={"input": "hi"}, headers=HEADERS)
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "not_entitled"
    missing = client.post("/agents/nope/runs", json={"input": "hi"}, headers=HEADERS)
    assert missing.status_code == 404
    invalid = client.post("/agents/cve-triage/runs", json={"input": ""}, headers=HEADERS)
    assert invalid.status_code == 422


def test_api_returns_limit_errors_with_retry_after():
    client = _app(FakeModel(GatewayLimitError(403, "user", 3600)))
    res = client.post("/agents/cve-triage/runs", json={"input": "hi"}, headers=HEADERS)
    assert res.status_code == 403
    assert res.headers["retry-after"] == "3600"
    assert res.json()["error"]["code"] == "quota_exceeded"
    assert res.json()["error"]["limit"] == "user"


# ----- telemetry: correlation attributes and captured content -----------------------------------


@pytest.fixture
def spans():
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(TracerProvider())
    exporter = InMemorySpanExporter()
    trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))
    yield exporter
    exporter.clear()


def _run_with_tool(ctx, tool="kev_lookup", args=None, providers=("kev",)):
    model = FakeModel(
        reply(calls=[tool_call(tool, args or {"cve_id": "CVE-2023-4966"})]), reply("done")
    )
    runner = AgentRunner(model)
    runner.sdk.execute = lambda op, params: Result(ok=True, operation=op, data={})  # type: ignore[method-assign]
    runner.run(agent(providers=list(providers)), ctx, "Is CVE-2023-4966 exploited?")
    return model


def _ours(exporter):
    return [s for s in exporter.get_finished_spans() if s.attributes.get("agw.run_id") == "run-s"]


def test_spans_carry_session_and_agent_ids_without_content(spans, monkeypatch):
    monkeypatch.delenv("AGW_CAPTURE_CONTENT", raising=False)
    ctx = UserContext(user="u1", tier="standard", team="secops", run_id="run-s", session="conv-1")
    model = _run_with_tool(ctx)
    assert model.calls[0]["headers"]["x-agw-session"] == "conv-1"
    ours = _ours(spans)
    names = [s.name for s in ours]
    assert "invoke_agent test-agent" in names and "execute_tool kev_lookup" in names
    for s in ours:
        assert s.attributes["gen_ai.agent.id"] == "test-agent"
        assert s.attributes["gen_ai.conversation.id"] == "conv-1"
        assert s.attributes["session.id"] == "conv-1"
        assert not s.events  # content capture is off by default
    tool = next(s for s in ours if s.name.startswith("execute_tool"))
    assert tool.attributes["gen_ai.tool.call.id"] == "call_1"
    assert tool.attributes["secsdk.operation"] == "kev.lookup"
    assert tool.attributes["agw.tool.outcome"] == "ok"
    chat = next(s for s in ours if s.name.startswith("chat ") and s.attributes["agw.turn"] == 0)
    assert chat.attributes["agw.tools_requested"] == "kev_lookup"


def test_content_capture_records_input_and_tool_arguments(spans, monkeypatch):
    monkeypatch.setenv("AGW_CAPTURE_CONTENT", "true")
    ctx = UserContext(user="u1", tier="standard", team="secops", run_id="run-s", session="conv-2")
    _run_with_tool(ctx)
    ours = _ours(spans)
    run = next(s for s in ours if s.name.startswith("invoke_agent"))
    (event,) = run.events
    assert event.name == "agw.user_input"
    assert event.attributes["agw.input"] == "Is CVE-2023-4966 exploited?"
    assert event.attributes["gen_ai.conversation.id"] == "conv-2"
    tool = next(s for s in ours if s.name.startswith("execute_tool"))
    (args,) = tool.events
    assert args.name == "agw.tool_arguments"
    assert json.loads(args.attributes["gen_ai.tool.call.arguments"]) == {"cve_id": "CVE-2023-4966"}
    assert args.attributes["agw.run_id"] == "run-s"


def test_sensitive_tool_arguments_are_redacted(spans, monkeypatch):
    monkeypatch.setenv("AGW_CAPTURE_CONTENT", "true")
    ctx = UserContext(user="u1", tier="standard", team="secops", run_id="run-s")
    _run_with_tool(
        ctx,
        tool="azure_query",
        args={"query": "Resources | where name has 'prod-db'", "limit": 5},
        providers=("azure",),
    )
    tool = next(s for s in _ours(spans) if s.name.startswith("execute_tool"))
    args = json.loads(tool.events[0].attributes["gen_ai.tool.call.arguments"])
    assert args == {"query": "[redacted]", "limit": 5}


def test_api_passes_session_and_cost_center_through(spans):
    model = FakeModel(reply("ok"))
    _app(model).post(
        "/agents/cve-triage/runs",
        json={"input": "hi"},
        headers={**HEADERS, "x-agw-session": "abc-1", "x-agw-cost-center": "1001"},
    )
    assert model.calls[0]["headers"]["x-agw-session"] == "abc-1"
    run = next(
        s
        for s in spans.get_finished_spans()
        if s.attributes.get("agw.run_id") == "run-9" and s.name.startswith("invoke_agent")
    )
    assert run.attributes["agw.cost_center"] == "1001"


def test_cached_tokens_and_user_names_are_recorded(spans):
    model = FakeModel(reply("ok", prompt=1000, completion=50))
    model.replies[0].usage["cached_tokens"] = 600
    _app(model).post(
        "/agents/cve-triage/runs",
        json={"input": "hi"},
        headers={**HEADERS, "x-agw-user-name": "Ada Lovelace", "x-agw-user-upn": "ada@example.com"},
    )
    run = next(
        s
        for s in spans.get_finished_spans()
        if s.attributes.get("agw.run_id") == "run-9" and s.name.startswith("invoke_agent")
    )
    assert run.attributes["agw.user.name"] == "Ada Lovelace"
    assert run.attributes["agw.user.upn"] == "ada@example.com"
    assert run.attributes["agw.usage.cached_tokens"] == 600
