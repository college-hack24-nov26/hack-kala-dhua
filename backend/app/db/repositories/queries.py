"""Read models.

Explicit SQL, one function per question the API asks. PostGIS geometry is
converted to plain lng/lat pairs here so nothing above this layer has to know
about WKB or SRIDs.
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from app.db import session as db
from app.hazards.base import WardContext
from app.schemas.domain import (
    Alert,
    AllocationPlan,
    Assignment,
    Authority,
    Decision,
    FieldTask,
    HazardType,
    Incident,
    Lifeline,
    Shelter,
    SolverStats,
    UncoveredDemand,
    Ward,
    WardLocation,
    WardRisk,
)

# ST_AsGeoJSON on a geography column gives {"type":"Point","coordinates":[lng,lat]}
_POINT = "extensions.ST_AsGeoJSON({col})::json -> 'coordinates'"


def _pt(value: Any) -> list[float]:
    if value is None:
        return [0.0, 0.0]
    if isinstance(value, str):
        value = json.loads(value)
    return [float(value[0]), float(value[1])]


def _ring(value: Any) -> list[list[float]]:
    if value is None:
        return []
    if isinstance(value, str):
        value = json.loads(value)
    # Polygon coordinates are [[ring]]; we only carry the outer ring.
    ring = value[0] if value and isinstance(value[0][0], (list, tuple)) else value
    return [[float(p[0]), float(p[1])] for p in ring]


# ----------------------------------------------------------------- wards ---
_WARD_SQL = f"""
select id, city_id, number, name, population, elderly_share, elevation_m, area_sq_km,
       {_POINT.format(col='centroid')} as centroid,
       extensions.ST_AsGeoJSON(boundary)::json -> 'coordinates' as boundary
