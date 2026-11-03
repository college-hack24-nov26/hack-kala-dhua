"""Connection pool.

One asyncpg pool for the process, opened on startup and closed on shutdown.
Queries are written as explicit SQL in the repository layer rather than through
an ORM: most of them are PostGIS spatial joins that an ORM would obscure, and
the query plan matters when a run scores every ward in a city.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import asyncpg

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_pool: asyncpg.Pool | None = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Encode and decode json/jsonb straight from Python objects.

    IMPORTANT, and the source of a real bug: this codec handles BOTH
    directions. Pass a dict or a list to a jsonb parameter and it is serialised
    for you. Passing `json.dumps(x)` serialises it twice, and Postgres stores a
    jsonb *string* containing JSON rather than a jsonb object. It reads back as
    `str`, every model that expects a dict fails validation, and the column
    quietly looks fine in psql.

    So: jsonb parameters take Python objects. Only genuine `text` columns, such
    as `agent_steps.tool_input`, take `json.dumps`.
    """
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )
    await conn.set_type_codec(
        "json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def connect() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    if not settings.dsn:
        raise RuntimeError(
            "No database DSN configured. Set SUPABASE_TRANSACTION_POOLER "
            "or SUPABASE_DIRECT_CONNECTION_STRING."
        )
    _pool = await asyncpg.create_pool(
        dsn=settings.dsn,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        command_timeout=30,
        init=_init_connection,
        # pgbouncer in transaction mode cannot use server-side prepared
        # statements; disabling the cache is what makes the pooler usable.
        statement_cache_size=0,
    )
    log.info("db_pool_open", min=settings.db_pool_min, max=settings.db_pool_max)
    return _pool


async def disconnect() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        log.info("db_pool_closed")


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool is not open. Did startup run?")
    return _pool


@asynccontextmanager
async def acquire() -> AsyncIterator[asyncpg.Connection]:
    async with pool().acquire() as conn:
        yield conn


@asynccontextmanager
async def transaction() -> AsyncIterator[asyncpg.Connection]:
    async with pool().acquire() as conn:
        async with conn.transaction():
            yield conn


async def fetch(query: str, *args: Any) -> list[asyncpg.Record]:
    async with acquire() as conn:
        return await conn.fetch(query, *args)


async def fetchrow(query: str, *args: Any) -> asyncpg.Record | None:
    async with acquire() as conn:
        return await conn.fetchrow(query, *args)


async def fetchval(query: str, *args: Any) -> Any:
    async with acquire() as conn:
        return await conn.fetchval(query, *args)


async def execute(query: str, *args: Any) -> str:
    async with acquire() as conn:
        return await conn.execute(query, *args)


async def healthcheck() -> bool:
    try:
        return await fetchval("select 1") == 1
    except Exception as exc:  # noqa: BLE001 - health must never raise
        log.warning("db_healthcheck_failed", error=str(exc))
        return False
