"""Offline tests for the example agent: no API key or network needed.

A scripted fake client stands in for Claude, so these tests check our side of the
loop: argument safety, workflow parsing, required-step enforcement, and that
secctl really runs.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

import secctl_agent as sa


@pytest.fixture(autouse=True)
def offline_env(monkeypatch):
    monkeypatch.setenv("SECSDK_RATE_LIMIT", "false")


# ----- tool safety -------------------------------------------------------------------------


def test_command_words_skip_options():
    assert sa.command_words(["-f", "table", "kev", "lookup", "CVE-1", "--fields", "a"]) == [
        "kev",
        "lookup",
        "CVE-1",
        "a",
    ]


@pytest.mark.parametrize(
    "args",
    [
        [],
        "kev lookup",
        [1, 2],
        ["dev", "scaffold", "x"],
        ["--compact", "cache", "clear"],
        ["--env-file=/etc/passwd", "providers"],
        ["--env-file", "x", "providers"],
    ],
)
def test_blocked_or_invalid_args(args):
    assert sa.check_args(args) is not None


def test_allowed_args():
    assert sa.check_args(["intel", "cve-priority", "CVE-2024-3400", "--no-nvd"]) is None


def test_run_secctl_executes_real_cli_without_network():
    text, is_error, rec = sa.run_secctl(["ops", "kev", "--compact"], "list kev ops")
    assert not is_error and rec.exit_code == 0
    assert text.startswith("exit_code: 0") and '"kev.lookup"' in text


def test_run_secctl_reports_nonzero_exit_as_information():
    text, is_error, rec = sa.run_secctl(["describe", "nope.nothing"])
    assert rec.exit_code == 2 and not is_error and "unknown_operation" in text


def test_run_secctl_blocked_is_error():
    text, is_error, rec = sa.run_secctl(["dev", "scaffold", "x"])
    assert is_error and rec.blocked and "not available" in text


def test_shell_metacharacters_are_just_arguments():
    # No shell is involved: this is an (invalid) domain, not a second command.
    text, _, rec = sa.run_secctl(["osint", "dns", "example.com; rm -rf /"])
    assert rec.exit_code == 2 and "invalid_input" in text


# ----- workflows ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [p.stem for p in sa.WORKFLOWS_DIR.glob("*.md")])
def test_every_workflow_loads_and_renders(name):
    wf = sa.Workflow.load(name)
    values = {k: "x" for k, spec in wf.inputs.items() if "default" not in spec}
    text = wf.render(values)
    assert "{" not in text.replace("{{", "")  # all placeholders filled
    assert wf.required, "workflows must name the secctl commands they require"


def test_render_requires_inputs():
    wf = sa.Workflow.load("cve-triage")
    with pytest.raises(SystemExit):
        wf.render({})
    with pytest.raises(SystemExit):
        wf.render({"cves": "CVE-1", "bogus": "1"})


def test_missing_steps_matches_prefixes():
    wf = sa.Workflow.load("cve-triage")

    def rec(*args):
        return sa.CommandRecord(list(args), "", 0, 0.0, 0, False)

    done = [
        rec("guide"),
        rec("--compact", "intel", "cve-priority", "CVE-1"),
        rec("kev", "lookup", "CVE-1"),
    ]
    assert wf.missing_steps(done) == ["epss score"]
    blocked = sa.CommandRecord(["epss", "score", "CVE-1"], "", None, 0.0, 0, False, blocked="nope")
    assert wf.missing_steps([*done, blocked]) == ["epss score"]


# ----- the loop, with a scripted fake model ------------------------------------------------


def _resp(stop, *blocks):
    return SimpleNamespace(
        stop_reason=stop,
        stop_details=None,
        content=list(blocks),
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0),
    )


def _tool(tid, *args):
    return SimpleNamespace(
        type="tool_use", id=tid, name="secctl", input={"args": list(args), "reason": "test"}
    )


def _text(t):
    return SimpleNamespace(type="text", text=t)


class FakeClient:
    """Plays back scripted responses and records every request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})  # snapshot
        return self.responses.pop(0)


