"""Shared types and helpers for providers."""

from __future__ import annotations

import ipaddress
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Annotated

from pydantic import AfterValidator, Field

_CVE_RE = re.compile(r"^CVE-\d{4}-\d{4,}$")
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?!-)([a-z0-9_-]{1,63}(?<!-)\.)+[a-z][a-z0-9-]{0,62}$")


def _cve(v: str) -> str:
    v = v.strip().upper()
    if not _CVE_RE.match(v):
        raise ValueError(f"not a CVE id: {v!r} (expected CVE-YYYY-NNNN)")
    return v


def _domain(v: str) -> str:
    v = v.strip().lower()
    v = re.sub(r"^[a-z]+://", "", v).split("/")[0].split(":")[0].rstrip(".")
    try:
        v = v.encode("idna").decode()
    except UnicodeError as exc:
        raise ValueError(f"invalid domain: {v!r}") from exc
    if not _DOMAIN_RE.match(v):
        raise ValueError(f"invalid domain: {v!r}")
    return v


def _ip(v: str) -> str:
    try:
        return str(ipaddress.ip_address(v.strip()))
    except ValueError as exc:
        raise ValueError(f"invalid IP address: {v!r}") from exc


CveId = Annotated[
    str, AfterValidator(_cve), Field(description="CVE identifier, e.g. CVE-2021-44228")
]
Domain = Annotated[str, AfterValidator(_domain), Field(description="Domain name, e.g. example.com")]
IPAddress = Annotated[str, AfterValidator(_ip), Field(description="IPv4 or IPv6 address")]


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_dt(value: str | int | float | None) -> datetime | None:
    """Parse ISO strings, epoch seconds or epoch milliseconds into aware datetimes."""
    if value in (None, ""):
        return None
    if isinstance(value, int | float):
        ts = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(ts, UTC)
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Strip tags/scripts and collapse whitespace while keeping paragraph breaks."""
    p = _TextExtractor()
    p.feed(html)
    text = "".join(p.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()
