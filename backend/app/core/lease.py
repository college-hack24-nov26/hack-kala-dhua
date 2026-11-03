"""A leader lease: exactly one replica runs a singleton loop.

    hold("event_router") -> (am I the leader?, meta)

One UPDATE renews (if I hold it) or steals (if it expired); if no row exists
an INSERT creates it. Plain statements, so it works through the transaction
pooler where advisory locks do not. `meta` carries state the next leader
needs (the router's cursor).

If migration 028 has not been applied the table is missing; this process then
behaves as the only replica, which is exactly the behaviour before the lease
existed, and says so once in the log.
"""
from __future__ import annotations

import os
import socket
import uuid
from typing import Any

from app.core.logging import get_logger
from app.db import session as db

log = get_logger(__name__)

HOLDER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
_missing_logged = False


async def hold(name: str, ttl_s: int, meta: dict[str, Any] | None = None) -> tuple[bool, dict[str, Any]]:
    global _missing_logged
    try:
        row = await db.fetchrow(
            """
            update service_leases
               set holder = $2, expires_at = now() + make_interval(secs => $3),
                   meta = coalesce($4::jsonb, meta), updated_at = now()
             where name = $1 and (holder = $2 or expires_at < now())
            returning meta
            """,
            name, HOLDER, float(ttl_s), meta,
        )
        if row is not None:
            return True, dict(row["meta"] or {})
        row = await db.fetchrow(
            """
            insert into service_leases (name, holder, expires_at, meta)
            values ($1, $2, now() + make_interval(secs => $3), coalesce($4::jsonb, '{}'::jsonb))
            on conflict (name) do nothing
            returning meta
            """,
            name, HOLDER, float(ttl_s), meta,
        )
        return (row is not None), (dict(row["meta"] or {}) if row else {})
    except Exception as exc:  # noqa: BLE001
        if "service_leases" in str(exc) and not _missing_logged:
            _missing_logged = True
            log.warning("lease_table_missing", name=name,
                        note="apply migration 028; running as the only replica")
        return True, {}


async def release(name: str) -> None:
    try:
        await db.execute("update service_leases set expires_at = now() where name = $1 and holder = $2",
                         name, HOLDER)
    except Exception:  # noqa: BLE001
        pass