def test_loop_runs_tools_and_nudges_until_required_steps_done(tmp_path):
    wf = sa.Workflow(
        name="t",
        description="",
        directions="Do {x}",
        inputs={"x": {}},
        required=["ops", "describe"],
    )
    client = FakeClient(
        [
            _resp("tool_use", _text("Listing ops."), _tool("t1", "ops", "kev", "--compact")),
            _resp("end_turn", _text("Report without describe")),  # skipped a required step
            _resp("tool_use", _tool("t2", "describe", "kev.lookup", "--compact")),
            _resp("end_turn", _text("# Final report")),
        ]
    )
    agent = sa.SecctlAgent(client=client, verbose=False)
    result = agent.run(wf, {"x": "it"})

    assert result.stop == "complete" and result.report == "# Final report"
    assert [r.args[0] for r in result.commands] == ["ops", "describe"]
    # The nudge named the missing command, and history was only ever appended to.
    nudge = client.requests[2]["messages"][4]["content"]
    assert "secctl describe" in nudge
    for earlier, later in zip(client.requests, client.requests[1:], strict=False):
        assert later["messages"][: len(earlier["messages"])] == earlier["messages"]
    # Request shape: one strict tool, cached system prompt, fallbacks enabled.
    req = client.requests[0]
    assert req["model"] == sa.DEFAULT_MODEL and req["tools"] == [sa.SECCTL_TOOL]
    assert req["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in req["betas"]
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    # Tool results went back as tool_result blocks with real secctl output.
    tool_result = client.requests[1]["messages"][2]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "t1"
    assert "exit_code: 0" in tool_result["content"]


def test_loop_gives_up_after_max_nudges():
    wf = sa.Workflow(name="t", description="", directions="go", required=["guide"])
    client = FakeClient([_resp("end_turn", _text("lazy"))] * 3)
    result = sa.SecctlAgent(client=client, verbose=False).run(wf, {}, max_nudges=2)
    assert result.stop == "incomplete" and "guide" in result.report and len(client.requests) == 3


def test_refusal_stops_cleanly():
    wf = sa.Workflow(name="t", description="", directions="go")
    refusal = _resp("refusal")
    refusal.stop_details = SimpleNamespace(category="cyber", explanation="declined")
    result = sa.SecctlAgent(client=FakeClient([refusal]), verbose=False).run(wf, {})
    assert result.stop == "refusal" and "cyber" in result.report


def test_tracing_links_agent_model_and_secctl_spans(tmp_path, monkeypatch):
    """With telemetry on, one trace covers the agent run, model calls and tool calls,
    and secctl (a subprocess) joins that trace through TRACEPARENT."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(TracerProvider())
    exporter = InMemorySpanExporter()
    trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))
    spans_file = tmp_path / "secctl-spans.jsonl"
    monkeypatch.setenv("SECSDK_TELEMETRY_FILE", str(spans_file))  # read by the secctl subprocess

    wf = sa.Workflow(name="t", description="", directions="go", required=["azure presets"])
    client = FakeClient(
        [
            _resp("tool_use", _tool("t1", "azure", "presets", "--compact")),
            _resp("end_turn", _text("done")),
        ]
    )
    sa.SecctlAgent(client=client, verbose=False).run(wf, {})

    spans = {s.name: s for s in exporter.get_finished_spans()}
    agent, tool = spans["invoke_agent t"], spans["execute_tool secctl"]
    assert spans[f"chat {sa.DEFAULT_MODEL}"].parent.span_id == agent.context.span_id
    assert tool.parent.span_id == agent.context.span_id
    assert tool.attributes["secctl.command"] == "azure presets"
    assert agent.attributes["gen_ai.usage.input_tokens"] == 20
    # The secctl subprocess exported its own spans under the same trace id.
    import json

    child = [json.loads(line) for line in spans_file.read_text().splitlines()]
    assert child and all(
        c["context"]["trace_id"] == f"0x{agent.context.trace_id:032x}" for c in child
    )
