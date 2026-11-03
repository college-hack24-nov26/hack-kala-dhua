"""Replay: the world as it was, at any point in the run.

`events` is append-only, so everything that happened is already recorded. What
did not exist was a way to *stand* at a moment and look around, and that is the
difference between a claim and a demonstration: the allocator's advantage over
nearest-first is currently a number in a table, and a judge who can watch the
same incident stream play out twice has seen it instead of been told it.

Three things are worth knowing before reading the fold below, because each one
shaped it and none is obvious from the table.

**The events are deltas, not snapshots.** `assignment.changed` says a unit moved
from one incident to another; it does not say what else was going on. So state
at time *t* is the result of applying every event from the start of the session
up to *t*, in order. That fold is the substance of this module. It is done here
rather than in the browser because it needs to know what each event kind *means*,
which is domain knowledge and belongs with the domain.

**The events carry no geometry.** `incident.opened` records the category, the
severity and the needs, and no coordinates. So the fold decides *which* things
were open and *who* was assigned to what, and the map positions come from the
entity tables, which is correct anyway: an incident does not move.

**Unit tracks were never recorded.** There is no history of where a vehicle was
at 14:32. What exists is `progress_pct` on `assignment.changed`, so a unit's
position during replay is interpolated along the line from its base to the
incident it was travelling to. That is a reconstruction and the API says so, in
`positions_are: "reconstructed"`. Drawing it as though it were a GPS trace would
be the one dishonest thing this screen could do.

There is also no `world.reset` event, despite what the design notes claim — the
kind has never been written. Session boundaries are therefore derived from gaps
in the stream, which is a weaker signal but an honest one, and it has the
advantage of working on data that already exists.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query

from app.core.errors import BadRequest
from app.core.security import StaffPrincipal
from app.db import session as db

router = APIRouter(tags=["replay"])

#: A quiet stretch longer than this ends a session. Chosen because the demo
#: generator ticks about once a second and a real shift has gaps of minutes, so
#: anything under ten minutes would split one run into several.
SESSION_GAP = timedelta(minutes=15)

#: Frames the scrubber asks for by default. 120 over a two-hour session is one
#: frame a minute, which is smooth enough to follow and small enough to send.
DEFAULT_STEPS = 120
MAX_STEPS = 600

#: Only these change the world. `report.received` and the decision kinds are
#: returned in the ticker because they explain *why* something happened, but
#: they move nothing on the map and so are not folded.
_FOLDED = (
    "incident.opened",
    "incident.resolved",
    "assignment.created",
    "assignment.changed",
    "assignment.cancelled",
    "alert.issued",
    "plan.generated",
    "demand.uncovered",
)


@router.get("/replay/sessions")
async def sessions(_: StaffPrincipal, city_id: str = Query(default="pune")) -> list[dict]:
    """Contiguous stretches of activity, newest first.

    Derived from gaps rather than from a reset marker, because no reset marker
    is ever written. Each row is something a person would call "a run".
    """
    rows = await db.fetch(
        """
        with marked as (
          select id, occurred_at,
                 case when occurred_at - lag(occurred_at) over (order by occurred_at)
                           > $2::interval
                      then 1 else 0 end as breaks
            from events
           where city_id = $1 and sim_run_id is null
        ),
        grouped as (
          select id, occurred_at, sum(breaks) over (order by occurred_at) as session
            from marked
        )
        select session,
               min(occurred_at) started_at,
               max(occurred_at) ended_at,
               count(*)::int events
          from grouped
         group by session
        having count(*) >= 5
         order by started_at desc
         limit 20
        """,
        city_id, SESSION_GAP,
    )
    return [
        {
            "id": int(r["session"]),
            "startedAt": r["started_at"].isoformat(),
            "endedAt": r["ended_at"].isoformat(),
            "events": r["events"],
            "durationMinutes": round(
                (r["ended_at"] - r["started_at"]).total_seconds() / 60, 1
            ),
        }
        for r in rows
    ]


def _describe(kind: str, payload: dict, subject: str | None) -> str:
    """One line a person can read, for the ticker beside the map."""
    p = payload or {}
    if kind == "incident.opened":
        return f"Incident opened — {(p.get('category') or '').replace('_', ' ')}, severity {p.get('severity')}"
    if kind == "incident.resolved":
        return "Incident resolved"
    if kind == "assignment.created":
        return f"{p.get('resource_id')} sent, {p.get('eta_minutes')} min out ({p.get('capability')})"
    if kind == "assignment.changed":
        # The reason is already a written sentence and it is the most valuable
        # text in the whole table: it says why the solver moved a unit.
        return p.get("reason") or f"{subject} reassigned"
    if kind == "assignment.cancelled":
        return f"{subject} stood down"
    if kind == "alert.issued":
        return f"Alert issued to {p.get('audience')} — severity {p.get('severity')}, {p.get('reach'):,} reached" \
            if p.get("reach") else f"Alert issued to {p.get('audience')}"
    if kind == "plan.generated":
        return (
            f"Re-solved ({p.get('trigger')}): {p.get('units')} units over "
            f"{p.get('demands')} demands, {round((p.get('coverage') or 0) * 100)}% covered "
            f"[{p.get('engine')}]"
        )
    if kind == "demand.uncovered":
        return f"Uncovered: {p.get('capability')} — {p.get('reason')}"
    if kind == "report.received":
        return "Report received"
    if kind == "decision.gated":
        return f"Decision gated: {p.get('outcome') or p.get('clause') or ''}".strip(": ")
    return kind.replace(".", " ")


@router.get("/replay/frames")
async def frames(
    _: StaffPrincipal,
    start: datetime = Query(..., alias="from"),
    end: datetime = Query(..., alias="to"),
    steps: int = Query(default=DEFAULT_STEPS, ge=2, le=MAX_STEPS),
    city_id: str = Query(default="pune"),
) -> dict[str, Any]:
    """The world at `steps` evenly spaced moments between `from` and `to`.

    Returns a catalogue of every incident and unit that appears anywhere in the
    window, and then frames that reference them by id. Sending the geometry once
    rather than in all 120 frames is the difference between a 200 KB response
    and a 6 MB one, and the client needs the catalogue anyway to draw a unit
    that has not moved.
    """
    if end <= start:
        raise BadRequest("`to` must be after `from`.")

    rows = await db.fetch(
        """
        select id, occurred_at, kind, actor, subject_type, subject_id,
               ward_id, payload, causation_id
          from events
         where city_id = $1 and sim_run_id is null
           and occurred_at >= $2 and occurred_at <= $3
         order by occurred_at, id
        """,
        city_id, start, end,
    )
    if not rows:
        return {"from": start.isoformat(), "to": end.isoformat(),
                "frames": [], "events": [], "incidents": {}, "resources": {},
                "positionsAre": "reconstructed"}

    # ---------------------------------------------------------- catalogue ---
    # Every incident and unit named anywhere in the window, with the geometry
    # the events do not carry.
    incident_ids = {
        r["subject_id"] for r in rows
        if r["subject_type"] == "incident" and r["subject_id"]
    }
    for r in rows:
        p = r["payload"] or {}
        for key in ("to_incident", "from_incident"):
            if p.get(key):
                incident_ids.add(p[key])
        if r["kind"] == "assignment.created" and r["subject_id"]:
            incident_ids.add(r["subject_id"])

    incidents: dict[str, dict] = {}
    if incident_ids:
        irows = await db.fetch(
            """
            select id::text, title, category, severity, ward_id, status::text status,
                   extensions.ST_X(location::extensions.geometry) lng,
                   extensions.ST_Y(location::extensions.geometry) lat
              from incidents where id = any($1::uuid[])
            """,
            list(incident_ids),
        )
        incidents = {
            r["id"]: {"title": r["title"], "category": r["category"],
                      "severity": r["severity"], "wardId": r["ward_id"],
                      "location": [float(r["lng"]), float(r["lat"])]}
            for r in irows
        }

    rrows = await db.fetch(
        """
        select id, label, kind, operator,
               extensions.ST_X(base_location::extensions.geometry) base_lng,
               extensions.ST_Y(base_location::extensions.geometry) base_lat,
               extensions.ST_X(location::extensions.geometry) lng,
               extensions.ST_Y(location::extensions.geometry) lat
          from resources where city_id = $1
        """,
        city_id,
    )
    resources = {
        r["id"]: {
            "label": r["label"], "kind": r["kind"], "operator": r["operator"],
            # Home is where a unit sits when it is not committed, and the anchor
            # the reconstruction interpolates from. Falls back to its current
            # position for a fleet that has no home recorded.
            "base": [
                float(r["base_lng"] if r["base_lng"] is not None else r["lng"]),
                float(r["base_lat"] if r["base_lat"] is not None else r["lat"]),
            ],
        }
        for r in rrows
    }

    # -------------------------------------------------------------- fold ----
    # Mutable state, advanced event by event. The frame boundaries are just
    # points at which a copy is taken.
    open_incidents: set[str] = set()
    # resource id -> {"incident": id, "progress": 0-100}
    assigned: dict[str, dict] = {}
    alerts: list[dict] = []
    plan: dict | None = None
    uncovered: dict[str, dict] = {}

    def apply(kind: str, subject_id: str | None, payload: dict, ward: str | None) -> None:
        nonlocal plan
        p = payload or {}
        if kind == "incident.opened" and subject_id:
            open_incidents.add(subject_id)
        elif kind == "incident.resolved" and subject_id:
            open_incidents.discard(subject_id)
            # Anything committed to it is free again, and anything it could not
            # get is no longer a gap.
            for res, a in list(assigned.items()):
                if a["incident"] == subject_id:
                    assigned.pop(res, None)
            uncovered.pop(subject_id, None)
        elif kind == "assignment.created":
            # subject_id is the assignment/incident; the unit is in the payload.
            res = p.get("resource_id")
            if res:
                assigned[res] = {"incident": subject_id, "progress": 0,
                                 "capability": p.get("capability"),
                                 "eta": p.get("eta_minutes")}
        elif kind == "assignment.changed":
            # Here subject_id IS the unit, and the payload names both ends. This
            # asymmetry with `assignment.created` is the single easiest thing to
            # get wrong in this fold.
            res = subject_id
            if res and p.get("to_incident"):
                assigned[res] = {"incident": p["to_incident"], "progress": 0,
                                 "capability": (assigned.get(res) or {}).get("capability"),
                                 "eta": None}
            elif res and p.get("progress_pct") is not None and res in assigned:
                assigned[res]["progress"] = p["progress_pct"]
        elif kind == "assignment.cancelled" and subject_id:
            assigned.pop(subject_id, None)
        elif kind == "alert.issued" and subject_id:
            alerts.append({"id": subject_id, "wardId": ward,
                           "severity": p.get("severity"),
                           "audience": p.get("audience"), "reach": p.get("reach")})
        elif kind == "plan.generated":
            plan = {"engine": p.get("engine"), "coverage": p.get("coverage"),
                    "units": p.get("units"), "demands": p.get("demands"),
                    "trigger": p.get("trigger")}
        elif kind == "demand.uncovered" and subject_id:
            uncovered[subject_id] = {"incidentId": subject_id,
                                     "capability": p.get("capability"),
                                     "reason": p.get("reason")}

    def snapshot(at: datetime, last_id: int) -> dict:
        return {
            "at": at.isoformat(),
            "eventId": last_id,
            "open": sorted(open_incidents),
            "assigned": {
                res: {"incident": a["incident"], "progress": a["progress"]}
                for res, a in assigned.items()
                # A unit committed to something already resolved is not on its
                # way anywhere; dropping it here keeps the frame consistent even
                # if an event pair arrived out of order.
                if a["incident"] in open_incidents
            },
            "alerts": list(alerts),
            "plan": dict(plan) if plan else None,
            "uncovered": list(uncovered.values()),
        }

    span = (end - start).total_seconds()
    marks = [start + timedelta(seconds=span * i / (steps - 1)) for i in range(steps)]

    out_frames: list[dict] = []
    cursor = 0
    for mark in marks:
        while cursor < len(rows) and rows[cursor]["occurred_at"] <= mark:
            r = rows[cursor]
            if r["kind"] in _FOLDED:
                apply(r["kind"], r["subject_id"], r["payload"] or {}, r["ward_id"])
            cursor += 1
        out_frames.append(snapshot(mark, int(rows[cursor - 1]["id"]) if cursor else 0))

    # ------------------------------------------------------------ ticker ----
    # Everything, including the kinds the fold ignores: the point of the ticker
    # is the causal story, and "a report arrived" is half of it.
    events = [
        {"id": int(r["id"]), "at": r["occurred_at"].isoformat(), "kind": r["kind"],
         "actor": r["actor"], "wardId": r["ward_id"],
         "subjectId": r["subject_id"],
         "causationId": int(r["causation_id"]) if r["causation_id"] else None,
         "text": _describe(r["kind"], r["payload"] or {}, r["subject_id"])}
        for r in rows
    ]

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "steps": steps,
        "incidents": incidents,
        "resources": resources,
        "frames": out_frames,
        "events": events,
        # Said in the payload, not only in the UI, so anything else consuming
        # this cannot mistake an interpolation for a recorded track.
        "positionsAre": "reconstructed",
        "positionsNote": (
            "Unit positions are interpolated from assignment progress. No "
            "per-unit location history is recorded, so these are where a unit "
            "must have been, not where it was observed."
        ),
    }
