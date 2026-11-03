"""Turning an approved decision into something that actually happened.

The gap this closes: the policy gate has always been able to *record* that an
action was authorised, and nothing downstream ever carried it out. A decision
that says "move Ambulance 4 to Kothrud" and leaves the ambulance where it is is
theatre, and an officer discovers that the first time they approve something.

So every action key the Copilot can propose has an executor here, and the flow
is the same for all of them:

    propose -> gate -> authorised?  -- no --> waits on the decision gate
                          |
                         yes
                          v
                       execute -> event

An action with no executor is still proposable and still gated; it simply says
so, rather than pretending. `_EXECUTORS` is the whole list, and if a key is not
in it the decision is recorded as authorised-but-manual and the console says
"this one is yours to carry out".

Nothing here decides anything. Authority was settled before it was called.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from app.core.logging import get_logger
from app.db import session as db
from app.solver import routing
from app.world import clock as clocks
from app.world import events as ev

log = get_logger(__name__)


class NotExecutable(Exception):
    """Authorised, but this system cannot be the thing that does it."""


def _geometry(line: Any) -> str | None:
    """The route as GeoJSON, or nothing when the router gave us two points.

    Same shape the planner stores, so a hand-made move draws on the map exactly
    like a solved one.
    """
    coords = getattr(line, "coordinates", None) or []
    if len(coords) < 2:
        return None
    return json.dumps({"type": "LineString", "coordinates": coords})


def _steps(line: Any) -> list[dict]:
    return [
        {"instruction": s.instruction, "street": s.street, "distanceM": s.distance_m}
        for s in getattr(line, "steps", []) or []
    ]


# ------------------------------------------------------------------ actions ---
async def _reallocate_unit(conn: Any, params: dict, *, actor: str, caused_by: int | None) -> dict:
    """Commit one unit to one incident, with a real route.

    Writes the same assignment shape the planner writes, including the road
    geometry, so a hand-made move is indistinguishable downstream from a solved
    one: it animates on the map, appears on the crew's phone, and is counted in
    coverage. A parallel "manual override" path that skipped any of that would
    be a second source of truth about where units are.
    """
    resource_id = params.get("resource_id")
    incident_id = params.get("incident_id")
    if not resource_id or not incident_id:
        raise NotExecutable("A reallocation needs a unit and an incident.")

    unit = await conn.fetchrow(
        """
        select r.id, r.label, r.operator, r.kind,
               extensions.ST_X(r.location::extensions.geometry) lng,
               extensions.ST_Y(r.location::extensions.geometry) lat
          from resources r where r.id = $1
        """,
        resource_id,
    )
    incident = await conn.fetchrow(
        """
        select i.id::text, i.title, i.ward_id, i.severity,
               extensions.ST_X(i.location::extensions.geometry) lng,
               extensions.ST_Y(i.location::extensions.geometry) lat
          from incidents i where i.id = $1::uuid and i.status <> 'resolved'
        """,
        incident_id,
    )
    if unit is None or incident is None:
        raise NotExecutable("That unit or incident is no longer in the world.")

    # Round the same blocks the planner avoids. This passed an empty list, so a
    # commissioner's move was the one route in the system that drove straight
    # through a reported closure.
    from app.agents.replan import _blocked_points, close_tasks

    blocked = await _blocked_points(conn, "pune", None)
    line = await routing.route_line(
        (float(unit["lng"]), float(unit["lat"])),
        (float(incident["lng"]), float(incident["lat"])),
        blocked,
    )

    # Whatever it was doing, it is not doing it any more — including on the
    # crew's phone, which used to keep showing the old job.
    old = await conn.fetch(
        """
        update assignments set status = 'cancelled'
         where resource_id = $1 and sim_run_id is null
           and status in ('proposed','approved','en_route','on_site')
        returning id::text
        """,
        resource_id,
    )
    for o in old:
        await close_tasks(conn, o["id"], f"Re-tasked to {incident['title']} by an officer.")
    row = await conn.fetchrow(
        """
        insert into assignments
          (resource_id, incident_id, ward_id, purpose, eta_minutes, distance_km,
           status, route, route_engine, steps, progress)
        values ($1,$2::uuid,$3,$4,$5,$6,'en_route',
                case when $7::text is null then null
                     else extensions.ST_SetSRID(
                            extensions.ST_GeomFromGeoJSON($7::text), 4326) end,
                $8,$9,0)
        returning id::text
        """,
        resource_id, incident_id, incident["ward_id"],
        f"Commissioner reallocation — {incident['title']}",
        int(line.minutes), round(float(line.km), 2),
        _geometry(line), line.engine, _steps(line),
    )
    await conn.execute(
        """
        insert into field_tasks
          (assignment_id, resource_id, operator, title, instruction, location,
           ward_id, priority)
        values ($1::uuid, $2, $3, $4, $5,
                extensions.ST_SetSRID(extensions.ST_MakePoint($6,$7),4326)::extensions.geography,
                $8, $9)
        """,
        row["id"], resource_id, unit["operator"], incident["title"],
        f"{incident['title']}. Directed by the control room, {int(line.minutes)} "
        "minutes out. Confirm on arrival and record what you found.",
        float(incident["lng"]), float(incident["lat"]), incident["ward_id"],
        int(incident["severity"] or 3),
    )
    await conn.execute(
        "update resources set status = 'en_route', updated_at = now() where id = $1",
        resource_id,
    )
    # Pin it, or the next re-plan prices this unit as free and undoes the order.
    from app.ops.operations import PIN_MINUTES, _add_override

    await _add_override(
        conn, city_id="pune", kind="pin", resource_id=resource_id,
        incident_id=incident_id, reason=params.get("reason") or "Directed by an officer.",
        actor=actor, minutes=PIN_MINUTES,
    )
    await ev.append(
        clock=clocks.WALL, kind=ev.Kind.ASSIGNMENT_CHANGED, actor=actor,
        subject_type="assignment", subject_id=row["id"],
        ward_id=incident["ward_id"],
        payload={"resource_id": resource_id, "resource_label": unit["label"],
                 "to_incident": incident_id, "incident_title": incident["title"],
                 "eta_minutes": int(line.minutes), "reason": "commissioner directed"},
        caused_by=caused_by, conn=conn,
    )
    return {
        "assignmentId": row["id"], "resource": unit["label"],
        "incident": incident["title"], "etaMinutes": int(line.minutes),
        "engine": line.engine, "avoided": line.avoided,
    }


async def _preposition(conn: Any, params: dict, *, actor: str, caused_by: int | None) -> dict:
    """Stage a unit in a ward, against demand that has not happened yet.

    An assignment with no incident, which the schema already allows, because
    that is precisely what prepositioning is: committed, with somewhere to be,
    and nothing yet to do there.
    """
    resource_id = params.get("resource_id")
    ward_id = params.get("ward_id")
    if not resource_id or not ward_id:
        raise NotExecutable("Prepositioning needs a unit and a ward.")

    unit = await conn.fetchrow(
        """
        select r.id, r.label, r.operator,
               extensions.ST_X(r.location::extensions.geometry) lng,
               extensions.ST_Y(r.location::extensions.geometry) lat
          from resources r where r.id = $1
        """,
        resource_id,
    )
    ward = await conn.fetchrow(
        """
        select id, name,
               extensions.ST_X(centroid::extensions.geometry) lng,
               extensions.ST_Y(centroid::extensions.geometry) lat
          from wards where id = $1
        """,
        ward_id,
    )
    if unit is None or ward is None:
        raise NotExecutable("That unit or ward is not in this city.")

    line = await routing.route_line(
        (float(unit["lng"]), float(unit["lat"])),
        (float(ward["lng"]), float(ward["lat"])),
        [],
    )
    await conn.execute(
        """
        update assignments set status = 'cancelled'
         where resource_id = $1 and sim_run_id is null
           and status in ('proposed','approved','en_route')
        """,
        resource_id,
    )
    row = await conn.fetchrow(
        """
        insert into assignments
          (resource_id, incident_id, ward_id, purpose, eta_minutes, distance_km,
           status, route, route_engine, steps, progress)
        values ($1, null, $2, $3, $4, $5, 'en_route',
                case when $6::text is null then null
                     else extensions.ST_SetSRID(
                            extensions.ST_GeomFromGeoJSON($6::text), 4326) end,
                $7,$8,0)
        returning id::text
        """,
        resource_id, ward_id, f"Prepositioned in {ward['name']}",
        int(line.minutes), round(float(line.km), 2),
        _geometry(line), line.engine, _steps(line),
    )
    await conn.execute(
        """
        insert into field_tasks
          (assignment_id, resource_id, operator, title, instruction, location,
           ward_id, priority)
        values ($1::uuid, $2, $3, $4, $5,
                extensions.ST_SetSRID(extensions.ST_MakePoint($6,$7),4326)::extensions.geography,
                $8, 3)
        """,
        row["id"], resource_id, unit["operator"],
        f"Stage in {ward['name']}",
        "Move to the ward staging point and hold. You have not been given an "
        "incident; you are being put where one is expected.",
        float(ward["lng"]), float(ward["lat"]), ward_id,
    )
    await conn.execute(
        "update resources set status = 'en_route', updated_at = now() where id = $1",
        resource_id,
    )
    await ev.append(
        clock=clocks.WALL, kind=ev.Kind.ASSIGNMENT_CREATED, actor=actor,
        subject_type="assignment", subject_id=row["id"], ward_id=ward_id,
        payload={"resource_id": resource_id, "resource_label": unit["label"],
                 "purpose": "preposition", "ward": ward["name"],
                 "eta_minutes": int(line.minutes)},
        caused_by=caused_by, conn=conn,
    )
    return {"assignmentId": row["id"], "resource": unit["label"],
            "ward": ward["name"], "etaMinutes": int(line.minutes)}


async def _mutual_aid(conn: Any, params: dict, *, actor: str, caused_by: int | None) -> dict:
    """Ask another agency for capability we do not have."""
    agency_id = params.get("agency_id")
    capability = params.get("capability")
    quantity = int(params.get("quantity") or 1)
    if not agency_id or not capability:
        raise NotExecutable("A mutual-aid request needs an agency and a capability.")

    row = await conn.fetchrow(
        """
        insert into agency_requests
          (city_id, incident_id, ward_id, from_agency, to_agency, capability_id,
           quantity, status, note, requested_at)
        values ('pune', $1::uuid, $2, 'pmc', $3, $4, $5, 'requested', $6, now())
        returning id::text
        """,
        params.get("incident_id"), params.get("ward_id") or _ANY_WARD,
        agency_id, capability, quantity,
        params.get("note") or "Raised from the command console against a projected shortfall.",
    )
    await ev.append(
        clock=clocks.WALL, kind=ev.Kind.DEMAND_UNCOVERED, actor=actor,
        subject_type="agency_request", subject_id=row["id"],
        payload={"to_agency": agency_id, "capability": capability,
                 "quantity": quantity},
        caused_by=caused_by, conn=conn,
    )
    from app.ops import agency_replies

    await agency_replies.schedule(row["id"], conn=conn)
    return {"requestId": row["id"], "agency": agency_id,
            "capability": capability, "quantity": quantity}


async def _activate_shelter(conn: Any, params: dict, *, actor: str, caused_by: int | None) -> dict:
    """Bring a lifeline back into service, or note the alternates."""
    lifeline_id = params.get("lifeline_id")
    if not lifeline_id:
        raise NotExecutable("Activating a shelter needs a lifeline.")
    row = await conn.fetchrow(
        "update lifelines set status = 'open', last_reported_at = now() "
        "where id = $1 returning id, name, ward_id",
        lifeline_id,
    )
    if row is None:
        raise NotExecutable("No such lifeline.")
    await ev.append(
        clock=clocks.WALL, kind=ev.Kind.RESOURCE_STATUS_CHANGED, actor=actor,
        subject_type="lifeline", subject_id=row["id"], ward_id=row["ward_id"],
        payload={"status": "open", "name": row["name"],
                 "alternates": params.get("alternates") or []},
        caused_by=caused_by, conn=conn,
    )
    return {"lifeline": row["name"], "status": "open"}


#: A ward id is required on `agency_requests`; when the shortfall is city-wide
#: rather than local there is no honest ward to name, so the request is filed
#: against the first one and the note says what it really is.
_ANY_WARD = "w-1"

async def _cancel_assignment(conn: Any, params: dict, *, actor: str, caused_by: int | None) -> dict:
    """Stop a unit's job and do what the officer said instead. See app.ops."""
    from app.ops.operations import execute_cancel

    return await execute_cancel(conn, params, actor=actor, caused_by=caused_by)


