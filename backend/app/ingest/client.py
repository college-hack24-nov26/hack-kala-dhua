"""Resilient upstream fetching.

Every external feed goes through here, and the contract is the same for all of
them: *never raise*. An upstream failure returns the last good value with
`live=False` so the run degrades to cached data and the console shows the feed
as stale, rather than the whole hazard run failing because one API blinked.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class FeedResult:
    data: Any | None
    live: bool
    fetched_at: float
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.data is not None


@dataclass(slots=True)
class _CacheEntry:
    value: Any
    stored_at: float


class FeedCache:
    """Small in-process TTL cache.

    Deliberately not Redis: a single API instance serving one city does not
    need a network hop to remember a rainfall forecast for fifteen minutes.
    Swap the implementation here if the deployment ever becomes multi-instance.
    """

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, _CacheEntry] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str, *, allow_stale: bool = False) -> Any | None:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if allow_stale or (time.monotonic() - entry.stored_at) < self._ttl:
                return entry.value
            return None

    async def set(self, key: str, value: Any) -> None:
        async with self._lock:
            self._store[key] = _CacheEntry(value=value, stored_at=time.monotonic())


_cache = FeedCache(settings.feed_cache_ttl_seconds)
_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.feed_timeout_seconds),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            headers={"User-Agent": "Indradhanu/0.1 (PCCOE Grand Challenge 2026)"},
            follow_redirects=True,
        )
    return _client


async def close_http() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=0.4, max=3.0),
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    reraise=True,
)
async def _get_json(url: str, params: dict[str, Any] | None) -> Any:
    response = await _http().get(url, params=params)
    response.raise_for_status()
    return response.json()


async def get_json(
    url: str,
    params: dict[str, Any] | None = None,
    *,
    cache_key: str | None = None,
) -> FeedResult:
    """Fetch JSON, falling back to cache. Never raises."""
    key = cache_key or f"{url}?{sorted((params or {}).items())}"

    fresh = await _cache.get(key)
    if fresh is not None:
        return FeedResult(data=fresh, live=True, fetched_at=time.time())

    try:
        data = await _get_json(url, params)
        await _cache.set(key, data)
        return FeedResult(data=data, live=True, fetched_at=time.time())
    except Exception as exc:  # noqa: BLE001 - this boundary must not raise
        stale = await _cache.get(key, allow_stale=True)
        log.warning(
            "feed_unavailable",
            url=url,
            error=str(exc)[:200],
            served_stale=stale is not None,
        )
        return FeedResult(
            data=stale,
            live=False,
            fetched_at=time.time(),
            error=type(exc).__name__,
        )