from wards
where city_id = $1
order by (number !~ '^[0-9]+$'), nullif(regexp_replace(number, '[^0-9]', '', 'g'), '')::int, number
"""


async def list_wards(city_id: str = "pune") -> list[Ward]:
    rows = await db.fetch(_WARD_SQL, city_id)
    return [
        Ward(
            id=r["id"],
            city_id=r["city_id"],
            number=r["number"],
            name=r["name"],
            centroid=_pt(r["centroid"]),
            boundary=_ring(r["boundary"]),
            population=r["population"],
            elderly_share=float(r["elderly_share"]),
            elevation_m=float(r["elevation_m"]),
            area_sq_km=float(r["area_sq_km"]),
        )
        for r in rows
    ]


async def ward_contexts(hazard: str, city_id: str = "pune") -> list[WardContext]:
    """Everything an adapter is allowed to see, in one round trip."""
    rows = await db.fetch(
        f"""
        with lf as (
          select ward_id, kind, count(*)::int as n
          from lifelines group by ward_id, kind
        ),
        hist as (
          select ward_id, count(*)::int as n
          from incidents where hazard = $1 group by ward_id
        )
        select w.id, w.number, w.name, w.population, w.elderly_share,
               w.elevation_m, w.area_sq_km,
               {_POINT.format(col='w.centroid')} as centroid,
               coalesce(jsonb_object_agg(lf.kind, lf.n)
                        filter (where lf.kind is not null), '{{}}'::jsonb) as lifelines,
               coalesce(max(hist.n), 0) as past_events,
               (select min(w2.elevation_m) from wards w2
                 where w2.city_id = $2
                   -- one deployment can hold several regions (Pune and
                   -- Ghaziabad); the flood datum is the local one
                   and extensions.ST_DWithin(w2.centroid, w.centroid, 60000)
               ) as city_min_elevation
        from wards w
        left join lf on lf.ward_id = w.id
        left join hist on hist.ward_id = w.id
        where w.city_id = $2
        group by w.id
        order by (w.number !~ '^[0-9]+$'), nullif(regexp_replace(w.number, '[^0-9]', '', 'g'), '')::int, w.number
        """,
        str(hazard),
        city_id,
    )
    return [
        WardContext(
            ward_id=r["id"],
            number=r["number"],
            name=r["name"],
            centroid=tuple(_pt(r["centroid"])),  # type: ignore[arg-type]
            population=r["population"],
            elderly_share=float(r["elderly_share"]),
            elevation_m=float(r["elevation_m"]),
            area_sq_km=float(r["area_sq_km"]),
            city_min_elevation_m=float(r["city_min_elevation"]),
            lifelines=dict(r["lifelines"] or {}),
            past_events=int(r["past_events"]),
        )
        for r in rows
    ]


async def list_lifelines() -> list[Lifeline]:
    rows = await db.fetch(
        f"select id, kind, name, ward_id, capacity, {_POINT.format(col='location')} as loc from lifelines"
    )
    return [
        Lifeline(
            id=r["id"],
            kind=r["kind"],
            name=r["name"],
            ward_id=r["ward_id"],
            capacity=r["capacity"],
            location=_pt(r["loc"]),
        )
        for r in rows
    ]


async def list_shelters() -> list[Shelter]:
    rows = await db.fetch(
        f"""
        select id, name, ward_id, capacity, occupancy,
               {_POINT.format(col='location')} as loc
        from lifelines where kind = 'shelter' order by name
        """
    )
    return [
        Shelter(
            id=r["id"],
            name=r["name"],
            ward_id=r["ward_id"],
            capacity=r["capacity"] or 0,
            occupancy=r["occupancy"] or 0,
            location=_pt(r["loc"]),
        )
        for r in rows
    ]


async def nearest_shelter(lng: float, lat: float) -> tuple[Shelter, float] | None:
    """Nearest shelter with room, and how far away it is in km.

    Distance is computed by PostGIS on the geography type, so it is a real
    great-circle distance rather than a degrees approximation.
    """
    row = await db.fetchrow(
        f"""
        select id, name, ward_id, capacity, occupancy,
               {_POINT.format(col='location')} as loc,
               extensions.ST_Distance(
                 location,
                 extensions.ST_SetSRID(extensions.ST_MakePoint($1, $2), 4326)::extensions.geography
               ) / 1000.0 as km
        from lifelines
        where kind = 'shelter' and coalesce(occupancy, 0) < coalesce(capacity, 0)
        order by km asc
        limit 1
        """,
        lng,
        lat,
    )
    if row is None:
        return None
    shelter = Shelter(
        id=row["id"],
        name=row["name"],
        ward_id=row["ward_id"],
        capacity=row["capacity"] or 0,
        occupancy=row["occupancy"] or 0,
        location=_pt(row["loc"]),
    )
    return shelter, round(float(row["km"]), 2)


# ------------------------------------------------------------------ risk ---
async def latest_run(hazard: str) -> dict | None:
    row = await db.fetchrow(
        """
        select id, hazard, started_at, sources, mode, replay_of
        from hazard_runs where hazard = $1
        order by started_at desc limit 1
        """,
        str(hazard),
    )
    return dict(row) if row else None


async def ward_risks(run_id: str) -> list[WardRisk]:
    rows = await db.fetch(
        """
        select ward_id, hazard, score, severity, lead_time_hours, confidence,
               population_at_risk, drivers, projection
        from ward_risks where run_id = $1 order by score desc
        """,
        run_id,
    )
    return [
        WardRisk(
            ward_id=r["ward_id"],
            hazard=r["hazard"],
            score=float(r["score"]),
            severity=r["severity"],
            lead_time_hours=float(r["lead_time_hours"]),
            confidence=float(r["confidence"]),
            population_at_risk=r["population_at_risk"],
            drivers=r["drivers"] or [],
            projection=r["projection"] or [],
        )
        for r in rows
    ]


# ------------------------------------------------------------- incidents ---
async def list_incidents() -> list[Incident]:
    rows = await db.fetch(
        f"""
        select i.id::text, i.title, i.category, i.hazard, i.ward_id, i.severity,
               i.status, i.report_count, i.confidence, i.created_at, i.updated_at,
               {_POINT.format(col='i.location')} as loc,
               coalesce(array_agg(r.id::text) filter (where r.id is not null), '{{}}') as report_ids
        from incidents i
        left join citizen_reports r on r.incident_id = i.id
        group by i.id
        order by i.severity desc, i.created_at desc
        """
    )
    return [
        Incident(
            id=r["id"],
            title=r["title"],
            category=r["category"],
            hazard=r["hazard"],
            ward_id=r["ward_id"],
            location=_pt(r["loc"]),
            severity=r["severity"],
            status=r["status"],
            report_count=r["report_count"],
            confidence=float(r["confidence"]),
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            report_ids=list(r["report_ids"]),
        )
        for r in rows
    ]


# ------------------------------------------------------------- decisions ---
async def list_decisions() -> list[Decision]:
    rows = await db.fetch(
        """
        select id::text, created_at, hazard, action, target, ward_id, rationale,
               confidence, authority, status, decided_by, decided_at,
               override_note, agent_run_id::text
        from decisions order by created_at desc
        """
    )
    return [
        Decision(
            id=r["id"],
            created_at=r["created_at"],
            hazard=r["hazard"],
            action=r["action"],
            target=r["target"],
            ward_id=r["ward_id"],
            rationale=r["rationale"],
            confidence=float(r["confidence"]),
            authority=Authority(**(r["authority"] or {})),
            status=r["status"],
            decided_by=r["decided_by"],
            decided_at=r["decided_at"],
            override_note=r["override_note"],
            agent_run_id=r["agent_run_id"],
        )
        for r in rows
    ]


# -------------------------------------------------------------- resources --
async def list_resources() -> list[dict]:
    rows = await db.fetch(
        f"""
        select id, kind, label, operator, capacity, status,
               {_POINT.format(col='location')} as loc,
               {_POINT.format(col='base_location')} as base
        from resources order by kind, label
        """
    )
    return [
        {
            "id": r["id"],
            "kind": r["kind"],
            "label": r["label"],
            "operator": r["operator"],
            "capacity": r["capacity"],
            "status": r["status"],
            "location": _pt(r["loc"]),
            "base_location": _pt(r["base"]),
            "assignment_id": None,
        }
        for r in rows
    ]


async def latest_plan() -> AllocationPlan | None:
    plan = await db.fetchrow(
        """
        select id::text, hazard, generated_at, objective, solver, uncovered
        from allocation_plans order by generated_at desc limit 1
        """
    )
    if plan is None:
        return None
    rows = await db.fetch(
        """
        select id::text, resource_id, incident_id::text, ward_id, purpose,
               eta_minutes, distance_km, status, created_at
        from assignments where plan_id = $1 order by eta_minutes
        """,
        plan["id"],
    )
    solver = plan["solver"] or {}
    return AllocationPlan(
        id=plan["id"],
        hazard=plan["hazard"],
        generated_at=plan["generated_at"],
        objective=plan["objective"],
        solver=SolverStats(
            engine=solver.get("engine", "greedy-fallback"),
            runtime_ms=solver.get("runtime_ms", 0),
            variables=solver.get("variables", 0),
            constraints=solver.get("constraints", 0),
            coverage=solver.get("coverage", 0.0),
        ),
        uncovered=[UncoveredDemand(**u) for u in (plan["uncovered"] or [])],
        assignments=[
            Assignment(
                id=r["id"],
                resource_id=r["resource_id"],
                incident_id=r["incident_id"],
                ward_id=r["ward_id"],
                purpose=r["purpose"],
                eta_minutes=r["eta_minutes"],
                distance_km=float(r["distance_km"]),
                status=r["status"],
                created_at=r["created_at"],
            )
            for r in rows
        ],
    )


# ---------------------------------------------------------------- alerts ---
async def list_alerts(ward_id: str | None = None) -> list[Alert]:
    sql = """
        select id::text, ward_id, hazard, severity, headline, action, by_time,
               safe_location, channels, issued_at, decision_id::text, language, reach
        from alerts
    """
    args: Sequence[Any] = ()
    if ward_id:
        sql += " where ward_id = $1"
        args = (ward_id,)
    sql += " order by issued_at desc"
    rows = await db.fetch(sql, *args)
    return [
        Alert(
            id=r["id"],
            ward_id=r["ward_id"],
            hazard=r["hazard"],
            severity=r["severity"],
            headline=r["headline"],
            action=r["action"],
            by_time=r["by_time"],
            safe_location=r["safe_location"],
            channels=list(r["channels"] or []),
            issued_at=r["issued_at"],
            decision_id=r["decision_id"],
            language=r["language"],
            reach=r["reach"],
        )
        for r in rows
    ]


# ----------------------------------------------------------- field tasks ---
async def list_field_tasks(operator: str | None = None) -> list[FieldTask]:
    sql = f"""
        select id::text, assignment_id::text, resource_id, operator, title,
               instruction, ward_id, status, accepted_at, completed_at,
               proof_note, priority, {_POINT.format(col='location')} as loc
        from field_tasks
    """
    args: Sequence[Any] = ()
    if operator:
        sql += " where operator = $1"
        args = (operator,)
    sql += " order by priority desc, created_at"
    rows = await db.fetch(sql, *args)
    return [
        FieldTask(
            id=r["id"],
            assignment_id=r["assignment_id"],
            resource_id=r["resource_id"],
            operator=r["operator"],
            title=r["title"],
            instruction=r["instruction"],
            location=_pt(r["loc"]),
            ward_id=r["ward_id"],
            status=r["status"],
            accepted_at=r["accepted_at"],
            completed_at=r["completed_at"],
            proof_note=r["proof_note"],
            priority=r["priority"],
        )
        for r in rows
    ]


async def locate_ward(lng: float, lat: float, city_id: str = "pune") -> WardLocation:
    """The ward containing this point, or the nearest one if it is outside.

    One query, two answers. `ST_Covers` against the boundary is the containment
    test; ordering by distance to the boundary gives the nearest ward for free
    when nothing contains the point. Both use the GiST indexes added with the
    Alandi extension, so this stays cheap enough to call on every page load.

    Distance is computed on the geography type, so it is metres over the
    ellipsoid rather than a degrees approximation that would be wrong by a
    different amount at every latitude.
    """
    row = await db.fetchrow(
        f"""
        with me as (
          select extensions.ST_SetSRID(
                   extensions.ST_MakePoint($1, $2), 4326
                 )::extensions.geography as g
        )
        select w.id, w.city_id, w.number, w.name, w.population, w.elderly_share,
               w.elevation_m, w.area_sq_km,
               {_POINT.format(col='w.centroid')} as centroid,
               extensions.ST_AsGeoJSON(w.boundary)::json -> 'coordinates' as boundary,
               extensions.ST_Covers(
                 w.boundary::extensions.geometry, me.g::extensions.geometry
               ) as inside,
               extensions.ST_Distance(w.boundary, me.g) / 1000.0 as km
          from wards w, me
         where w.city_id = $3
         order by inside desc, km asc
         limit 1
        """,
        lng,
        lat,
        city_id,
    )
    if row is None:
        return WardLocation(
            inside=False,
            ward=None,
            city_id=city_id,
            note="No wards are configured for this deployment yet.",
        )

    ward = Ward(
        id=row["id"],
        city_id=row["city_id"],
        number=row["number"],
        name=row["name"],
        centroid=_pt(row["centroid"]),
        boundary=_ring(row["boundary"]),
        population=row["population"],
        elderly_share=float(row["elderly_share"]),
        elevation_m=float(row["elevation_m"]),
        area_sq_km=float(row["area_sq_km"]),
    )
    inside = bool(row["inside"])
    km = round(float(row["km"]), 2)
    note = (
        f"You are in {ward.name}."
        if inside
        else (
            f"You are about {km:g} km outside the area this deployment covers. "
            f"The nearest covered ward is {ward.name}, and what you see below is "
            "its risk, not yours."
        )
    )
    return WardLocation(
        inside=inside, ward=ward, distance_km=0.0 if inside else km,
        city_id=city_id, note=note,
    )