async def _standing_order(kind: str, conn: Any, params: dict, *, actor: str,
                          caused_by: int | None) -> dict:
    """Hold a free unit out of the plan, or pin a committed one to its job."""
    from app.ops.operations import HOLD_MINUTES, PIN_MINUTES, _add_override

    resource_id = params.get("resource_id")
    if not resource_id:
        raise NotExecutable("A standing order needs a unit.")
    instead = params.get("instead") or {}
    minutes = int(params.get("minutes") or instead.get("minutes")
                  or (HOLD_MINUTES if kind == "hold" else PIN_MINUTES))
    oid = await _add_override(
        conn, city_id="pune", kind=kind, resource_id=resource_id,
        incident_id=params.get("incident_id"),
        reason=params.get("reason") or f"{kind} set by an officer", actor=actor,
        minutes=minutes,
    )
    if oid is None:
        raise NotExecutable("Standing orders need migration 021.")
    await ev.append(
        clock=clocks.WALL, kind=f"override.{kind}", actor=actor,
        subject_type="resource", subject_id=resource_id,
        payload={"override_id": oid, "minutes": minutes,
                 "reason": params.get("reason") or ""},
        caused_by=caused_by, conn=conn,
    )
    return {"override": oid, "kind": kind, "minutes": minutes}


