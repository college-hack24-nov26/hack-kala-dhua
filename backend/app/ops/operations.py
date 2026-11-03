"""Operations: what every unit is doing, and how a person stops or changes it.

An "operation" is one committed unit and the job it is on: the assignment row,
the road it is driving, how far along it is, who put it there and why, and
anything an officer has said about it since. This module is the one place that
answers three questions an officer asks and the rest of the system did not:

  * **What is going on?**  `active()` — every operation, live, with its reason.
  * **Stop that.**         `cancel` — take a unit off its job.
  * **Do this instead.**   the `instead` half of a cancel:

        replan          let the planner cover the job with someone else
        redirect        send this unit to a different incident
        stage           send it to a ward and have it wait there
        hold            keep it where it is, out of the plan, for N minutes
        return_to_base  send it home

Why cancel is not just `update assignments set status = 'cancelled'`:

  1. The next re-plan, six seconds later, would see a free unit next to an
     uncovered demand and send it straight back. So a cancel writes a `forbid`
     override (this unit, not that incident) and a redirect writes a `pin`.
     The planner reads both on every solve (`replan.load_overrides`).
  2. The crew's phone would keep showing the old job. So the field task is
     closed with the reason.
  3. Nobody would know why. So the event carries who, why, and what instead,
     and the Copilot's memory keeps the instruction in words.

Authority is not decided here. A cancel is proposed through the policy gate as
`cancel_assignment` (migration 021 puts it under the same clause as moving a
unit) and this module's `execute_cancel` only runs once the gate allows it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from app.agents import replan as replanner
from app.core.logging import get_logger
from app.db import session as db
from app.taxonomy import cache as taxonomy
from app.world import clock as clocks
from app.world import events as ev

log = get_logger(__name__)

INSTEAD = ("replan", "redirect", "stage", "hold", "return_to_base")

#: How long a cancelled unit is kept away from the incident it was taken off.
FORBID_MINUTES = 45
#: How long a redirected unit is pinned to its new job.
PIN_MINUTES = 60
#: Default hold.
HOLD_MINUTES = 30

ACTIVE = ("proposed", "approved", "en_route", "on_site")


class CannotCancel(Exception):
    """The request does not describe something that can be stopped."""


# -------------------------------------------------------------------- read ---
async def active(city_id: str = "pune") -> dict[str, Any]:
    """Every live operation, plus every standing instruction on the planner."""
    rows = await db.fetch(
        """
        select a.id::text assignment_id, a.status::text status, a.purpose,
               coalesce(a.progress, 0)::float progress, a.eta_minutes,
               a.distance_km, a.route_engine, a.created_at,
               r.id resource_id, r.label, r.kind, r.operator,
               i.id::text incident_id, i.title incident_title, i.severity,
               i.category, coalesce(i.ward_id, a.ward_id) ward_id, w.name ward_name,
               (select count(*) from events e
                 where e.kind = 'assignment.rerouted' and e.subject_id = r.id
                   and e.occurred_at >= a.created_at)::int reroutes,
               (select e.payload ->> 'reason' from events e
                 where e.kind in ('assignment.changed','assignment.created',
                                  'assignment.rerouted')
                   and (e.subject_id = r.id or e.payload ->> 'resource_id' = r.id
                        or e.subject_id = a.id::text)
                   and e.occurred_at >= a.created_at - interval '5 seconds'
                 order by e.id desc limit 1) why,
               (select e.actor from events e
                 where e.kind in ('assignment.changed','assignment.created')
                   and (e.subject_id = r.id or e.payload ->> 'resource_id' = r.id
                        or e.subject_id = a.id::text)
                   and e.occurred_at >= a.created_at - interval '5 seconds'
                 order by e.id desc limit 1) set_by
          from assignments a
          join resources r on r.id = a.resource_id
          left join incidents i on i.id = a.incident_id
          left join wards w on w.id = coalesce(i.ward_id, a.ward_id)
         where r.city_id = $1
           and a.sim_run_id is null
           and a.status = any($2::assignment_status[])
         order by coalesce(i.severity, 0) desc, a.created_at
        """,
        city_id, list(ACTIVE),
    )
    overrides = await list_overrides(city_id)
    by_unit: dict[str, list[dict]] = {}
    for o in overrides:
        by_unit.setdefault(o["resource_id"], []).append(o)

    ops = []
    for r in rows:
        progress = max(0.0, min(1.0, float(r["progress"] or 0)))
        eta = int(r["eta_minutes"] or 0)
        ops.append({
            "assignmentId": r["assignment_id"],
            "resourceId": r["resource_id"],
            "unit": r["label"],
            "kind": r["kind"],
            "operator": r["operator"],
            "status": r["status"],
            "incidentId": r["incident_id"],
            "doing": r["incident_title"] or r["purpose"] or "",
            "severity": r["severity"],
            "category": r["category"],
            "wardId": r["ward_id"],
            "ward": r["ward_name"],
            "progress": round(progress, 3),
            "minutesLeft": max(0, round(eta * (1 - progress))) if r["status"] != "on_site" else 0,
            "routeEngine": r["route_engine"],
            "reroutes": r["reroutes"],
            "why": r["why"] or "",
            "setBy": (r["set_by"] or "").replace("agent:", ""),
            "since": r["created_at"].isoformat() if r["created_at"] else None,
            "instructions": by_unit.get(r["resource_id"], []),
            "canCancel": True,
        })
    held = [o for o in overrides if o["kind"] == "hold"]
    return {"operations": ops, "overrides": overrides, "held": held,
            "count": len(ops)}


async def list_overrides(city_id: str = "pune") -> list[dict]:
    try:
        rows = await db.fetch(
            """
            select o.id::text, o.kind, o.resource_id, r.label unit,
                   o.incident_id::text, i.title incident, o.reason, o.created_by,
                   o.created_at, o.expires_at
              from operator_overrides o
              join resources r on r.id = o.resource_id
              left join incidents i on i.id = o.incident_id
             where o.city_id = $1 and o.active
               and (o.expires_at is null or o.expires_at > now())
             order by o.created_at desc
            """,
            city_id,
        )
    except Exception as exc:  # noqa: BLE001 - migration 021 not applied yet
        log.info("overrides_unavailable", error=type(exc).__name__)
        return []
    return [
        {
            "id": r["id"], "kind": r["kind"], "resource_id": r["resource_id"],
            "unit": r["unit"], "incident_id": r["incident_id"],
            "incident": r["incident"], "reason": r["reason"],
            "createdBy": r["created_by"],
            "createdAt": r["created_at"].isoformat(),
            "expiresAt": r["expires_at"].isoformat() if r["expires_at"] else None,
            "text": describe_override(r["kind"], r["unit"], r["incident"]),
        }
        for r in rows
    ]


def describe_override(kind: str, unit: str, incident: str | None) -> str:
    if kind == "pin":
        return f"{unit} is pinned to its job; the planner may not move it."
    if kind == "hold":
        return f"{unit} is held out of the plan."
    return f"{unit} will not be sent to {incident or 'that incident'} again."


def describe_instead(instead: dict, names: dict[str, str] | None = None) -> str:
    """One sentence for what happens after the cancel. Used by UI and Copilot."""
    kind = (instead or {}).get("kind") or "replan"
    names = names or {}
    if kind == "redirect":
        return f"Send it to {names.get('incident') or 'the chosen incident'} instead, pinned there."
    if kind == "stage":
        return f"Send it to {names.get('ward') or 'the chosen ward'} to wait there."
    if kind == "hold":
        return f"Keep it where it is, out of the plan for {int(instead.get('minutes') or HOLD_MINUTES)} minutes."
    if kind == "return_to_base":
        return "Send it back to base."
    return "Let the planner cover the job with another unit; this one is free for other work."


async def _unit_and_job(conn: Any, resource_id: str) -> tuple[Any, Any]:
    unit = await conn.fetchrow(
        """
        select r.id, r.label, r.kind, r.operator, r.city_id,
               extensions.ST_X(r.location::extensions.geometry) lng,
               extensions.ST_Y(r.location::extensions.geometry) lat,
               extensions.ST_X(r.base_location::extensions.geometry) base_lng,
               extensions.ST_Y(r.base_location::extensions.geometry) base_lat
          from resources r where r.id = $1
        """,
        resource_id,
    )
    job = await conn.fetchrow(
        """
        select a.id::text assignment_id, a.status::text status,
               a.incident_id::text incident_id, i.title, i.severity,
               coalesce(i.ward_id, a.ward_id) ward_id, a.purpose
          from assignments a
          left join incidents i on i.id = a.incident_id
         where a.resource_id = $1 and a.sim_run_id is null
           and a.status = any($2::assignment_status[])
         order by a.created_at desc limit 1
        """,
        resource_id, list(ACTIVE),
    )
    return unit, job


async def alternatives(resource_id: str, city_id: str = "pune", limit: int = 3) -> list[dict]:
    """What this unit could usefully do instead, best first.

    Open incidents with a need this unit's kind can meet and nobody meeting it,
    by severity then distance. This is how the Copilot says "what to do
    instead" without inventing it: the options are rows, not prose.
    """
    unit = await db.fetchrow(
        "select id, kind, label, location from resources where id = $1", resource_id
    )
    if unit is None:
        return []
    caps = [c for c in taxonomy.capabilities if taxonomy.kind_can(unit["kind"], c)]
    if not caps:
        return []
    rows = await db.fetch(
        """
        select i.id::text incident_id, i.title, i.severity, i.ward_id, w.name ward,
               n.capability_id, n.required - n.met short,
               round((extensions.ST_Distance(i.location, r.location) / 1000.0)::numeric, 1)::float km
          from incidents i
          join incident_needs n on n.incident_id = i.id
          join wards w on w.id = i.ward_id
          join resources r on r.id = $1
         where i.city_id = $2 and i.status <> 'resolved' and i.sim_run_id is null
           and n.required > n.met
           and n.capability_id = any($3::text[])
           and not exists (
             select 1 from assignments a
              where a.resource_id = r.id and a.incident_id = i.id
                and a.status = any($4::assignment_status[]))
         order by i.severity desc, km asc
         limit $5
        """,
        resource_id, city_id, caps, list(ACTIVE), limit,
    )
    return [
        {"incidentId": r["incident_id"], "title": r["title"], "severity": r["severity"],
         "ward": r["ward"], "wardId": r["ward_id"], "capability": r["capability_id"],
         "short": r["short"], "km": r["km"]}
        for r in rows
    ]


# ------------------------------------------------------------------ preview ---
async def preview(resource_id: str, instead: dict | None = None,
                  city_id: str = "pune") -> dict:
    """What cancelling this unit's job would do, before anybody approves it.

    Re-solves the real model with the unit withdrawn (or pinned elsewhere, for a
    redirect), so the officer sees who covers the job, what gets slower, and
    what stays uncovered. Nothing is written.
    """
    from app.copilot import tools  # local: tools imports a lot

    instead = instead or {"kind": "replan"}
    async with db.acquire() as conn:
        unit, job = await _unit_and_job(conn, resource_id)
    if unit is None:
        raise CannotCancel("No such unit.")
    if job is None:
        raise CannotCancel(f"{unit['label']} is not on a job, so there is nothing to cancel.")

    kind = instead.get("kind") or "replan"
    if kind == "redirect" and instead.get("incident_id"):
        table = await tools.call(
            "simulate_reallocation", resource_id=resource_id,
            incident_id=instead["incident_id"], city_id=city_id,
        )
    else:
        table = await tools.call(
            "simulate_withdrawal", resource_ids=[resource_id], city_id=city_id
        )
    return {
        "unit": unit["label"],
        "resourceId": resource_id,
        "current": {
            "assignmentId": job["assignment_id"], "status": job["status"],
            "incidentId": job["incident_id"], "doing": job["title"] or job["purpose"],
            "severity": job["severity"],
        },
        "instead": instead,
        "insteadText": describe_instead(instead, {
            "incident": instead.get("incident_title"), "ward": instead.get("ward_name"),
        }),
        "comparison": table,
        "alternatives": await alternatives(resource_id, city_id),
    }


# ------------------------------------------------------------------ execute ---
async def _add_override(conn: Any, *, city_id: str, kind: str, resource_id: str,
                        incident_id: str | None, reason: str, actor: str,
                        minutes: int | None, decision_id: str | None = None) -> str | None:
    """Write an override. Returns None when migration 021 is not applied."""
    expires = datetime.now(UTC) + timedelta(minutes=minutes) if minutes else None
    try:
        async with conn.transaction():  # savepoint: a missing table must not abort the cancel
            return await conn.fetchval(
                """
                insert into operator_overrides
                  (city_id, kind, resource_id, incident_id, reason, created_by,
                   decision_id, expires_at)
                values ($1,$2,$3,$4::uuid,$5,$6,$7::uuid,$8)
                returning id::text
                """,
                city_id, kind, resource_id, incident_id, reason[:600], actor,
                decision_id, expires,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("override_not_written", kind=kind, error=type(exc).__name__)
        return None


async def execute_cancel(conn: Any, params: dict, *, actor: str,
                         caused_by: int | None) -> dict:
    """Carry out an authorised `cancel_assignment`. Runs inside a transaction."""
    from app.copilot import execute  # executors for redirect / stage

    resource_id = params.get("resource_id")
    reason = (params.get("reason") or "Cancelled by an officer.").strip()
    instead = params.get("instead") or {"kind": "replan"}
    kind = instead.get("kind") or "replan"
    if not resource_id:
        raise execute.NotExecutable("A cancel needs a unit.")
    if kind not in INSTEAD:
        raise execute.NotExecutable(f"Unknown instruction {kind!r}.")

    unit, job = await _unit_and_job(conn, resource_id)
    if unit is None:
        raise execute.NotExecutable("That unit is not in this city.")
    if job is None:
        raise execute.NotExecutable(f"{unit['label']} is not on a job any more.")
    city_id = unit["city_id"] or "pune"

    # 1. Stop the job, and take it off the crew's phone.
    await conn.execute(
        "update assignments set status = 'cancelled' where id = $1::uuid",
        job["assignment_id"],
    )
    await replanner.close_tasks(conn, job["assignment_id"], f"Cancelled: {reason}")
    await conn.execute(
        "update resources set status = 'available', updated_at = now() where id = $1",
        resource_id,
    )

    # 2. Make it stick: the planner must not send it straight back.
    overrides: list[str] = []
    if job["incident_id"]:
        oid = await _add_override(
            conn, city_id=city_id, kind="forbid", resource_id=resource_id,
            incident_id=job["incident_id"], reason=reason, actor=actor,
            minutes=FORBID_MINUTES, decision_id=params.get("decision_id"),
        )
        if oid:
            overrides.append(oid)

    cancelled = await ev.append(
        clock=clocks.WALL, kind=ev.Kind.ASSIGNMENT_CANCELLED, actor=actor,
        subject_type="resource", subject_id=resource_id, city_id=city_id,
        ward_id=job["ward_id"],
        payload={
            "from_incident": job["incident_id"], "incident_title": job["title"],
            "assignment_id": job["assignment_id"], "reason": reason,
            "instead": instead, "by": actor, "resource_label": unit["label"],
        },
        caused_by=caused_by, conn=conn,
    )

    # 3. Do what was asked instead.
    outcome: dict[str, Any] = {}
    if kind == "redirect":
        target = instead.get("incident_id")
        if not target:
            raise execute.NotExecutable("A redirect needs an incident.")
        # reallocate_unit writes the route, the crew's task and the pin.
        outcome = await execute.run(
            "reallocate_unit",
            {"resource_id": resource_id, "incident_id": target,
             "reason": f"Redirected by an officer: {reason}"},
            actor=actor, caused_by=cancelled.id, conn=conn,
        )
    elif kind == "stage":
        ward_id = instead.get("ward_id")
        if not ward_id:
            raise execute.NotExecutable("Staging needs a ward.")
        outcome = await execute.run(
            "preposition_equipment", {"resource_id": resource_id, "ward_id": ward_id},
            actor=actor, caused_by=cancelled.id, conn=conn,
        )
        minutes = int(instead.get("minutes") or 0)
        if minutes:
            oid = await _add_override(
                conn, city_id=city_id, kind="hold", resource_id=resource_id,
                incident_id=None, reason=f"Staged and held: {reason}", actor=actor,
                minutes=minutes,
            )
            if oid:
                overrides.append(oid)
    elif kind == "hold":
        minutes = int(instead.get("minutes") or HOLD_MINUTES)
        oid = await _add_override(
            conn, city_id=city_id, kind="hold", resource_id=resource_id,
            incident_id=None, reason=reason, actor=actor, minutes=minutes,
        )
        if oid:
            overrides.append(oid)
        outcome = {"held": True, "minutes": minutes}
    elif kind == "return_to_base":
        outcome = await _return_to_base(conn, unit, actor=actor, caused_by=cancelled.id)
        oid = await _add_override(
            conn, city_id=city_id, kind="hold", resource_id=resource_id,
            incident_id=None, reason=f"Returning to base: {reason}", actor=actor,
            minutes=int(instead.get("minutes") or 20),
        )
        if oid:
            overrides.append(oid)

    # 4. Whatever was uncovered by the cancel is the planner's to fill, now.
    _replan_soon(f"{unit['label']} cancelled by an officer")

    return {
        "resource": unit["label"],
        "cancelled": job["title"] or job["purpose"],
        "instead": kind,
        "insteadText": describe_instead(instead, {
            "incident": outcome.get("incident"), "ward": outcome.get("ward"),
        }),
        "overrides": overrides,
        "overridesStored": bool(overrides) or not job["incident_id"],
        "outcome": outcome,
        "eventId": cancelled.id,
    }


async def _return_to_base(conn: Any, unit: Any, *, actor: str, caused_by: int | None) -> dict:
    from app.copilot import execute
    from app.solver import routing

    if unit["base_lng"] is None:
        return {"note": "No base recorded for this unit; it is holding where it is."}
    line = await routing.route_line(
        (float(unit["lng"]), float(unit["lat"])),
        (float(unit["base_lng"]), float(unit["base_lat"])),
    )
    row = await conn.fetchrow(
        """
        insert into assignments
          (resource_id, incident_id, ward_id, purpose, eta_minutes, distance_km,
           status, route, route_engine, steps, progress)
        values ($1, null, null, 'Returning to base', $2, $3, 'en_route',
                case when $4::text is null then null
                     else extensions.ST_SetSRID(
                            extensions.ST_GeomFromGeoJSON($4::text), 4326) end,
                $5, $6, 0)
        returning id::text
        """,
        unit["id"], int(line.minutes), round(float(line.km), 2),
        execute._geometry(line), line.engine, execute._steps(line),
    )
    await conn.execute(
        "update resources set status = 'en_route', updated_at = now() where id = $1",
        unit["id"],
    )
    await ev.append(
        clock=clocks.WALL, kind=ev.Kind.ASSIGNMENT_CREATED, actor=actor,
        subject_type="assignment", subject_id=row["id"],
        payload={"resource_id": unit["id"], "resource_label": unit["label"],
                 "purpose": "return_to_base", "eta_minutes": int(line.minutes),
                 "reason": "Sent back to base by an officer."},
        caused_by=caused_by, conn=conn,
    )
    return {"assignmentId": row["id"], "etaMinutes": int(line.minutes)}


def _replan_soon(trigger: str) -> None:
    """Re-plan after the cancel's transaction has committed.

    Through the demo runner's lock when it is running, so a tick and this
    re-plan never write the same rows at once (the TimeoutError of 11 Sep).
    """
    # The event router serialises and coalesces re-plans; use it when it runs.
    from app.agents import event_router

    if event_router.running():
        event_router.request_replan(trigger)
        return

    async def _go() -> None:
        await asyncio.sleep(0.5)
        try:
            from app.demo import runner

            if runner.state.running:
                runner.state.dirty = True
                runner.state.last_replan_tick = -10_000
            else:
                await replanner.replan(trigger=trigger, actor="agent:allocation_planner")
        except Exception as exc:  # noqa: BLE001
            log.warning("replan_after_cancel_failed", error=str(exc)[:200])

    try:
        asyncio.get_running_loop().create_task(_go())
    except RuntimeError:
        pass


async def lift(override_id: str, *, actor: str) -> dict:
    """Withdraw an instruction early. The row stays, marked lifted."""
    row = await db.fetchrow(
        """
        update operator_overrides
           set active = false, lifted_by = $2, lifted_at = now()
         where id = $1::uuid and active
        returning kind, resource_id
        """,
        override_id, actor,
    )
    if row is None:
        raise CannotCancel("That instruction is not active.")
    await ev.append(
        clock=clocks.WALL, kind="override.lifted", actor=actor,
        subject_type="resource", subject_id=row["resource_id"],
        payload={"override_id": override_id, "kind": row["kind"]},
    )
    _replan_soon("instruction lifted")
    return {"lifted": override_id, "kind": row["kind"]}


# ------------------------------------------------------------------ propose ---
async def propose_cancel(
    *, resource_id: str, reason: str, instead: dict | None, actor: str,
    city_id: str = "pune", severity: int = 3,
) -> dict:
    """Put a cancel to the policy gate, and carry it out if the gate allows.

    The same two steps as every other act in this system: the gate decides
    authority, the executor does what was authorised. A cancel the gate holds
    back lands on the approvals screen with its params, so approving it later
    cancels the job it named.
    """
    from app.copilot import agent

    instead = instead or {"kind": "replan"}
    unit = await db.fetchrow("select label from resources where id = $1", resource_id)
    if unit is None:
        raise CannotCancel("No such unit.")
    result = await agent.apply_actions(
        [{
            "actionKey": "cancel_assignment",
            "action": f"Cancel {unit['label']}'s job. {describe_instead(instead)}",
            "target": unit["label"],
            "rationale": reason,
            "severity": severity,
            "confidence": 0.9,
            "params": {"resource_id": resource_id, "reason": reason, "instead": instead},
        }],
        actor=actor, city_id=city_id,
    )
    await _remember(resource_id, unit["label"], reason, instead, actor, city_id)
    return result


async def _remember(resource_id: str, label: str, reason: str, instead: dict,
                    actor: str, city_id: str) -> None:
    """Keep the instruction in words, so the Copilot can say why later."""
    try:
        from app.copilot import memory

        await memory.remember(
            scope="standing_order",
            content=f"{actor} cancelled {label}'s job: {reason}. {describe_instead(instead)}",
            data={"resource_id": resource_id, "instead": instead},
            subject_type="resource", subject_id=resource_id,
            created_by=actor, city_id=city_id, importance=4,
            expires_in_minutes=FORBID_MINUTES * 4,
        )
    except Exception as exc:  # noqa: BLE001 - memory is best-effort
        log.info("cancel_not_remembered", error=type(exc).__name__)
