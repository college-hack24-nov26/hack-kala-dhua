"""Per-agent memory, with access control and a ledger.

Every agent in the graph gets a `ScopedMemory` bound to its name. What it may
read and write is fixed here, in one table, not decided by the agent:

    namespace   what lives there                       who may write   who may read
    ---------   ------------------------------------   -------------   -----------------------------
    orders      officers' standing orders ("hold        officers only   every agent (read-only)
                boat 3 at Sangam", "no re-tasking
                away from the hospital")
    triage      what triage has seen                    triage          triage, commander
    rescue      the rescue planner's episodes           rescue          rescue, planner, commander
    medical     the medical planner's episodes          medical         medical, planner, commander
    logistics   the logistics planner's episodes        logistics       logistics, planner, commander
    planner     solver outcomes                         planner         planner, gate, commander
    gate        approvals and rejections by officers    gate            gate, planner, commander
    commander   the LLM Commander's lessons             commander       commander
    police      confidential                            (nobody here)   (nobody here)

A read or write outside an agent's rights is refused, raises
`MemoryAccessDenied`, and is written to the ledger as `denied`. Every allowed
read and write is also in the ledger, which the console shows, so "why did the
gate ask again?" can be answered by what it remembered.

Storage is the existing `agent_memory` table (`namespace` column, migration
024), with an in-process fallback when the database is unavailable, so an
agent never fails because memory did.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)

NAMESPACES = ("orders", "triage", "rescue", "medical", "logistics", "planner", "gate",
              "commander", "police")

ACCESS: dict[str, dict[str, frozenset[str]]] = {
    "triage":    {"read": frozenset({"triage", "orders"}), "write": frozenset({"triage"})},
    "rescue":    {"read": frozenset({"rescue", "orders"}), "write": frozenset({"rescue"})},
    "medical":   {"read": frozenset({"medical", "orders"}), "write": frozenset({"medical"})},
    "logistics": {"read": frozenset({"logistics", "orders"}), "write": frozenset({"logistics"})},
    "planner":   {"read": frozenset({"planner", "orders", "rescue", "medical", "logistics", "gate"}),
                  "write": frozenset({"planner"})},
    "gate":      {"read": frozenset({"gate", "orders", "planner"}), "write": frozenset({"gate"})},
    "commander": {"read": frozenset(set(NAMESPACES) - {"police"}), "write": frozenset({"commander"})},
    # Officers write standing orders through the console, not through an agent.
    "officer":   {"read": frozenset(set(NAMESPACES) - {"police"}), "write": frozenset({"orders"})},
}

#: agent_memory.scope has a fixed check constraint; map namespaces onto it.
_SCOPE = {"orders": "standing_order", "gate": "lesson", "commander": "lesson"}


class MemoryAccessDenied(PermissionError):
    pass


@dataclass(slots=True)
class LedgerEntry:
    at: float
    agent: str
    op: str
    namespace: str
    memory_id: str | None
    detail: str
    run_id: str | None


LEDGER: deque[LedgerEntry] = deque(maxlen=500)
#: In-process fallback store: namespace -> list of memories.
_LOCAL: dict[str, list[dict[str, Any]]] = {}
_down_until = 0.0


def _db_ok() -> bool:
    return time.monotonic() >= _down_until


async def _ledger(entry: LedgerEntry, city_id: str) -> None:
    LEDGER.appendleft(entry)
    if not _db_ok():
        return
    try:
        from app.db import session as db

        await db.execute(
            """insert into agent_memory_ledger (agent, op, namespace, memory_id, detail, run_id, city_id)
               values ($1,$2,$3,$4,$5,$6,$7)""",
            entry.agent, entry.op, entry.namespace, entry.memory_id, entry.detail[:300],
            entry.run_id, city_id,
        )
    except Exception as exc:  # noqa: BLE001 - the ledger is best-effort, the ring is not
        _down(exc)


def _down(exc: Exception, seconds: float = 60.0) -> None:
    """Stop trying the database for a minute; the in-process store carries on."""
    global _down_until
    _down_until = time.monotonic() + seconds
    log.info("agent_memory_db_unavailable", error=type(exc).__name__)


class ScopedMemory:
    def __init__(self, agent: str, *, city_id: str = "pune", run_id: str | None = None) -> None:
        if agent not in ACCESS:
            raise MemoryAccessDenied(f"unknown agent {agent!r}")
        self.agent, self.city_id, self.run_id = agent, city_id, run_id
        self.rights = ACCESS[agent]

    async def _deny(self, op: str, ns: str) -> None:
        await _ledger(LedgerEntry(time.time(), self.agent, "denied", ns, None,
                                  f"{op} refused", self.run_id), self.city_id)
        raise MemoryAccessDenied(f"{self.agent} may not {op} {ns!r}")

    async def recall(self, namespaces: list[str] | tuple[str, ...] | None = None, *,
                     about: str | None = None, limit: int = 5,
                     max_age_s: int | None = None) -> list[dict[str, Any]]:
        wanted = list(namespaces) if namespaces else sorted(self.rights["read"])
        for ns in wanted:
            if ns not in self.rights["read"]:
                await self._deny("read", ns)
        rows: list[dict[str, Any]] = []
        use_db = _db_ok()
        if use_db:
            try:
                from app.db import session as db

                recs = await db.fetch(
                    """
                    select id::text, namespace, content, data, importance, created_by,
                           extract(epoch from created_at)::float at
                      from agent_memory
                     where city_id = $1 and namespace = any($2::text[])
                       and (expires_at is null or expires_at > now())
                       and ($3::text is null or content ilike '%' || $3 || '%'
                            or data::text ilike '%' || $3 || '%')
                       and ($4::int is null or created_at > now() - make_interval(secs => $4::int))
                     order by importance desc, created_at desc
                     limit $5
                    """,
                    self.city_id, wanted, about, max_age_s, limit,
                )
                rows = [dict(r) for r in recs]
            except Exception as exc:  # noqa: BLE001
                _down(exc)
                use_db = False
        if not use_db:
            now = time.time()
            for ns in wanted:
                for m in _LOCAL.get(ns, []):
                    if about and about.lower() not in (m["content"] + str(m["data"])).lower():
                        continue
                    if max_age_s and now - m["at"] > max_age_s:
                        continue
                    if m.get("expires") and m["expires"] < now:
                        continue
                    rows.append(m)
            rows = sorted(rows, key=lambda m: (-m["importance"], -m["at"]))[:limit]
        for ns in set(r["namespace"] for r in rows) or set(wanted[:1]):
            await _ledger(LedgerEntry(time.time(), self.agent, "read", ns, None,
                                      f"{sum(1 for r in rows if r['namespace'] == ns)} recalled"
                                      + (f" about {about}" if about else ""), self.run_id), self.city_id)
        return rows

    async def remember(self, namespace: str, content: str, *, data: dict | None = None,
                       importance: int = 3, ttl_minutes: int | None = 360) -> str | None:
        if namespace not in self.rights["write"]:
            await self._deny("write", namespace)
        content = " ".join((content or "").split())[:500]
        if len(content) < 3:
            return None
        mid: str | None = None
        use_db = _db_ok()
        if use_db:
            try:
                from app.db import session as db

                mid = await db.fetchval(
                    """
                    insert into agent_memory
                      (city_id, scope, namespace, content, data, importance, source, created_by, expires_at)
                    values ($1,$2,$3,$4,$5::jsonb,$6,'agent_graph',$7,
                            case when $8::int is null then null else now() + make_interval(mins => $8::int) end)
                    returning id::text
                    """,
                    self.city_id, _SCOPE.get(namespace, "episode"), namespace, content,
                    data or {}, max(1, min(5, importance)), f"agent:{self.agent}", ttl_minutes,
                )
            except Exception as exc:  # noqa: BLE001
                _down(exc)
                use_db = False
        if not use_db:
            mid = f"local-{len(_LOCAL.get(namespace, [])) + 1}"
            _LOCAL.setdefault(namespace, []).append({
                "id": mid, "namespace": namespace, "content": content, "data": data or {},
                "importance": importance, "created_by": f"agent:{self.agent}", "at": time.time(),
                "expires": time.time() + ttl_minutes * 60 if ttl_minutes else None,
            })
        await _ledger(LedgerEntry(time.time(), self.agent, "write", namespace, mid, content[:160],
                                  self.run_id), self.city_id)
        return mid


def ledger(limit: int = 100) -> list[dict[str, Any]]:
    return [
        {"at": e.at, "agent": e.agent, "op": e.op, "namespace": e.namespace,
         "memoryId": e.memory_id, "detail": e.detail, "runId": e.run_id}
        for e in list(LEDGER)[:limit]
    ]


def access_map() -> dict[str, dict[str, list[str]]]:
    return {a: {"read": sorted(r["read"]), "write": sorted(r["write"])} for a, r in ACCESS.items()}


def reset_for_tests() -> None:
    """Empty the ledger and local store and keep the database off for an hour."""
    LEDGER.clear()
    _LOCAL.clear()
    _down(RuntimeError("tests"), seconds=3600)