async def _hold_unit(conn: Any, params: dict, *, actor: str, caused_by: int | None) -> dict:
    return await _standing_order("hold", conn, params, actor=actor, caused_by=caused_by)


async def _pin_unit(conn: Any, params: dict, *, actor: str, caused_by: int | None) -> dict:
    return await _standing_order("pin", conn, params, actor=actor, caused_by=caused_by)


_EXECUTORS: dict[str, Callable[..., Awaitable[dict]]] = {
    "hold_unit": _hold_unit,
    "pin_unit": _pin_unit,
    "reallocate_unit": _reallocate_unit,
    "cancel_assignment": _cancel_assignment,
    "preposition_equipment": _preposition,
    "request_mutual_aid": _mutual_aid,
    "activate_shelter": _activate_shelter,
}


def executable(action_key: str) -> bool:
    return action_key in _EXECUTORS


def executable_actions() -> list[str]:
    """The action keys this system can carry out itself.

    Everything else is proposable and gateable and then has to be done by a
    person — which the console says out loud rather than leaving somebody to
    discover after they approve it.
    """
    return sorted(_EXECUTORS)


# -------------------------------------------------------------------- entry ---
async def run(
    action_key: str,
    params: dict,
    *,
    actor: str = "agent:command",
    caused_by: int | None = None,
    conn: Any | None = None,
) -> dict:
    """Carry out one authorised action. Raises `NotExecutable` if it cannot."""
    fn = _EXECUTORS.get(action_key)
    if fn is None:
        raise NotExecutable(
            f"{action_key.replace('_', ' ')} is authorised but has to be done by "
            "a person; this system has no way to carry it out."
        )
    if conn is not None:
        return await fn(conn, params, actor=actor, caused_by=caused_by)
    async with db.transaction() as c:
        return await fn(c, params, actor=actor, caused_by=caused_by)


async def apply_decision(decision_id: str, *, actor: str = "officer") -> dict:
    """Execute a decision that a person has just approved.

    Called from the decision gate. The parameters were stored when the action
    was proposed, so approving a three-hour-old decision still moves the unit it
    named rather than re-deriving one from a world that has changed.
    """
    row = await db.fetchrow(
        "select action_key, action, params, status::text status "
        "from decisions where id = $1::uuid",
        decision_id,
    )
    if row is None:
        raise NotExecutable("No such decision.")
    params = row["params"] or {}
    if not params:
        raise NotExecutable(
            "This decision carries no parameters, so there is nothing mechanical "
            "to carry out."
        )
    result = await run(row["action_key"], dict(params), actor=f"officer:{actor}")
    return {"decision": row["action"], "result": result}
