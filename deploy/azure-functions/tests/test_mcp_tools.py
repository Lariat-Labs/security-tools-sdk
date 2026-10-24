"""Tests for the MCP tool logic (no Azure Functions runtime needed)."""

import importlib
import json
import sys
from pathlib import Path

import httpx
import pytest
import respx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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


@pytest.fixture
def tools(monkeypatch, tmp_path):
    for key in ("SECSDK_MCP_ALLOWED_PROVIDERS", "TENABLE_ACCESS_KEY", "TENABLE_SECRET_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SECSDK_CACHE_ENABLED", "false")
    monkeypatch.setenv("SECSDK_RATE_LIMIT", "false")
    monkeypatch.setenv("SECSDK_AZURE_USE_CLI", "false")
    monkeypatch.chdir(tmp_path)
    import mcp_tools

    module = importlib.reload(mcp_tools)  # fresh Client per test
    yield module


@respx.mock
def test_call_returns_cli_envelope(tools):
    respx.get(KEV_URL).mock(return_value=httpx.Response(200, json=KEV))
    out = json.loads(tools.call("kev.lookup", '{"cve_id": "cve-2023-4966"}'))
    assert out["ok"] and out["operation"] == "kev.lookup" and out["data"]["ransomware"] is True
    # dict params work too, and CLI-style dashed operation names are accepted
    assert json.loads(tools.call("kev.lookup", {"cve_id": "CVE-2023-4966"}))["ok"]
    assert json.loads(tools.describe("osint.email-security"))["ok"]


def test_call_errors_are_envelopes(tools):
    assert json.loads(tools.call("kev.lookup", "{not json"))["error"]["code"] == "invalid_input"
    assert json.loads(tools.call("kev.lookup", "[1]"))["error"]["code"] == "invalid_input"
    assert json.loads(tools.call("nope.op", "{}"))["error"]["code"] == "unknown_operation"
    assert json.loads(tools.call("tenable.scans", "{}"))["error"]["code"] == "not_configured"


def test_allowed_providers_restrict_everything(tools, monkeypatch):
    monkeypatch.setenv("SECSDK_MCP_ALLOWED_PROVIDERS", "kev,epss")
    denied = json.loads(tools.call("azure.presets", "{}"))
    assert not denied["ok"] and "not available" in denied["error"]["message"]
    assert "azure." not in tools.guide() and "kev.lookup" in tools.guide()
    assert {p["provider"] for p in json.loads(tools.providers())["data"]} == {"kev", "epss"}
    assert not json.loads(tools.describe("azure.query"))["ok"]


def test_describe_and_guide(tools):
    d = json.loads(tools.describe("kev.lookup"))["data"]
    assert d["input_schema"]["required"] == ["cve_id"]
    assert "`intel.cve_priority`(cve_ids: list[str], nvd?: bool)" in tools.guide()


def test_oversized_results_become_a_hint(tools, monkeypatch):
    monkeypatch.setenv("SECSDK_MCP_MAX_CHARS", "200")
    out = json.loads(tools.call("azure.presets", "{}"))
    assert out["error"]["code"] == "result_too_large" and "Narrow" in out["error"]["hint"]


def test_function_app_registers_expected_tools():
    """Only runs where azure-functions >= 2.3 (Python 3.13) is installed."""
    func = pytest.importorskip("azure.functions")
    if not hasattr(func.FunctionApp, "mcp_tool"):
        pytest.skip("azure-functions without MCP support")
    import function_app

    names = {f.get_function_name() for f in function_app.app.get_functions()}
    assert names == {
        "secsdk_guide",
        "secsdk_providers",
        "secsdk_describe",
        "secsdk_call",
        "intel_cve_priority",
        "kev_lookup",
        "osint_profile",
        "feeds_news",
    }
