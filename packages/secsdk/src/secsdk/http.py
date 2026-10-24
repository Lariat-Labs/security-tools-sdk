"""Shared HTTP layer: retries, backoff, rate limits, caching and error mapping.

Providers never touch ``httpx`` directly; they call :class:`HttpClient` so every
source gets the same behaviour and the same error types.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any

import httpx

from .cache import FileCache
from .config import CoreSettings
from .errors import AuthError, NotFoundError, RateLimitedError, UpstreamError
from .ratelimit import RateLimiter

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_BACKOFF = 30.0


class HttpClient:
    def __init__(
        self, settings: CoreSettings, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.settings = settings
        self.cache = FileCache(settings.cache_dir, enabled=settings.cache_enabled)
        self._client = httpx.Client(
            timeout=settings.timeout,
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent},
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        json_body: Any = None,
        data: Mapping[str, Any] | None = None,
        limiter: RateLimiter | None = None,
        cache_ttl: float | None = None,
        timeout: float | None = None,
        ok_statuses: tuple[int, ...] = (200, 201, 202),
    ) -> httpx.Response:
        """Send a request and return the response, or raise a mapped SDK error."""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        cache_key = None
        if cache_ttl and method.upper() == "GET":
            cache_key = f"{method} {url} {sorted(params.items())}"
            hit = self.cache.get(cache_key, cache_ttl)
            if hit:
                status, body = hit
                return httpx.Response(status, text=body, request=httpx.Request(method, url))

        last_exc: Exception | None = None
        resp: httpx.Response | None = None
        for attempt in range(self.settings.max_retries + 1):
            if limiter and self.settings.rate_limit:
                limiter.acquire()
            try:
                resp = self._client.request(
                    method,
                    url,
                    params=params,
                    headers=dict(headers or {}),
                    json=json_body,
                    data=data,
                    timeout=timeout or self.settings.timeout,
                )
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt < self.settings.max_retries:
                    time.sleep(_backoff(attempt))
                    continue
                raise UpstreamError(f"Network error calling {_host(url)}: {exc}") from exc

            if resp.status_code in RETRY_STATUSES and attempt < self.settings.max_retries:
                time.sleep(_retry_after(resp) or _backoff(attempt))
                continue
            break

        assert resp is not None, last_exc
        _raise_for_status(resp, ok_statuses)
        if cache_key:
            self.cache.set(cache_key, resp.status_code, resp.text)
        return resp

    def get_json(self, url: str, **kw: Any) -> Any:
        return parse_json(self.request("GET", url, **kw))

    def post_json(self, url: str, **kw: Any) -> Any:
        return parse_json(self.request("POST", url, **kw))


def parse_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise UpstreamError(
            f"{_host(str(resp.request.url))} returned non-JSON content",
            status=resp.status_code,
            details=resp.text[:300],
        ) from exc


def _raise_for_status(resp: httpx.Response, ok: tuple[int, ...]) -> None:
    if resp.status_code in ok:
        return
    host = _host(str(resp.request.url))
    snippet = resp.text[:300] if resp.text else None
    if resp.status_code in (401, 403):
        raise AuthError(
            f"{host} rejected the request ({resp.status_code})",
            hint="Check the provider's credentials and permissions (`secctl providers`).",
            details=snippet,
        )
    if resp.status_code == 404:
        raise NotFoundError(f"{host}: not found", details=snippet)
    if resp.status_code == 429:
        raise RateLimitedError(f"{host} rate limited the request", status=429, details=snippet)
    raise UpstreamError(
        f"{host} returned HTTP {resp.status_code}", status=resp.status_code, details=snippet
    )


def _backoff(attempt: int) -> float:
    return min(0.5 * (2**attempt), MAX_BACKOFF)


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if value and value.isdigit():
        return min(float(value), MAX_BACKOFF)
    return None


def _host(url: str) -> str:
    return httpx.URL(url).host or url
