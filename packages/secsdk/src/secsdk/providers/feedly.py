"""Feedly Cloud API: your boards/collections and streams, plus keyless feed search.

Set ``FEEDLY_ACCESS_TOKEN`` (or ``SECSDK_FEEDLY_TOKEN``) for account operations.
Developer tokens are available from your Feedly account settings (Enterprise /
Threat Intelligence plans expose the same endpoints with richer entities).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any

from pydantic import AliasChoices, BaseModel, Field
from pydantic_settings import SettingsConfigDict

from ..config import ProviderSettings
from ..operation import operation
from ..provider import AuthStatus, Provider
from ._common import html_to_text, parse_dt, utcnow

API = "https://api.feedly.com/v3"


class FeedlySettings(ProviderSettings):
    model_config = SettingsConfigDict(env_prefix="SECSDK_FEEDLY_")
    token: str | None = Field(
        None,
        validation_alias=AliasChoices("SECSDK_FEEDLY_TOKEN", "FEEDLY_ACCESS_TOKEN", "FEEDLY_TOKEN"),
    )
    base_url: str = API


class FeedSearchResult(BaseModel):
    feed_id: str
    title: str
    website: str | None = None
    subscribers: int | None = None
    description: str | None = None


class Collection(BaseModel):
    id: str
    label: str
    feeds: int = 0
    description: str | None = None


class Article(BaseModel):
    id: str
    title: str
    url: str | None = None
    published: datetime | None = None
    source: str | None = None
    summary: str = ""
    keywords: list[str] = []
    entities: list[str] = Field(
        default=[], description="Feedly-detected entities (threat actors, malware, CVEs...)"
    )


class StreamPage(BaseModel):
    items: list[Article]
    continuation: str | None = Field(
        None, description="Pass back as --continuation for the next page"
    )


class Feedly(Provider):
    name = "feedly"
    title = "Feedly"
    description = (
        "Feedly boards, collections and streams (token), and public feed discovery (no token)."
    )
    docs_url = "https://developers.feedly.com/"
    settings_class = FeedlySettings
    settings: FeedlySettings
    rate_limit = (10, 1.0)

    def auth_status(self) -> AuthStatus:
        ok = bool(self.settings.token)
        return AuthStatus(
            required=True,
            configured=ok,
            missing=[] if ok else ["FEEDLY_ACCESS_TOKEN"],
            note="`feedly.search` works without a token; other operations need FEEDLY_ACCESS_TOKEN.",
        )

    def _get(self, path: str, params: dict[str, Any] | None = None, auth: bool = True) -> Any:
        headers = {"Authorization": f"Bearer {self.settings.token}"} if auth else None
        return self.http.get_json(
            f"{self.settings.base_url}{path}", params=params, headers=headers, limiter=self.limiter
        )

    @operation(examples=["secctl feedly search 'threat intelligence' --limit 10"])
    def search(
        self,
        query: Annotated[str, Field(min_length=2, description="Topic, site or keyword")],
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> list[FeedSearchResult]:
        """Discover public feeds by topic or site. No token required."""
        data = self._get("/search/feeds", {"query": query, "count": limit}, auth=False)
        return [
            FeedSearchResult(
                feed_id=r.get("feedId") or r.get("id", ""),
                title=r.get("title", ""),
                website=r.get("website"),
                subscribers=r.get("subscribers"),
                description=r.get("description"),
            )
            for r in data.get("results", [])
        ]

    @operation(auth=True, examples=["secctl feedly profile"])
    def profile(self) -> dict[str, Any]:
        """Token check: return the authenticated user's id, email and plan."""
        p = self._get("/profile")
        return {
            k: p.get(k)
            for k in ("id", "email", "fullName", "product", "client")
            if p.get(k) is not None
        }

    @operation(auth=True, examples=["secctl feedly collections"])
    def collections(self) -> list[Collection]:
        """Your Feedly collections (folders). Use the id as a stream id."""
        data = self._get("/collections")
        return [
            Collection(
                id=c["id"],
                label=c.get("label", ""),
                feeds=len(c.get("feeds", [])),
                description=c.get("description"),
            )
            for c in data
        ]

    @operation(
        auth=True,
        examples=[
            "secctl feedly stream 'user/<id>/category/global.all' --hours 24",
            "secctl feedly stream 'feed/https://krebsonsecurity.com/feed/' --limit 5",
        ],
    )
    def stream(
        self,
        stream_id: Annotated[
            str, Field(description="Feed, collection or board id (feed/..., user/.../category/...)")
        ],
        hours: Annotated[
            int | None, Field(ge=1, le=24 * 31, description="Only items newer than N hours")
        ] = 24,
        limit: Annotated[int, Field(ge=1, le=1000)] = 20,
        unread_only: bool = False,
        continuation: Annotated[
            str | None, Field(description="Continuation token from a previous page")
        ] = None,
    ) -> StreamPage:
        """Articles from a feed, collection or board, newest first."""
        params: dict[str, Any] = {
            "streamId": stream_id,
            "count": limit,
            "continuation": continuation,
        }
        if hours:
            params["newerThan"] = int((utcnow() - timedelta(hours=hours)).timestamp() * 1000)
        if unread_only:
            params["unreadOnly"] = "true"
        data = self._get("/streams/contents", params)
        return StreamPage(
            items=[parse_article(i) for i in data.get("items", [])],
            continuation=data.get("continuation"),
        )


def parse_article(item: dict[str, Any]) -> Article:
    url = next(
        (a.get("href") for a in item.get("alternate", []) if a.get("href")), None
    ) or item.get("originId")
    summary = (item.get("summary") or item.get("content") or {}).get("content", "")
    entities = [e.get("label") for e in item.get("entities", []) if e.get("label")]
    return Article(
        id=item["id"],
        title=item.get("title", ""),
        url=url if url and url.startswith("http") else None,
        published=parse_dt(item.get("published")),
        source=(item.get("origin") or {}).get("title"),
        summary=html_to_text(summary)[:1000],
        keywords=item.get("keywords", []),
        entities=entities,
    )
