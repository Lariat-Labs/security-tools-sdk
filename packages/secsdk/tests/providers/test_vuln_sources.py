"""NVD, KEV, EPSS and the intel scoring rules."""

import httpx
import pytest
import respx

from secsdk import NotFoundError
from secsdk.providers import epss, kev, nvd
from secsdk.providers.intel import score_cve
from secsdk.providers.kev import KEVEntry

NVD_CVE = {
    "id": "CVE-2021-44228",
    "published": "2021-12-10T10:15:09.143",
    "lastModified": "2024-01-01T00:00:00.000",
    "vulnStatus": "Analyzed",
    "cisaExploitAdd": "2021-12-10",
    "descriptions": [{"lang": "es", "value": "x"}, {"lang": "en", "value": "Log4Shell"}],
    "metrics": {
        "cvssMetricV31": [
            {
                "source": "nvd@nist.gov",
                "type": "Primary",
                "cvssData": {
                    "baseScore": 10.0,
                    "baseSeverity": "CRITICAL",
                    "vectorString": "CVSS:3.1/AV:N",
                },
            }
        ],
        "cvssMetricV2": [{"type": "Primary", "cvssData": {"baseScore": 9.3}}],
    },
    "weaknesses": [
        {
            "description": [
                {"lang": "en", "value": "CWE-502"},
                {"lang": "en", "value": "NVD-CWE-Other"},
            ]
        }
    ],
    "references": [{"url": "https://logging.apache.org/log4j/2.x/security.html"}],
}

KEV_FEED = {
    "catalogVersion": "2026.10.24",
    "dateReleased": "2026-10-24T00:00:00Z",
    "vulnerabilities": [
        {
            "cveID": "CVE-2021-44228",
            "vendorProject": "Apache",
            "product": "Log4j2",
            "vulnerabilityName": "Log4j2 RCE",
            "dateAdded": "2021-12-10",
            "dueDate": "2021-12-24",
            "shortDescription": "JNDI lookup RCE",
            "requiredAction": "Patch",
            "knownRansomwareCampaignUse": "Known",
            "cwes": ["CWE-502"],
        },
        {
            "cveID": "CVE-2023-0001",
            "vendorProject": "Microsoft",
            "product": "Windows",
            "vulnerabilityName": "Windows bug",
            "dateAdded": "2023-01-05",
            "dueDate": "",
            "knownRansomwareCampaignUse": "Unknown",
        },
    ],
}


def test_parse_cve_prefers_newest_cvss_and_english():
    cve = nvd.parse_cve(NVD_CVE)
    assert cve.description == "Log4Shell"
    assert cve.cvss.version == "3.1" and cve.cvss.score == 10.0
    assert cve.cwes == ["CWE-502"]
    assert cve.in_kev is True


@respx.mock
def test_nvd_cve_and_not_found(client):
    route = respx.get(nvd.API).mock(
        side_effect=[
            httpx.Response(200, json={"vulnerabilities": [{"cve": NVD_CVE}]}),
            httpx.Response(200, json={"vulnerabilities": []}),
        ]
    )
    assert client.nvd.cve("cve-2021-44228").id == "CVE-2021-44228"  # normalised to upper case
    assert route.calls[0].request.url.params["cveId"] == "CVE-2021-44228"
    with pytest.raises(NotFoundError):
        client.nvd.cve("CVE-2000-0001")


@respx.mock
def test_nvd_sends_api_key_when_configured(client, monkeypatch):
    monkeypatch.setenv("NVD_API_KEY", "k")
    route = respx.get(nvd.API).mock(return_value=httpx.Response(200, json={"vulnerabilities": []}))
    client.nvd.search(keyword="x", kev_only=True)
    req = route.calls[0].request
    assert req.headers["apiKey"] == "k"
    assert "hasKev" in req.url.params


@respx.mock
def test_kev_lookup_and_search(client):
    respx.get(kev.FEED).mock(return_value=httpx.Response(200, json=KEV_FEED))
    entry = client.kev.lookup("CVE-2021-44228")
    assert entry.ransomware and entry.vendor == "Apache"
    assert [e.cve_id for e in client.kev.search(vendor="micro")] == ["CVE-2023-0001"]
    assert [e.cve_id for e in client.kev.search(ransomware=True)] == ["CVE-2021-44228"]
    assert client.kev.search()[0].cve_id == "CVE-2023-0001"  # newest first
    assert client.kev.stats().total == 2
    with pytest.raises(NotFoundError):
        client.kev.lookup("CVE-1999-0001")


@respx.mock
def test_epss_batches_and_dedupes(client):
    route = respx.get(epss.API).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "cve": "CVE-2021-44228",
                        "epss": "0.97",
                        "percentile": "0.99",
                        "date": "2026-10-24",
                    }
                ]
            },
        )
    )
    ids = ["CVE-2021-44228"] * 3 + [f"CVE-2020-{i:05d}" for i in range(150)]
    scores = client.epss.score(ids)
    assert route.call_count == 2  # 151 unique ids -> 2 batches of <=100
    assert scores[0].epss == 0.97


@pytest.mark.parametrize(
    ("cvss", "epss_score", "in_kev", "expected"),
    [
        (None, None, True, "P1"),
        (3.0, 0.01, True, "P1"),
        (5.0, 0.6, False, "P2"),
        (9.8, 0.2, False, "P2"),
        (9.8, 0.05, False, "P3"),
        (7.0, 0.0, False, "P3"),
        (4.0, 0.15, False, "P3"),
        (6.9, 0.09, False, "P4"),
        (None, None, False, "P4"),
    ],
)
def test_score_cve_rules(cvss, epss_score, in_kev, expected):
    entry = (
        KEVEntry(cve_id="CVE-1", vendor="v", product="p", name="n", date_added="2024-01-01")
        if in_kev
        else None
    )
    prio, reasons = score_cve("CVE-1", cvss=cvss, epss=epss_score, kev=entry)
    assert prio == expected
    assert reasons


@respx.mock
def test_intel_cve_priority_combines_sources(client):
    respx.get(kev.FEED).mock(return_value=httpx.Response(200, json=KEV_FEED))
    respx.get(epss.API).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "cve": "CVE-2021-44228",
                        "epss": "0.97",
                        "percentile": "0.99",
                        "date": "2026-10-24",
                    },
                    {
                        "cve": "CVE-2022-1111",
                        "epss": "0.01",
                        "percentile": "0.2",
                        "date": "2026-10-24",
                    },
                ]
            },
        )
    )
    out = client.intel.cve_priority(["CVE-2022-1111", "CVE-2021-44228"], nvd=False)
    assert [p.cve_id for p in out] == ["CVE-2021-44228", "CVE-2022-1111"]
    assert out[0].priority == "P1" and out[0].ransomware
    assert out[1].priority == "P4"
