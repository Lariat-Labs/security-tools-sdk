"""feeds (RSS/Atom) and feedly."""

import httpx
import pytest
import respx

from secsdk import ConfigError, UpstreamError
from secsdk.providers import feedly, feeds

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>t</title>
<item><title>Patch CVE-2024-3400 now</title><link>https://n.example/1</link>
<pubDate>Mon, 20 Oct 2026 10:00:00 GMT</pubDate><description>&lt;p&gt;PAN-OS bug cve-2024-3400&lt;/p&gt;</description></item>
<item><title>Old news</title><link>https://n.example/2</link><pubDate>Mon, 01 Jan 2001 10:00:00 GMT</pubDate></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>a</title>
<entry><title>Ransomware hits X</title><link rel="alternate" href="https://a.example/1"/>
<updated>2026-10-21T00:00:00Z</updated><summary>details</summary></entry></feed>"""

BOMB = b"""<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>
<rss><channel><item><title>&lol2;</title></item></channel></rss>"""


def test_parse_rss_and_atom():
    items = feeds.parse_feed(RSS, "s")
    assert items[0].title == "Patch CVE-2024-3400 now"
    assert items[0].summary == "PAN-OS bug cve-2024-3400"
    assert items[0].cves == ["CVE-2024-3400"]
    assert items[0].published.tzinfo is not None
    atom = feeds.parse_feed(ATOM, "a")
    assert atom[0].url == "https://a.example/1"


def test_entity_expansion_is_rejected():
    with pytest.raises(UpstreamError):
        feeds.parse_feed(BOMB, "evil")


@respx.mock
def test_news_merges_filters_and_reports_errors(client, monkeypatch):
    monkeypatch.setattr(
        "secsdk.providers.feeds.utcnow",
        lambda: __import__("datetime").datetime(2026, 10, 22, tzinfo=__import__("datetime").UTC),
    )
    respx.get("https://r.example/feed").mock(return_value=httpx.Response(200, content=RSS))
    respx.get("https://a.example/feed").mock(return_value=httpx.Response(200, content=ATOM))
    respx.get("https://down.example/feed").mock(return_value=httpx.Response(500))
    digest = client.feeds.news(
        days=5,
        sources=["https://r.example/feed", "https://a.example/feed", "https://down.example/feed"],
    )
    assert [i.title for i in digest.items] == ["Ransomware hits X", "Patch CVE-2024-3400 now"]
    assert digest.errors[0].source == "https://down.example/feed"
    only = client.feeds.news(days=5, keyword="ransomware", sources=["https://a.example/feed"])
    assert len(only.items) == 1


@respx.mock
def test_feedly_search_needs_no_token(client):
    respx.get(f"{feedly.API}/search/feeds").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"feedId": "feed/https://k.example/feed", "title": "K", "subscribers": 10}
                ]
            },
        )
    )
    res = client.feedly.search("security")
    assert res[0].feed_id.startswith("feed/")


def test_feedly_stream_requires_token(client):
    with pytest.raises(ConfigError):
        client.feedly.stream("feed/x")


@respx.mock
def test_feedly_stream(client, monkeypatch):
    monkeypatch.setenv("FEEDLY_ACCESS_TOKEN", "tok")
    route = respx.get(f"{feedly.API}/streams/contents").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "i1",
                        "title": "APT29 phishing",
                        "published": 1760000000000,
                        "alternate": [{"href": "https://x.example/a"}],
                        "origin": {"title": "X"},
                        "summary": {"content": "<b>bold</b> text"},
                        "entities": [{"label": "APT29"}],
                    }
                ],
                "continuation": "c2",
            },
        )
    )
    page = client.feedly.stream("feed/x", hours=24)
    assert route.calls[0].request.headers["Authorization"] == "Bearer tok"
    assert page.continuation == "c2"
    art = page.items[0]
    assert art.summary == "bold text" and art.entities == ["APT29"] and art.published.year == 2025
