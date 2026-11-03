"""Shared state across replicas, when there is more than one.

Every piece of state that used to live in one process (rate-limit windows,
the LLM cache and token budget, the ingest queue, which replica runs the event
router) has two backends behind the same functions:

* **In-process**, the default. Correct for one replica and costs nothing.
* **Redis**, when `REDIS_URL` is set. Correct for many replicas.

Redis is optional at every call site: if the package is missing, the URL is
empty, or the server stops answering, callers get `None` and use their
in-process path. A disaster API that goes down because its cache did is a
worse failure than one that counts rate limits per replica for a minute.
"""
from __future__ import annotations

import time
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_client: Any = None
_down_until = 0.0
_RETRY_S = 10.0


def client() -> Any:
    """The Redis client, or None. Never raises."""
    global _client, _down_until
    if not settings.redis_url or time.monotonic() < _down_until:
        return None
    if _client is None:
        try:
            import redis.asyncio as aioredis  # optional dependency

            _client = aioredis.from_url(settings.redis_url, decode_responses=False,
                                        socket_timeout=0.25, socket_connect_timeout=0.5,
                                        health_check_interval=30)
        except Exception as exc:  # noqa: BLE001
            log.warning("redis_unavailable", error=str(exc)[:160])
            _down_until = time.monotonic() + _RETRY_S
            return None
    return _client


def mark_down(exc: Exception) -> None:
    """A call failed: use the in-process path for a few seconds."""
    global _down_until
    _down_until = time.monotonic() + _RETRY_S
    log.warning("redis_call_failed", error=f"{type(exc).__name__}: {str(exc)[:120]}")


def enabled() -> bool:
    return client() is not None
