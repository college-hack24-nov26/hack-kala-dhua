"""Supabase JWT verification keys.

Supabase signs user access tokens one of two ways, and which one depends on how
old the project is and whether the signing keys have been rotated:

  * **Legacy, symmetric.** HS256 against the project's shared JWT secret, the
    one that appears in the dashboard as "JWT Secret" and in `.env` as
    `SUPABASE_JWT_KEY`.
  * **Current, asymmetric.** ES256 or RS256 against a private key Supabase
    holds, with the public half published at
    `{SUPABASE_URL}/auth/v1/.well-known/jwks.json`. The token header carries a
    `kid` naming which key signed it.

Supabase is migrating projects from the first to the second, and a project can
be flipped at any time from the dashboard. Hard-coding either one is how an
auth layer works perfectly right up until the day somebody clicks "rotate", so
this module supports both and picks per token, from the token's own header.

Keys are cached. A JWKS fetch on every request would put an outbound HTTP call
in front of every authenticated call in the system, which during a flood is
exactly the wrong place for one.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import jwt
from jwt import PyJWKClient

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

#: How long a fetched key set is trusted before it is refetched. Supabase key
#: rotation is rare and deliberate, so an hour is generous without being stale
#: enough to matter.
JWKS_TTL_SECONDS = 3600

#: Algorithms we will accept. `none` is obviously excluded, and so is every
#: symmetric algorithm when verifying against a public key: accepting HS256
#: against a JWKS public key is the classic algorithm-confusion forgery.
ASYMMETRIC_ALGORITHMS = ("ES256", "RS256", "EdDSA")
SYMMETRIC_ALGORITHMS = ("HS256",)


class KeyUnavailable(RuntimeError):
    """The key needed to verify this token could not be obtained."""


@dataclass(slots=True)
class _JwksCache:
    client: PyJWKClient | None = None
    fetched_at: float = 0.0
    url: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def is_fresh(self, url: str) -> bool:
        return (
            self.client is not None
            and self.url == url
            and (time.monotonic() - self.fetched_at) < JWKS_TTL_SECONDS
        )


_cache = _JwksCache()


def jwks_url() -> str:
    """Derived from SUPABASE_URL so there is one less thing to configure wrong."""
    base = (settings.supabase_url or "").rstrip("/")
    if not base:
        return ""
    return f"{base}/auth/v1/.well-known/jwks.json"


async def _client() -> PyJWKClient:
    url = jwks_url()
    if not url:
        raise KeyUnavailable(
            "SUPABASE_URL is not set, so the JWKS endpoint cannot be derived."
        )
    if _cache.is_fresh(url):
        return _cache.client  # type: ignore[return-value]

    async with _cache.lock:
        # Re-check inside the lock: several requests can arrive together on a
        # cold cache and there is no reason for all of them to fetch.
        if _cache.is_fresh(url):
            return _cache.client  # type: ignore[return-value]
        # PyJWKClient does its own blocking fetch, so keep it off the loop.
        client = await asyncio.to_thread(
            PyJWKClient, url, cache_keys=True, lifespan=JWKS_TTL_SECONDS
        )
        _cache.client = client
        _cache.url = url
        _cache.fetched_at = time.monotonic()
        log.info("jwks_fetched", url=url)
        return client


async def signing_key_for(token: str) -> Any:
    """The public key that signed this token, by its `kid`."""
    client = await _client()
    try:
        return await asyncio.to_thread(client.get_signing_key_from_jwt, token)
    except Exception as exc:  # noqa: BLE001 - any failure here means "no key"
        # A kid we have never seen usually means the keys were rotated after we
        # cached them. Drop the cache and try once more before giving up.
        log.warning("jwks_key_miss", error=str(exc))
        _cache.client = None
        _cache.fetched_at = 0.0
        client = await _client()
        try:
            return await asyncio.to_thread(client.get_signing_key_from_jwt, token)
        except Exception as exc2:  # noqa: BLE001
            raise KeyUnavailable(str(exc2)) from exc2


def unverified_header(token: str) -> dict:
    """Read the header without trusting it.

    Used only to decide *which* verification path to take. Nothing in here is
    treated as a fact: the algorithm we actually verify with is chosen from our
    own allow-list, never from the token.
    """
    try:
        return jwt.get_unverified_header(token)
    except jwt.InvalidTokenError:
        return {}


def reset_cache() -> None:
    """For tests, and for an operator who has just rotated keys."""
    _cache.client = None
    _cache.fetched_at = 0.0
    _cache.url = ""
