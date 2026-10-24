import httpx
import pytest
import respx

from secsdk import ConfigError, InvalidInputError
from secsdk.providers import sec

TICKERS = {
    "0": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
    "1": {"cik_str": 1660134, "ticker": "OKTA", "title": "Okta, Inc."},
}
RECENT = {
    "accessionNumber": ["0001-24-000001", "0001-24-000002", "0001-24-000003"],
    "filingDate": ["2099-01-10", "2099-01-05", "2000-01-01"],
    "form": ["8-K", "10-K", "8-K"],
    "primaryDocument": ["a.htm", "b.htm", "c.htm"],
    "items": ["1.05,9.01", "", "2.02"],
    "primaryDocDescription": ["8-K", "10-K", ""],
}


@pytest.fixture
def ua(monkeypatch):
    monkeypatch.setenv("SECSDK_SEC_USER_AGENT", "Test Org test@example.com")


def test_requires_user_agent(client):
    with pytest.raises(ConfigError):
        client.sec.lookup("msft")


@respx.mock
def test_lookup_and_filings(client, ua):
    respx.get(sec.TICKERS).mock(return_value=httpx.Response(200, json=TICKERS))
    sub = respx.get(sec.SUBMISSIONS.format(cik=789019)).mock(
        return_value=httpx.Response(
            200, json={"name": "MICROSOFT CORP", "filings": {"recent": RECENT}}
        )
    )
    assert client.sec.lookup("okta")[0].cik == 1660134
    filings = client.sec.filings("msft", item="1.05")
    assert [f.accession for f in filings] == ["0001-24-000001"]
    assert filings[0].url.endswith("/789019/000124000001/a.htm")
    assert sub.calls[0].request.headers["User-Agent"] == "Test Org test@example.com"
    assert [f.form for f in client.sec.filings("789019", forms=["10-k"])] == ["10-K"]


@respx.mock
def test_cyber_incidents_filters_item(client, ua):
    hits = [
        {
            "_id": "0001-24-000009:x.htm",
            "_source": {
                "ciks": ["0000000001"],
                "display_names": ["Acme  (ACME)  (CIK 0000000001)"],
                "file_date": "2026-10-01",
                "form": "8-K",
                "items": ["1.05"],
            },
        },
        {
            "_id": "0001-24-000010:y.htm",
            "_source": {
                "ciks": ["0000000002"],
                "display_names": ["Other (CIK 0000000002)"],
                "file_date": "2026-10-02",
                "form": "8-K",
                "items": ["8.01"],
            },
        },
    ]
    respx.get(sec.EFTS).mock(
        return_value=httpx.Response(200, json={"hits": {"total": {"value": 2}, "hits": hits}})
    )
    out = client.sec.cyber_incidents(days=30)
    assert [f.company for f in out] == ["Acme (ACME)"]
    assert out[0].url == "https://www.sec.gov/Archives/edgar/data/1/000124000009/x.htm"


def test_extract_item_skips_table_of_contents():
    text = "Item 1A. Risk Factors 10\nItem 1C. Cybersecurity 20\nItem 2. Properties 30\n\nItem 1C. Cybersecurity\nWe run a program.\nIt is good.\nItem 2. Properties\nBuildings."
    section = sec.extract_item(text, "1C")
    assert section.startswith("Item 1C. Cybersecurity\nWe run a program.")
    assert "Buildings" not in section
    assert sec.extract_item(text, "1.05") is None


def test_document_rejects_non_sec_hosts(client, ua):
    with pytest.raises(InvalidInputError):
        client.sec.document("https://evil.example.com/x.htm")


@respx.mock
def test_document_section(client, ua):
    html = "<html><body><p>Item 1.05 Material Cybersecurity Incidents.</p><p>We were breached.</p><p>Item 9.01 Exhibits</p></body></html>"
    respx.get("https://www.sec.gov/Archives/edgar/data/1/2/a.htm").mock(
        return_value=httpx.Response(200, text=html)
    )
    doc = client.sec.document("https://www.sec.gov/Archives/edgar/data/1/2/a.htm", section="1.05")
    assert doc.found and "We were breached." in doc.text and "Exhibits" not in doc.text
