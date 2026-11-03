"""The event log.

Every state change in the system is appended here before anyone reads it back.
One table doing four jobs:

  * the PS20 activity and audit log
  * the trigger for reactive replanning (a new event is what wakes the planner)
  * the tape a replay reads back
  * the causal chain the console shows when an officer asks "why did that boat
    get re-tasked?"

Two timestamps, and the difference matters. `occurred_at` is world time: when
the thing happened, on whichever clock the scope is running. `recorded_at` is
wall time: when it reached us. They are usually a millisecond apart and
occasionally forty minutes, which is exactly the case a relayed report from a
device that was offline creates. Ordering by `occurred_at` and auditing by
`recorded_at` is what keeps a late arrival from being treated as a new fact.

The table is append-only, enforced by a trigger rather than by convention. An
audit log you can quietly rewrite is not an audit log.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from app.core.logging import get_logger
from app.db import session as db
from app.world.clock import Clock

log = get_logger(__name__)


# --------------------------------------------------------------- event kinds --
# Plain strings, not an enum: the same reasoning as the taxonomy. A new hazard
# or a new channel brings new kinds, and that should not need a type migration.
# These constants exist so the ones we emit today are spelled consistently.
class Kind:
    RUN_STARTED = "run.started"
    RUN_FINISHED = "run.finished"
    #: The world was put back to its opening position. The log is append-only,
    #: so this is the mark that says where the current run begins rather than a
    #: record of something being erased.
    WORLD_RESET = "world.reset"
    RISK_UPDATED = "risk.updated"
    FEED_DEGRADED = "feed.degraded"

    REPORT_RECEIVED = "report.received"
    REPORT_LINKED = "report.linked"
    REPORT_REJECTED = "report.rejected"
    #: Stored but not acted on: nothing could tell what it describes. It opens no
    #: incident and joins none until a person classifies it.
    REPORT_HELD = "report.held"
    REPORT_CLASSIFIED = "report.classified"

    INCIDENT_OPENED = "incident.opened"
    INCIDENT_SEVERITY_CHANGED = "incident.severity_changed"
    INCIDENT_RESOLVED = "incident.resolved"

    ROAD_BLOCKED = "road.blocked"
    ROAD_CLEARED = "road.cleared"

    RESOURCE_STATUS_CHANGED = "resource.status_changed"

    PLAN_GENERATED = "plan.generated"
    ASSIGNMENT_CREATED = "assignment.created"
    ASSIGNMENT_CHANGED = "assignment.changed"
    ASSIGNMENT_CANCELLED = "assignment.cancelled"
    DEMAND_UNCOVERED = "demand.uncovered"

    DECISION_PROPOSED = "decision.proposed"
    DECISION_GATED = "decision.gated"
    DECISION_ACTED = "decision.acted"

    ALERT_ISSUED = "alert.issued"
    TASK_CREATED = "task.created"
    TASK_UPDATED = "task.updated"


# ------------------------------------------------------------------ actors ---
def agent(name: str) -> str:
    return f"agent:{name}"


def officer(name: str) -> str:
    return f"officer:{name}"


def citizen(user_id: str | None) -> str:
    return f"citizen:{user_id or 'anonymous'}"


def feed(feed_id: str) -> str:
    return f"feed:{feed_id}"


SIM = "sim"
SYSTEM = "system"


@dataclass(slots=True)
class Appended:
    id: int
    kind: str
    occurred_at: datetime


_INSERT = """
insert into events
  (city_id, sim_run_id, occurred_at, kind, actor, subject_type, subject_id,
   ward_id, payload, causation_id)
values ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10)
returning id, kind, occurred_at
"""


async def append(
    *,
    clock: Clock,
    kind: str,
    actor: str,
    subject_type: str,
    subject_id: str,
    city_id: str = "pune",
    ward_id: str | None = None,
    payload: dict[str, Any] | None = None,
    caused_by: int | None = None,
    conn: Any = None,
) -> Appended:
    """Append one event. `caused_by` is what makes the chain reconstructable.

    Takes an optional connection so a caller inside a transaction writes its
    events in the same transaction as the state change they describe. An event
    that survives a rolled-back write would be a lie.
    """
    args = (
        city_id,
        clock.sim_run_id,
        clock.now(),
        kind,
        actor,
        subject_type,
        str(subject_id),
        ward_id,
        # A dict, not json.dumps(dict): the pool's codec serialises jsonb
        # parameters itself. See app/db/session.py.
        payload or {},
        caused_by,
    )
    row = await (conn.fetchrow(_INSERT, *args) if conn else db.fetchrow(_INSERT, *args))
    return Appended(id=row["id"], kind=row["kind"], occurred_at=row["occurred_at"])


async def append_many(
    *,
    clock: Clock,
    rows: Sequence[dict[str, Any]],
    city_id: str = "pune",
    conn: Any = None,
) -> list[int]:
    """Append a batch in one round trip. Same shape as `append` per row."""
    import json

    if not rows:
        return []
    now = clock.now()
    records = [
        (
            r.get("city_id", city_id),
            clock.sim_run_id,
            r.get("occurred_at", now),
            r["kind"],
            r["actor"],
            r["subject_type"],
            str(r["subject_id"]),
            r.get("ward_id"),
            json.dumps(r.get("payload") or {}),  # text[] then ::jsonb in SQL
            r.get("caused_by"),
        )
        for r in rows
    ]
    sql = """
    insert into events
      (city_id, sim_run_id, occurred_at, kind, actor, subject_type, subject_id,
       ward_id, payload, causation_id)
    select x.city_id, x.sim_run_id, x.occurred_at, x.kind, x.actor,
           x.subject_type, x.subject_id, x.ward_id, x.payload::jsonb, x.causation_id
      from unnest($1::text[], $2::uuid[], $3::timestamptz[], $4::text[], $5::text[],
                  $6::text[], $7::text[], $8::text[], $9::text[], $10::bigint[])
        as x(city_id, sim_run_id, occurred_at, kind, actor, subject_type,
             subject_id, ward_id, payload, causation_id)
    returning id
    """
    cols = list(zip(*records))
    fetch = conn.fetch if conn else db.fetch
    out = await fetch(sql, *cols)
    return [r["id"] for r in out]


# ------------------------------------------------------------------- reads ---
_SELECT = """
select id, city_id, sim_run_id::text, occurred_at, recorded_at, kind, actor,
       subject_type, subject_id, ward_id, payload, causation_id
  from events
"""


async def recent(
    *,
    sim_run_id: str | None = None,
    limit: int = 200,
    kinds: Sequence[str] | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
) -> list[dict]:
    """The audit log view. Newest first."""
    where = ["sim_run_id is not distinct from $1::uuid"]
    args: list[Any] = [sim_run_id]
    if kinds:
        args.append(list(kinds))
        where.append(f"kind = any(${len(args)})")
    if subject_type:
        args.append(subject_type)
        where.append(f"subject_type = ${len(args)}")
    if subject_id:
        args.append(str(subject_id))
        where.append(f"subject_id = ${len(args)}")
    args.append(limit)
    sql = f"{_SELECT} where {' and '.join(where)} order by id desc limit ${len(args)}"
    return [dict(r) for r in await db.fetch(sql, *args)]


async def chain(event_id: int) -> list[dict]:
    """Walk the causation chain back to its root.

    This is the answer to "why did this happen?", and it is a single recursive
    query because the causal link was recorded at write time rather than
    inferred afterwards.
    """
    sql = f"""
    with recursive up as (
      select * from events where id = $1
      union all
      select e.* from events e join up on up.causation_id = e.id
    )
    select id, city_id, sim_run_id::text, occurred_at, recorded_at, kind, actor,
           subject_type, subject_id, ward_id, payload, causation_id
      from up order by id
    """
    return [dict(r) for r in await db.fetch(sql, event_id)]


async def replay_stream(sim_run_id: str, *, after_id: int = 0) -> list[dict]:
    """Events in the order the world produced them, for replay.

    Ordered by `occurred_at` then id, not by `recorded_at`: replay has to see
    the world as it happened, including a relayed report that arrived late.
    """
    sql = f"""
    {_SELECT} where sim_run_id = $1::uuid and id > $2
     order by occurred_at, id
    """
    return [dict(r) for r in await db.fetch(sql, sim_run_id, after_id)]
