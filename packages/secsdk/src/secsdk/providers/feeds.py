"""RSS/Atom security news, keyless. Parsed with defusedxml (feeds are untrusted XML)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Annotated
from xml.etree.ElementTree import Element

from defusedxml import ElementTree
from pydantic import BaseModel, Field

from ..errors import UpstreamError
from ..operation import operation
from ..provider import Provider
from ._common import html_to_text, parse_dt, utcnow

#: Curated default sources. Override per call with --sources.
DEFAULT_SOURCES: dict[str, str] = {
    "cisa-advisories": "https://www.cisa.gov/cybersecurity-advisories/all.xml",
    "cisa-news": "https://www.cisa.gov/news.xml",
    "krebs": "https://krebsonsecurity.com/feed/",
    "bleepingcomputer": "https://www.bleepingcomputer.com/feed/",
    "thehackernews": "https://feeds.feedburner.com/TheHackersNews",
    "sans-isc": "https://isc.sans.edu/rssfeed_full.xml",
}

ATOM = "{http://www.w3.org/2005/Atom}"


class FeedItem(BaseModel):
    source: str
    title: str
    url: str | None = None
    published: datetime | None = None
    summary: str = ""
    cves: list[str] = Field(default=[], description="CVE ids mentioned in title or summary")


class FeedError(BaseModel):
    source: str
    error: str


class NewsDigest(BaseModel):
    items: list[FeedItem]
    errors: list[FeedError] = []


class Feeds(Provider):
    name = "feeds"
    title = "RSS/Atom feeds"
    description = (
        "Security news and advisories from public RSS/Atom feeds (CISA, Krebs, SANS ISC, ...)."
    )
    rate_limit = (10, 1.0)

    @operation
    def sources(self) -> dict[str, str]:
        """The built-in feed catalogue (name -> URL)."""
        return DEFAULT_SOURCES

    @operation(examples=["secctl feeds read https://krebsonsecurity.com/feed/ --limit 5"])
    def read(
        self,
        url: Annotated[str, Field(description="Feed URL or a built-in source name")],
        limit: Annotated[int, Field(ge=1, le=500)] = 20,
    ) -> list[FeedItem]:
        """Read one RSS or Atom feed."""
        source, feed_url = (url, DEFAULT_SOURCES[url]) if url in DEFAULT_SOURCES else (url, url)
        if not feed_url.startswith(("http://", "https://")):
            raise UpstreamError(
                f"Not a URL or known source: {url}", hint="See `secctl feeds sources`."
            )
        resp = self.http.request("GET", feed_url, limiter=self.limiter, cache_ttl=900)
        return parse_feed(resp.content, source)[:limit]

    @operation(
        examples=[
            "secctl feeds news --days 2",
            "secctl feeds news --keyword ransomware --sources krebs",
        ]
    )
    def news(
        self,
        days: Annotated[
            int, Field(ge=1, le=60, description="Items published in the last N days")
        ] = 3,
        keyword: Annotated[
            str | None, Field(description="Case-insensitive filter on title/summary")
        ] = None,
        sources: Annotated[
            list[str], Field(description="Source names or URLs (default: all built-ins)")
        ] = [],
        limit: Annotated[int, Field(ge=1, le=1000)] = 50,
    ) -> NewsDigest:
        """Merged, de-duplicated digest across feeds. Failing feeds are reported, not fatal."""
        cutoff = utcnow() - timedelta(days=days)
        items: list[FeedItem] = []
        errors: list[FeedError] = []
        for src in sources or list(DEFAULT_SOURCES):
            try:
                items.extend(self.read(src, limit=200))
            except Exception as exc:
                errors.append(FeedError(source=src, error=str(exc)))
        kw = keyword.lower() if keyword else None
        seen: set[str] = set()
        out = []
        for it in sorted(items, key=lambda i: i.published or cutoff, reverse=True):
            key = (it.url or it.title).lower()
            if key in seen or (it.published and it.published < cutoff):
                continue
            if kw and kw not in (it.title + " " + it.summary).lower():
                continue
            seen.add(key)
            out.append(it)
        return NewsDigest(items=out[:limit], errors=errors)


# ----- parsing -------------------------------------------------------------------------


def parse_feed(content: bytes, source: str) -> list[FeedItem]:
    try:
        root = ElementTree.fromstring(content)
    except Exception as exc:
        raise UpstreamError(f"{source}: not a valid RSS/Atom document ({exc})") from exc
    if root.tag == f"{ATOM}feed":
        return [_atom_item(e, source) for e in root.findall(f"{ATOM}entry")]
    channel = root.find("channel")
    nodes = channel.findall("item") if channel is not None else root.findall(".//item")
    return [_rss_item(e, source) for e in nodes]


def _text(el: Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def _rss_item(e: Element, source: str) -> FeedItem:
    summary = html_to_text(_text(e.find("description")))[:1000]
    title = html_to_text(_text(e.find("title")))
    return FeedItem(
        source=source,
        title=title,
        url=_text(e.find("link")) or None,
        published=_rss_date(_text(e.find("pubDate"))),
        summary=summary,
        cves=_cves(title + " " + summary),
    )


def _atom_item(e: Element, source: str) -> FeedItem:
    link = None
    for ln in e.findall(f"{ATOM}link"):
        if ln.get("rel", "alternate") == "alternate":
            link = ln.get("href")
            break
    summary = html_to_text(_text(e.find(f"{ATOM}summary")) or _text(e.find(f"{ATOM}content")))[
        :1000
    ]
    title = html_to_text(_text(e.find(f"{ATOM}title")))
    return FeedItem(
        source=source,
        title=title,
        url=link,
        published=parse_dt(_text(e.find(f"{ATOM}published")) or _text(e.find(f"{ATOM}updated"))),
        summary=summary,
        cves=_cves(title + " " + summary),
    )


def _rss_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return parse_dt(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _cves(text: str) -> list[str]:
    return sorted({m.upper() for m in re.findall(r"CVE-\d{4}-\d{4,}", text, flags=re.I)})
