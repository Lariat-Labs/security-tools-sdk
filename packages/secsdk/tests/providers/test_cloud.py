"""Tenable and Azure (credentialed) providers, fully mocked."""

import json

import httpx
import pytest
import respx

from secsdk import ConfigError, UpstreamError
from secsdk.providers import azure, kev

T = "https://cloud.tenable.com"


@pytest.fixture
def tenable_keys(monkeypatch):
    monkeypatch.setenv("TENABLE_ACCESS_KEY", "ak")
    monkeypatch.setenv("TENABLE_SECRET_KEY", "sk")


def test_tenable_requires_keys(client):
    with pytest.raises(ConfigError) as exc:
        client.tenable.scans()
    assert "TENABLE_ACCESS_KEY" in exc.value.message


FINDING = {
    "asset": {"uuid": "u1", "hostname": "web01", "ipv4": "10.0.0.5"},
    "plugin": {
        "id": 1,
        "name": "Log4j RCE",
        "cve": ["CVE-2021-44228"],
        "vpr": {"score": 9.9},
        "cvss3_base_score": 10.0,
    },
    "severity": "critical",
    "state": "OPEN",
    "port": {"port": 443},
    "first_found": "2026-01-01T00:00:00Z",
}


@respx.mock
def test_tenable_findings_export_flow(client, tenable_keys, monkeypatch):
    start = respx.post(f"{T}/vulns/export").mock(
        return_value=httpx.Response(200, json={"export_uuid": "e1"})
    )
    respx.get(f"{T}/vulns/export/e1/status").mock(
        side_effect=[
            httpx.Response(200, json={"status": "PROCESSING", "chunks_available": [1]}),
            httpx.Response(200, json={"status": "FINISHED", "chunks_available": [1, 2]}),
        ]
    )
    respx.get(f"{T}/vulns/export/e1/chunks/1").mock(
        return_value=httpx.Response(200, json=[FINDING])
    )
    respx.get(f"{T}/vulns/export/e1/chunks/2").mock(
        return_value=httpx.Response(
            200, json=[FINDING | {"asset": {"uuid": "u2", "ipv4": "10.0.0.6"}}]
        )
    )
    monkeypatch.setattr(
        "secsdk.providers.tenable.time.sleep", lambda s: None
    )  # no wait between polls
    out = client.tenable.findings(severity=["critical"], days=7)
    assert [f.asset for f in out] == ["web01", "10.0.0.6"]
    assert out[0].cves == ["CVE-2021-44228"] and out[0].vpr == 9.9 and out[0].port == 443
    body = json.loads(start.calls[0].request.content)
    assert body["filters"]["severity"] == ["critical"] and "since" in body["filters"]
    assert start.calls[0].request.headers["X-ApiKeys"] == "accessKey=ak;secretKey=sk"


@respx.mock
def test_tenable_export_error(client, tenable_keys):
    respx.post(f"{T}/vulns/export").mock(
        return_value=httpx.Response(200, json={"export_uuid": "e2"})
    )
    respx.get(f"{T}/vulns/export/e2/status").mock(
        return_value=httpx.Response(200, json={"status": "ERROR"})
    )
    with pytest.raises(UpstreamError):
        client.tenable.findings()


@respx.mock
def test_intel_kev_exposure(client, tenable_keys):
    respx.post(f"{T}/vulns/export").mock(
        return_value=httpx.Response(200, json={"export_uuid": "e3"})
    )
    respx.get(f"{T}/vulns/export/e3/status").mock(
        return_value=httpx.Response(200, json={"status": "FINISHED", "chunks_available": [1]})
    )
    other = FINDING | {"plugin": {"id": 2, "name": "Other", "cve": ["CVE-2020-9999"]}}
    respx.get(f"{T}/vulns/export/e3/chunks/1").mock(
        return_value=httpx.Response(200, json=[FINDING, other])
    )
    respx.get(kev.FEED).mock(
        return_value=httpx.Response(
            200,
            json={
                "vulnerabilities": [
                    {
                        "cveID": "CVE-2021-44228",
                        "dateAdded": "2021-12-10",
                        "knownRansomwareCampaignUse": "Known",
                    }
                ]
            },
        )
    )
    respx.get("https://api.first.org/data/v1/epss").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    out = client.intel.kev_exposure()
    assert [(e.cve_id, e.asset_count, e.ransomware) for e in out] == [("CVE-2021-44228", 1, True)]


@respx.mock
def test_azure_service_principal_and_paging(client, monkeypatch):
    monkeypatch.setenv("AZURE_TENANT_ID", "t")
    monkeypatch.setenv("AZURE_CLIENT_ID", "c")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "s")
    token = respx.post(azure.LOGIN.format(tenant="t")).mock(
        return_value=httpx.Response(200, json={"access_token": "AT", "expires_in": 3600})
    )
    graph = respx.post(f"{azure.ARM}/providers/Microsoft.ResourceGraph/resources").mock(
        side_effect=[
            httpx.Response(
                200, json={"totalRecords": 3, "data": [{"n": 1}, {"n": 2}], "$skipToken": "next"}
            ),
            httpx.Response(200, json={"totalRecords": 3, "data": [{"n": 3}]}),
        ]
    )
    res = client.azure.check("public_ips", subscriptions=["sub1"])
    assert res.count == 3 and not res.truncated
    assert token.call_count == 1
    first, second = (json.loads(c.request.content) for c in graph.calls)
    assert first["subscriptions"] == ["sub1"] and "publicipaddresses" in first["query"]
    assert second["options"]["$skipToken"] == "next"
    assert graph.calls[0].request.headers["Authorization"] == "Bearer AT"


def test_azure_not_configured_without_cli(client):
    with pytest.raises(ConfigError):
        client.azure.subscriptions()


def test_azure_presets_need_no_auth(client):
    presets = client.azure.presets()
    assert set(presets) == set(azure.QUERIES)


def test_azure_managed_identity_used_inside_functions(client, monkeypatch):
    """IDENTITY_ENDPOINT (set by Functions/App Service) switches on managed identity."""
    import sys
    import types

    calls = {}

    class FakeMI:
        def __init__(self, client_id=None):
            calls["client_id"] = client_id

        def get_token(self, scope):
            calls["scope"] = scope
            return types.SimpleNamespace(token="MI-TOKEN", expires_on=9_999_999_999)

    fake = types.ModuleType("azure.identity")
    fake.ManagedIdentityCredential = FakeMI
    monkeypatch.setitem(sys.modules, "azure.identity", fake)
    monkeypatch.setattr("secsdk.providers.azure.importlib.util.find_spec", lambda name: object())
    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://localhost/msi")
    monkeypatch.setenv("SECSDK_AZURE_MANAGED_IDENTITY_CLIENT_ID", "uami-123")

    assert client.azure.auth_status().note == "auth method: managed_identity"
    assert client.azure._headers() == {"Authorization": "Bearer MI-TOKEN"}
    assert calls == {"client_id": "uami-123", "scope": "https://management.azure.com/.default"}
