"""CLI contract tests. These protect what agents rely on: JSON shape, exit codes, discovery."""

import json
import os

import httpx
import pytest
import respx
from click.testing import CliRunner

from secctl.app import cli
from secsdk import default_registry

KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
KEV = {
    "vulnerabilities": [
        {
            "cveID": "CVE-2021-44228",
            "vendorProject": "Apache",
            "product": "Log4j2",
            "dateAdded": "2021-12-10",
            "knownRansomwareCampaignUse": "Known",
        }
    ]
}


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith(("SECSDK_", "TENABLE_", "AZURE_", "FEEDLY_", "SECCTL_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SECSDK_CACHE_ENABLED", "false")
    monkeypatch.setenv("SECSDK_RATE_LIMIT", "false")
    monkeypatch.setenv("SECSDK_AZURE_USE_CLI", "false")


@pytest.fixture
def run():
    runner = CliRunner()

    def _run(*args):
        res = runner.invoke(cli, list(args), catch_exceptions=False)
        return res

    return _run


def test_every_operation_has_a_working_command(run):
    for op in default_registry().operations():
        res = run(op.provider, op.name.replace("_", "-"), "--help")
        assert res.exit_code == 0, (op.fqname, res.output)
        assert op.summary.split("(")[0][:20] in res.output


def test_ops_and_describe(run):
    res = run("ops", "kev")
    data = json.loads(res.output)
    assert data["ok"] and {d["operation"] for d in data["data"]} >= {"kev.lookup", "kev.search"}
    desc = json.loads(run("describe", "kev", "lookup").output)["data"]
    assert desc["input_schema"]["required"] == ["cve_id"]
    assert desc["usage"] == "secctl kev lookup <cve_id>"


def test_describe_unknown_operation(run):
    res = run("describe", "nope.nothing")
    assert res.exit_code == 2
    assert json.loads(res.output)["error"]["code"] == "unknown_operation"


@respx.mock
def test_dynamic_command_json_envelope(run):
    respx.get(KEV_URL).mock(return_value=httpx.Response(200, json=KEV))
    res = run("kev", "lookup", "cve-2021-44228", "--fields", "cve_id,ransomware")
    assert res.exit_code == 0
    out = json.loads(res.output)
    assert out == {
        "ok": True,
        "operation": "kev.lookup",
        "data": {"cve_id": "CVE-2021-44228", "ransomware": True},
        "meta": out["meta"],
    }


@respx.mock
def test_not_found_exit_code(run):
    respx.get(KEV_URL).mock(return_value=httpx.Response(200, json=KEV))
    res = run("kev", "lookup", "CVE-2000-0001")
    assert res.exit_code == 5
    assert json.loads(res.output)["error"]["code"] == "not_found"


def test_invalid_input_exit_code(run):
    res = run("osint", "dns", "not a domain")
    assert res.exit_code == 2


def test_not_configured_exit_code(run):
    res = run("tenable", "scans")
    assert res.exit_code == 3
    err = json.loads(res.output)["error"]
    assert "TENABLE_ACCESS_KEY" in err["message"] and err["hint"]


@respx.mock
def test_list_options_accept_commas_and_repeats(run):
    route = respx.get("https://cloudflare-dns.com/dns-query").mock(
        return_value=httpx.Response(200, json={"Answer": []})
    )
    run("osint", "dns", "example.com", "--types", "MX,TXT", "--types", "NS")
    assert [c.request.url.params["type"] for c in route.calls] == ["MX", "TXT", "NS"]


@respx.mock
def test_call_with_params_and_pairs(run):
    route = respx.get("https://api.first.org/data/v1/epss").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    assert run("call", "epss.score", "--params", '{"cve_ids": ["CVE-2021-44228"]}').exit_code == 0
    assert run("call", "epss.score", "-p", 'cve_ids=["CVE-2021-44228"]').exit_code == 0
    assert route.call_count == 2


@respx.mock
def test_jsonl_and_table_formats(run):
    respx.get(KEV_URL).mock(return_value=httpx.Response(200, json=KEV))
    lines = run("kev", "search", "-f", "jsonl").output.strip().splitlines()
    assert json.loads(lines[0])["cve_id"] == "CVE-2021-44228"
    table = run("-f", "table", "kev", "search", "--fields", "cve_id,vendor").output
    assert "CVE-2021-44228" in table and "Apache" in table


def test_providers_and_guide(run):
    data = json.loads(run("providers").output)["data"]
    by_name = {p["provider"]: p for p in data}
    assert by_name["kev"]["configured"] and not by_name["tenable"]["configured"]
    guide = run("guide").output
    assert "secctl kev lookup <cve_id>" in guide and "Exit codes" in guide


def test_schema_is_valid_tool_list(run):
    tools = json.loads(run("schema", "--provider", "kev").output)
    assert {t["name"] for t in tools} == {"kev_lookup", "kev_search", "kev_stats"}
    assert all("input_schema" in t for t in tools)


def test_env_file_option(run, tmp_path):
    env = tmp_path / "test.env"
    env.write_text("TENABLE_ACCESS_KEY=a\nTENABLE_SECRET_KEY='b'\n")
    data = json.loads(run("--env-file", str(env), "providers").output)["data"]
    assert {p["provider"]: p for p in data}["tenable"]["configured"]


def test_scaffold_writes_provider_and_test(run, tmp_path, monkeypatch):
    import secsdk.providers

    fake_pkg = tmp_path / "packages" / "secsdk" / "src" / "secsdk" / "providers"
    fake_pkg.mkdir(parents=True)
    (tmp_path / "packages" / "secsdk" / "tests").mkdir()
    monkeypatch.setattr(secsdk.providers, "__file__", str(fake_pkg / "__init__.py"))
    res = run("dev", "scaffold", "greynoise", "--auth")
    assert res.exit_code == 0, res.output
    src = (fake_pkg / "greynoise.py").read_text()
    compile(src, "greynoise.py", "exec")
    assert "class Greynoise(Provider)" in src and "GREYNOISE_API_KEY" in src
    assert (tmp_path / "packages" / "secsdk" / "tests" / "providers" / "test_greynoise.py").exists()
    assert run("dev", "scaffold", "greynoise").exit_code != 0  # refuses to overwrite
