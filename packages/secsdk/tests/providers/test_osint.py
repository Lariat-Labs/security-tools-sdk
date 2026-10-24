import httpx
import pytest
import respx

from secsdk import InvalidInputError
from secsdk.providers import osint


def test_aggregate_ct_dedupes_and_filters():
    rows = [
        {
            "name_value": "A.example.com\n*.example.com",
            "common_name": "a.example.com",
            "not_before": "2024-01-01T00:00:00",
            "not_after": "2024-04-01T00:00:00",
        },
        {
            "name_value": "A.example.com\nevil-example.com",
            "common_name": "",
            "not_before": "2023-06-01T00:00:00",
            "not_after": "2025-01-01T00:00:00",
        },
    ]
    subs = {s.name: s for s in osint.aggregate_ct(rows, "example.com")}
    assert set(subs) == {"a.example.com", "example.com"}
    assert subs["a.example.com"].certificates == 2  # once per certificate, case-insensitive
    assert subs["a.example.com"].first_seen.year == 2023
    assert subs["a.example.com"].last_seen.year == 2025


@pytest.mark.parametrize(
    ("txt", "dmarc", "expected_ids"),
    [
        ([], [], {"spf-missing", "dmarc-missing"}),
        (["v=spf1 +all"], ["v=DMARC1; p=reject; rua=mailto:x@y"], {"spf-pass-all"}),
        (
            ["v=spf1 include:x ~all"],
            ["v=DMARC1; p=none"],
            {"dmarc-monitor-only", "dmarc-no-reports"},
        ),
        (
            ["v=spf1 -all", "v=spf1 ~all"],
            ["v=DMARC1; p=quarantine; pct=50; rua=mailto:a@b"],
            {"spf-multiple", "dmarc-partial"},
        ),
        (["v=spf1 -all"], ["v=DMARC1; p=reject; rua=mailto:a@b"], set()),
    ],
)
def test_assess_email(txt, dmarc, expected_ids):
    result = osint.assess_email("example.com", txt, dmarc, ["10 mx.example.com"])
    assert {f.id for f in result.findings} == expected_ids


@respx.mock
def test_dns_filters_chain_and_cleans_txt(client):
    respx.get(osint.DOH).mock(
        return_value=httpx.Response(
            200,
            json={
                "Answer": [
                    {"name": "www.example.com.", "type": 5, "TTL": 60, "data": "example.com."},
                    {"name": "example.com.", "type": 16, "TTL": 60, "data": '"v=spf1 " "-all"'},
                ]
            },
        )
    )
    records = client.osint.dns("www.example.com", types=["TXT"])
    assert [(r.type, r.value) for r in records] == [("TXT", "v=spf1 -all")]


def test_domain_validation(client):
    with pytest.raises(InvalidInputError):
        client.osint.whois("not a domain")
    # URLs and trailing dots are normalised rather than rejected
    assert osint.Domain  # type alias exists


@respx.mock
def test_exposure_unknown_ip_returns_known_false(client):
    respx.get(f"{osint.INTERNETDB}/192.0.2.1").mock(
        return_value=httpx.Response(404, json={"detail": "No information available"})
    )
    assert client.osint.exposure("192.0.2.1").known is False


@respx.mock
def test_whois_parses_rdap(client):
    respx.get(f"{osint.RDAP}/domain/example.com").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": ["active"],
                "events": [
                    {"eventAction": "registration", "eventDate": "1995-08-14T04:00:00Z"},
                    {"eventAction": "expiration", "eventDate": "2030-08-13T04:00:00Z"},
                ],
                "nameservers": [
                    {"ldhName": "B.IANA-SERVERS.NET"},
                    {"ldhName": "A.IANA-SERVERS.NET"},
                ],
                "entities": [
                    {
                        "roles": ["registrar"],
                        "vcardArray": [
                            "vcard",
                            [
                                ["version", {}, "text", "4.0"],
                                ["fn", {}, "text", "Example Registrar"],
                            ],
                        ],
                    }
                ],
                "secureDNS": {"delegationSigned": True},
            },
        )
    )
    reg = client.osint.whois("https://Example.com/path")
    assert reg.domain == "example.com"
    assert reg.registrar == "Example Registrar"
    assert reg.nameservers == ["a.iana-servers.net", "b.iana-servers.net"]
    assert reg.registered.year == 1995 and reg.dnssec is True
