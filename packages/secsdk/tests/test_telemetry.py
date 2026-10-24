"""Telemetry: spans and metrics are emitted with the right shape, and nothing sensitive leaks."""

import json
import subprocess
import sys

import httpx
import pytest
import respx
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from secsdk import telemetry
from secsdk.providers import kev

SPANS = InMemorySpanExporter()
READER = InMemoryMetricReader()
# Global providers can only be set once per process, and other test modules may
# have set one already: reuse it if so.
if not isinstance(trace.get_tracer_provider(), TracerProvider):
    trace.set_tracer_provider(TracerProvider())
trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(SPANS))
metrics.set_meter_provider(MeterProvider(metric_readers=[READER]))

KEV_FEED = {
    "vulnerabilities": [
        {
            "cveID": "CVE-2021-44228",
            "vendorProject": "Apache",
            "product": "Log4j2",
            "dateAdded": "2021-12-10",
        }
    ]
}


@pytest.fixture(autouse=True)
def reset_spans():
    SPANS.clear()
    yield


def _metrics() -> dict[str, list]:
    data = READER.get_metrics_data()
    out: dict[str, list] = {}
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                out[m.name] = list(m.data.data_points)
    return out


@respx.mock
def test_operation_and_http_spans_are_nested(client):
    respx.get(kev.FEED + "").mock(return_value=httpx.Response(200, json=KEV_FEED))
    client.kev.lookup("CVE-2021-44228")
    spans = {s.name: s for s in SPANS.get_finished_spans()}
    op, http = spans["secsdk kev.lookup"], spans["GET www.cisa.gov"]
    assert http.parent.span_id == op.context.span_id
    assert op.attributes["secsdk.operation"] == "kev.lookup"
    assert op.attributes["secsdk.outcome"] == "ok"
    assert op.attributes["secsdk.params"] == ("cve_id",)
    assert "secsdk.params.values" not in op.attributes  # values are opt-in
    assert http.attributes["http.response.status_code"] == 200
    assert (
        http.attributes["url.path"]
        == "/sites/default/files/feeds/known_exploited_vulnerabilities.json"
    )
    assert http.kind == trace.SpanKind.CLIENT


@respx.mock
def test_errors_mark_spans_and_metrics(client):
    respx.get(kev.FEED).mock(return_value=httpx.Response(200, json=KEV_FEED))
    result = client.execute("kev.lookup", {"cve_id": "CVE-1999-0001"})
    assert not result.ok
    span = next(s for s in SPANS.get_finished_spans() if s.name == "secsdk kev.lookup")
    assert span.status.status_code == trace.StatusCode.ERROR
    assert span.attributes["error.type"] == "not_found"
    calls = _metrics()["secsdk.operation.calls"]
    assert any(p.attributes.get("secsdk.outcome") == "not_found" for p in calls)


@respx.mock
def test_retries_and_no_query_strings(client):
    respx.get("https://api.first.org/data/v1/epss").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"data": []})]
    )
    client.epss.score(["CVE-2021-44228"])
    http = next(s for s in SPANS.get_finished_spans() if s.name == "GET api.first.org")
    assert http.attributes["http.request.resend_count"] == 1
    assert all("CVE-2021-44228" not in str(v) for v in http.attributes.values())
    assert sum(p.value for p in _metrics()["secsdk.http.retries"]) >= 1


@respx.mock
def test_capture_params_opt_in(client, monkeypatch):
    monkeypatch.setenv("SECSDK_TELEMETRY_CAPTURE_PARAMS", "true")
    respx.get(kev.FEED).mock(return_value=httpx.Response(200, json=KEV_FEED))
    client.kev.lookup("CVE-2021-44228")
    op = next(s for s in SPANS.get_finished_spans() if s.name == "secsdk kev.lookup")
    assert json.loads(op.attributes["secsdk.params.values"]) == {"cve_id": "CVE-2021-44228"}


@respx.mock
def test_composite_operations_nest(client):
    respx.get(kev.FEED).mock(return_value=httpx.Response(200, json=KEV_FEED))
    respx.get("https://api.first.org/data/v1/epss").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    client.intel.cve_priority(["CVE-2021-44228"], nvd=False)
    spans = {s.name: s for s in SPANS.get_finished_spans()}
    parent = spans["secsdk intel.cve_priority"].context.span_id
    assert spans["secsdk epss.score"].parent.span_id == parent


def test_disabled_by_env(client, monkeypatch):
    monkeypatch.setenv("SECSDK_TELEMETRY", "off")
    client.azure.presets()
    assert SPANS.get_finished_spans() == ()


def test_trace_context_round_trips_through_env():
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("parent") as parent:
        env = telemetry.inject_env({})
    trace_id = format(parent.get_span_context().trace_id, "032x")
    assert env["TRACEPARENT"].split("-")[1] == trace_id


def test_cli_writes_spans_to_file_and_joins_parent_trace(tmp_path):
    """End to end: secctl in a subprocess exports to a JSONL file under the caller's trace."""
    out = tmp_path / "spans.jsonl"
    traceparent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    env = {
        "PATH": "",
        "SECSDK_TELEMETRY_FILE": str(out),
        "TRACEPARENT": traceparent,
        "HOME": str(tmp_path),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "secctl", "azure", "presets", "--compact"],
        env=env,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    spans = [json.loads(line) for line in out.read_text().splitlines()]
    op = next(s for s in spans if s["name"] == "secsdk azure.presets")
    assert op["context"]["trace_id"] == "0x0af7651916cd43dd8448eb211c80319c"
    assert op["resource"]["attributes"]["service.name"] == "secctl"
