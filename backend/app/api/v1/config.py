"""Standing up a deployment, from a browser.

The portability claim has been true in the schema since the first migration:
hazards, categories, capabilities, resource kinds and lifeline kinds are rows,
not enums, so a new city needs no code. What it also needed until now was
somebody with psql, which is not a claim anybody can check in a demo and not a
thing a municipal IT department will do.

So this is that same portability, exposed. A city, its wards, its fleet, its
lifelines and its staff logins, all creatable from the configuration screen.
Nothing here is a special case: every write lands in the same tables the Pune
deployment uses, and the moment a ward exists the risk agent scores it and the
allocator can send units into it.

Two things are deliberately strict:

  * **Everything is staff-only, and creating logins is Commissioner-only.**
    A screen that can mint a ward officer account is an administrative
    interface, not a settings page.
  * **Nothing is deleted that is referenced.** A ward with incidents in it, or a
    unit with a live assignment, refuses rather than cascading. Losing the
    history is worse than keeping a row nobody wants.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx
from fastapi import APIRouter, Query
from pydantic import Field

from app.core.config import settings
from app.core.errors import BadRequest, Conflict, NotFound
from app.core.logging import get_logger
from app.core.security import CommissionerPrincipal, StaffPrincipal
from app.db import session as db
from app.schemas.domain import Camel
from app.taxonomy import cache as taxonomy
from app import taxonomy as taxonomy_module
from app.world import clock as clocks
from app.world import events as ev

log = get_logger(__name__)
router = APIRouter(tags=["configuration"])

ROLES = ("citizen", "field_operator", "ward_officer", "commissioner", "admin")


# ------------------------------------------------------------------ reads ---
@router.get("/config/deployment")
async def deployment(
    _: StaffPrincipal, city_id: str = Query(default="pune")
) -> dict:
    """Everything the configuration screen needs, in one call.

    Counts come back alongside the rows because the useful question on this
    screen is never "what wards exist", it is "which of them has nothing
    covering it".
    """
    (cities, wards, agencies, resources, lifelines, profiles) = (
        await db.fetch(
            """
            select c.id, c.name, c.country, c.timezone,
                   (select count(*) from wards w where w.city_id = c.id)::int wards,
                   (select count(*) from resources r where r.city_id = c.id)::int units,
                   (select count(*) from lifelines l where l.city_id = c.id)::int lifelines
              from cities c order by c.name
            """
        ),
        await db.fetch(
            """
            select w.id, w.name, w.number, w.population, w.city_id,
                   extensions.ST_X(w.centroid::extensions.geometry) lng,
                   extensions.ST_Y(w.centroid::extensions.geometry) lat,
                   extensions.ST_NPoints(w.boundary::extensions.geometry) points,
                   (select count(*) from incidents i
                     where i.ward_id = w.id and i.status <> 'resolved')::int open_incidents,
                   (select count(*) from lifelines l where l.ward_id = w.id)::int lifelines
              from wards w where w.city_id = $1 order by (w.number !~ '^[0-9]+$'), nullif(regexp_replace(w.number, '[^0-9]', '', 'g'), '')::int, w.number
            """,
            city_id,
        ),
        await db.fetch(
            "select id, name, short_name, kind, jurisdiction, active from agencies "
            "where city_id = $1 order by name",
            city_id,
        ),
        await db.fetch(
            """
            select r.id, r.kind, r.label, r.operator, r.agency_id, r.capacity,
                   r.status::text status, r.city_id,
                   extensions.ST_X(r.location::extensions.geometry) lng,
                   extensions.ST_Y(r.location::extensions.geometry) lat,
                   (select count(*) from assignments a
                     where a.resource_id = r.id
                       and a.status in ('proposed','approved','en_route','on_site'))::int committed
              from resources r where r.city_id = $1 order by r.kind, r.label
            """,
            city_id,
        ),
        await db.fetch(
            """
            select l.id, l.kind, l.name, l.ward_id, l.capacity, l.occupancy,
                   l.status, l.supplies, l.people_served_per_hour,
                   extensions.ST_X(l.location::extensions.geometry) lng,
                   extensions.ST_Y(l.location::extensions.geometry) lat
              from lifelines l where l.city_id = $1 order by l.kind, l.name
            """,
            city_id,
        ),
        await db.fetch(
            "select id::text, full_name, role, ward_id, operator from profiles "
            "order by role, full_name limit 200"
        ),
    )

    return {
        "cities": [dict(r) for r in cities],
        "wards": [
            {"id": r["id"], "name": r["name"], "number": r["number"],
             "population": r["population"], "cityId": r["city_id"],
             "centroid": [float(r["lng"]), float(r["lat"])],
             # Five points is the synthesised square; anything more was drawn
             # or imported.
             "boundaryDrawn": int(r["points"] or 0) > 5,
             "openIncidents": r["open_incidents"], "lifelines": r["lifelines"]}
            for r in wards
        ],
        "agencies": [dict(r) for r in agencies],
        "resources": [
            {"id": r["id"], "kind": r["kind"], "label": r["label"],
             "operator": r["operator"], "agencyId": r["agency_id"],
             "capacity": r["capacity"], "status": r["status"],
             "committed": r["committed"],
             "location": [float(r["lng"]), float(r["lat"])]}
            for r in resources
        ],
        "lifelines": [
            {"id": r["id"], "kind": r["kind"], "name": r["name"],
             "wardId": r["ward_id"], "capacity": r["capacity"],
             "occupancy": r["occupancy"], "status": r["status"],
             "supplies": r["supplies"] or {},
             "servedPerHour": r["people_served_per_hour"],
             "location": [float(r["lng"]), float(r["lat"])]}
            for r in lifelines
        ],
        "people": [
            {"id": r["id"], "fullName": r["full_name"], "role": r["role"],
             "wardId": r["ward_id"], "operator": r["operator"]}
            for r in profiles
        ],
        # The taxonomy is what makes this portable, so the screen shows it
        # rather than hiding it: these are the vocabularies a new deployment
        # inherits and can extend.
        "taxonomy": {
            "resourceKinds": [
                {"id": k.id, "label": k.display_name,
                 "defaultCapacity": k.default_capacity,
                 "capabilities": list(k.capabilities)}
                for k in taxonomy.resource_kinds.values()
            ],
            "lifelineKinds": [
                {"id": k.id, "label": k.display_name}
                for k in taxonomy.lifeline_kinds.values()
            ],
            "capabilities": [
                {"id": c.id, "label": c.label} for c in taxonomy.capabilities.values()
            ],
            "categories": [
                {"id": c.id, "label": c.display_name, "lifeSafety": c.life_safety}
                for c in taxonomy.categories.values()
            ],
            "roles": list(ROLES),
        },
    }


# ------------------------------------------------------------------ cities ---
class CityIn(Camel):
    id: str = Field(min_length=2, max_length=40, pattern=r"^[a-z0-9_-]+$")
    name: str = Field(min_length=2, max_length=120)
    country: str = "IN"
    timezone: str = "Asia/Kolkata"
    languages: list[str] = Field(default_factory=lambda: ["English"])
    #: What this city calls its subdivisions. Pune has wards; somewhere else has
    #: zones, circles or barangays, and the interface should use their word.
    admin_unit_singular: str = "ward"
    admin_unit_plural: str = "wards"
    #: Where the map opens.
    lng: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)


@router.post("/config/cities", status_code=201)
async def create_city(body: CityIn, principal: StaffPrincipal) -> dict:
    """A new deployment. No code changes, because there never were any."""
    exists = await db.fetchval("select 1 from cities where id = $1", body.id)
    if exists:
        raise Conflict(f"A city with id {body.id!r} already exists.")

    await db.execute(
        """
        insert into cities
          (id, name, country, timezone, languages,
           admin_unit_singular, admin_unit_plural, centroid)
        values ($1,$2,$3,$4,$5,$6,$7,
                extensions.ST_SetSRID(extensions.ST_MakePoint($8,$9),4326)::extensions.geography)
        """,
        body.id, body.name, body.country, body.timezone, body.languages,
        body.admin_unit_singular, body.admin_unit_plural, body.lng, body.lat,
    )
    await ev.append(
        clock=clocks.WALL, kind="config.city_created",
        actor=f"officer:{principal.full_name or 'unknown'}",
        subject_type="city", subject_id=body.id, city_id=body.id,
        payload={"name": body.name},
    )
    await taxonomy_module.load(force=True)
    return {"id": body.id, "name": body.name}


# ------------------------------------------------------------------- wards ---
class WardIn(Camel):
    id: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9_-]+$")
    city_id: str = "pune"
    name: str = Field(min_length=1, max_length=120)
    number: str = Field(min_length=1, max_length=12)
    population: int = Field(ge=0, le=50_000_000)
    lng: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)
    #: Optional polygon as [[lng,lat], ...]. Closed automatically.
    #:
    #: Without one, a square is drawn around the centroid sized from the area,
    #: and the response says the boundary was approximated. The schema requires
    #: a polygon because PostGIS is what decides which ward a report landed in,
    #: and a deployment that cannot answer that question cannot route anything.
    #: A rough square that answers it is more useful than a null that does not,
    #: as long as nobody is told it is a survey.
    boundary: list[list[float]] | None = None
    area_sq_km: float = Field(default=4.0, gt=0, le=10_000)
    elderly_share: float = Field(default=0.09, ge=0, le=1)
    elevation_m: float = Field(default=560.0, ge=-500, le=9000)


def _ring(points: list[list[float]]) -> str:
    if len(points) < 3:
        raise BadRequest("A boundary needs at least three points.")
    ring = [[float(p[0]), float(p[1])] for p in points]
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    return "POLYGON((" + ", ".join(f"{lng} {lat}" for lng, lat in ring) + "))"


def _square(lng: float, lat: float, area_sq_km: float) -> str:
    """A placeholder boundary, sized from the stated area.

    Honest about what it is: a square, centred on the point given, with the
    right area and none of the right shape. It exists so `ST_Covers` can answer
    "which ward is this report in" on day one, and it is meant to be replaced by
    the real geometry from the municipal register.
    """
    import math

    half_km = math.sqrt(max(area_sq_km, 0.01)) / 2.0
    dlat = half_km / 110.574
    dlng = half_km / (111.320 * max(math.cos(math.radians(lat)), 0.01))
    corners = [
        (lng - dlng, lat - dlat), (lng + dlng, lat - dlat),
        (lng + dlng, lat + dlat), (lng - dlng, lat + dlat),
        (lng - dlng, lat - dlat),
    ]
    return "POLYGON((" + ", ".join(f"{x} {y}" for x, y in corners) + "))"


@router.post("/config/wards", status_code=201)
async def create_ward(body: WardIn, principal: StaffPrincipal) -> dict:
    if not await db.fetchval("select 1 from cities where id = $1", body.city_id):
        raise NotFound(f"No city {body.city_id!r}. Create the city first.")
    if await db.fetchval("select 1 from wards where id = $1", body.id):
        raise Conflict(f"A ward with id {body.id!r} already exists.")

    drawn = bool(body.boundary)
    polygon = (
        _ring(body.boundary) if drawn
        else _square(body.lng, body.lat, body.area_sq_km)
    )
    await db.execute(
        """
        insert into wards
          (id, city_id, name, number, population, area_sq_km,
           elderly_share, elevation_m, centroid, boundary)
        values ($1,$2,$3,$4,$5,$6,$7,$8,
                extensions.ST_SetSRID(extensions.ST_MakePoint($9,$10),4326)::extensions.geography,
                extensions.ST_SetSRID(
                  extensions.ST_GeomFromText($11::text), 4326)::extensions.geography)
        """,
        body.id, body.city_id, body.name, body.number, body.population,
        body.area_sq_km, body.elderly_share, body.elevation_m,
        body.lng, body.lat, polygon,
    )
    await ev.append(
        clock=clocks.WALL, kind="config.ward_created",
        actor=f"officer:{principal.full_name or 'unknown'}",
        subject_type="ward", subject_id=body.id, city_id=body.city_id,
        ward_id=body.id,
        payload={"name": body.name, "population": body.population,
                 "boundary_drawn": drawn},
    )
    return {
        "id": body.id, "name": body.name, "boundaryDrawn": drawn,
        "note": (
            "Boundary taken from the shape you drew."
            if drawn
            else f"No boundary was given, so a {body.area_sq_km} km² square was "
                 "placed around the centre. Reports will locate correctly; "
                 "replace it with the real geometry when you have it."
        ),
    }


@router.delete("/config/wards/{ward_id}")
async def delete_ward(ward_id: str, principal: StaffPrincipal) -> dict:
    """Refuses rather than cascading. Losing the history is the worse outcome."""
    open_incidents = await db.fetchval(
        "select count(*) from incidents where ward_id = $1", ward_id
    )
    if open_incidents:
        raise Conflict(
            f"{open_incidents} incident(s) reference this ward. Deleting it "
            "would take their history with it, so it is refused."
        )
    lifelines = await db.fetchval(
        "select count(*) from lifelines where ward_id = $1", ward_id
    )
    if lifelines:
        raise Conflict(
            f"{lifelines} facility(ies) sit in this ward. Move or remove them first."
        )
    deleted = await db.execute("delete from wards where id = $1", ward_id)
    if deleted.endswith("0"):
        raise NotFound("No such ward.")
    await ev.append(
        clock=clocks.WALL, kind="config.ward_deleted",
        actor=f"officer:{principal.full_name or 'unknown'}",
        subject_type="ward", subject_id=ward_id, payload={},
    )
    return {"deleted": ward_id}


# --------------------------------------------------------------- resources ---
class ResourceIn(Camel):
    id: str | None = None
    city_id: str = "pune"
    kind: str
    label: str = Field(min_length=1, max_length=120)
    operator: str = Field(min_length=1, max_length=120)
    agency_id: str | None = None
    capacity: int | None = Field(default=None, ge=0, le=1_000_000)
    lng: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)


@router.post("/config/resources", status_code=201)
async def create_resource(body: ResourceIn, principal: StaffPrincipal) -> dict:
    """Register a unit.

    The kind is what carries the meaning: capability, and therefore what the
    solver will and will not send it to, comes from `resource_kind_capabilities`
    rather than from anything typed here. Registering a boat in a new city makes
    it eligible for water rescue there immediately, and for nothing else.
    """
    if body.kind not in taxonomy.resource_kinds:
        raise NotFound(
            f"Unknown resource kind {body.kind!r}. Known kinds: "
            + ", ".join(sorted(taxonomy.resource_kinds))
        )
    if body.agency_id and body.agency_id not in taxonomy.agencies:
        raise NotFound(f"Unknown agency {body.agency_id!r}.")

    kind = taxonomy.resource_kinds[body.kind]
    resource_id = body.id or await _next_id("resources", f"{body.kind}-")
    if await db.fetchval("select 1 from resources where id = $1", resource_id):
        raise Conflict(f"A unit with id {resource_id!r} already exists.")

    await db.execute(
        """
        insert into resources
          (id, kind, label, operator, agency_id, capacity, status,
           base_location, location, city_id)
        values ($1,$2,$3,$4,$5,$6,'available',
                extensions.ST_SetSRID(extensions.ST_MakePoint($7,$8),4326)::extensions.geography,
                extensions.ST_SetSRID(extensions.ST_MakePoint($7,$8),4326)::extensions.geography,
                $9)
        """,
        resource_id, body.kind, body.label, body.operator, body.agency_id,
        body.capacity if body.capacity is not None else kind.default_capacity,
        body.lng, body.lat, body.city_id,
    )
    await ev.append(
        clock=clocks.WALL, kind="config.resource_created",
        actor=f"officer:{principal.full_name or 'unknown'}",
        subject_type="resource", subject_id=resource_id, city_id=body.city_id,
        payload={"kind": body.kind, "label": body.label,
                 "capabilities": list(kind.capabilities)},
    )
    return {
        "id": resource_id, "kind": body.kind, "label": body.label,
        "capabilities": list(kind.capabilities),
    }


@router.delete("/config/resources/{resource_id}")
async def delete_resource(resource_id: str, principal: StaffPrincipal) -> dict:
    live = await db.fetchval(
        """
        select count(*) from assignments
         where resource_id = $1
           and status in ('proposed','approved','en_route','on_site')
        """,
        resource_id,
    )
    if live:
        raise Conflict(
            "That unit is on a live assignment. Stand it down from the field "
            "screen first, so the work it was doing goes back into unmet need "
            "rather than disappearing."
        )
    deleted = await db.execute("delete from resources where id = $1", resource_id)
    if deleted.endswith("0"):
        raise NotFound("No such unit.")
    await ev.append(
        clock=clocks.WALL, kind="config.resource_deleted",
        actor=f"officer:{principal.full_name or 'unknown'}",
        subject_type="resource", subject_id=resource_id, payload={},
    )
    return {"deleted": resource_id}


# --------------------------------------------------------------- lifelines ---
class LifelineIn(Camel):
    id: str | None = None
    city_id: str = "pune"
    kind: str
    name: str = Field(min_length=1, max_length=160)
    ward_id: str | None = None
    capacity: int = Field(default=0, ge=0, le=1_000_000)
    occupancy: int = Field(default=0, ge=0, le=1_000_000)
    accepts_casualties: bool = False
    specialities: list[str] = Field(default_factory=list)
    #: Opening stock, e.g. {"food_packets": 800, "water_litres": 5000}.
    supplies: dict[str, float] = Field(default_factory=dict)
    people_served_per_hour: int | None = Field(default=None, ge=0, le=100_000)
    lng: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)


@router.post("/config/lifelines", status_code=201)
async def create_lifeline(body: LifelineIn, principal: StaffPrincipal) -> dict:
    """Register a hospital, shelter, relief centre, kitchen or water point."""
    if body.kind not in taxonomy.lifeline_kinds:
        raise NotFound(
            f"Unknown facility kind {body.kind!r}. Known kinds: "
            + ", ".join(sorted(taxonomy.lifeline_kinds))
        )
    if body.ward_id and not await db.fetchval(
        "select 1 from wards where id = $1", body.ward_id
    ):
        raise NotFound(f"No ward {body.ward_id!r}.")

    lifeline_id = body.id or await _next_id("lifelines", f"{body.kind}-")
    if await db.fetchval("select 1 from lifelines where id = $1", lifeline_id):
        raise Conflict(f"A facility with id {lifeline_id!r} already exists.")

    await db.execute(
        """
        insert into lifelines
          (id, kind, name, ward_id, location, capacity, occupancy, city_id,
           accepts_casualties, status, specialities, supplies,
           people_served_per_hour, opened_at)
        values ($1,$2,$3,$4,
                extensions.ST_SetSRID(extensions.ST_MakePoint($5,$6),4326)::extensions.geography,
                $7,$8,$9,$10,'open',$11,$12,$13, now())
        """,
        lifeline_id, body.kind, body.name, body.ward_id, body.lng, body.lat,
        body.capacity, body.occupancy, body.city_id, body.accepts_casualties,
        body.specialities, {k: float(v) for k, v in body.supplies.items()},
        body.people_served_per_hour,
    )
    await ev.append(
        clock=clocks.WALL, kind="config.lifeline_created",
        actor=f"officer:{principal.full_name or 'unknown'}",
        subject_type="lifeline", subject_id=lifeline_id, city_id=body.city_id,
        ward_id=body.ward_id,
        payload={"kind": body.kind, "name": body.name,
                 "capacity": body.capacity},
    )
    return {"id": lifeline_id, "kind": body.kind, "name": body.name}


@router.delete("/config/lifelines/{lifeline_id}")
async def delete_lifeline(lifeline_id: str, principal: StaffPrincipal) -> dict:
    deleted = await db.execute("delete from lifelines where id = $1", lifeline_id)
    if deleted.endswith("0"):
        raise NotFound("No such facility.")
    await ev.append(
        clock=clocks.WALL, kind="config.lifeline_deleted",
        actor=f"officer:{principal.full_name or 'unknown'}",
        subject_type="lifeline", subject_id=lifeline_id, payload={},
    )
    return {"deleted": lifeline_id}


# ------------------------------------------------------------------ people ---
class PersonIn(Camel):
    email: str = Field(min_length=5, max_length=200)
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=120)
    role: Literal["citizen", "field_operator", "ward_officer", "commissioner", "admin"]
    #: A ward officer is scoped to a ward; a field operator to an agency's fleet.
    ward_id: str | None = None
    operator: str | None = None


@router.post("/config/people", status_code=201)
async def create_person(body: PersonIn, principal: CommissionerPrincipal) -> dict:
    """Create a login and promote it to a role.

    Commissioner-only, because a screen that can mint a ward officer account is
    an administrative interface rather than a settings page.

    The role is written to `profiles`, never taken from the signup payload. That
    is the whole reason this endpoint exists instead of letting people
    self-register with a role: the database policies authorise against
    `profiles.role`, so anything that can set it is a privilege boundary and
    belongs behind one.
    """
    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise BadRequest(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be configured to "
            "create logins."
        )
    if body.role == "field_operator" and not body.operator:
        raise BadRequest(
            "A field operator needs an operator, or they will sign in to an "
            "empty fleet."
        )
    if body.ward_id and not await db.fetchval(
        "select 1 from wards where id = $1", body.ward_id
    ):
        raise NotFound(f"No ward {body.ward_id!r}.")

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                f"{settings.supabase_url.rstrip('/')}/auth/v1/admin/users",
                headers={
                    "apikey": settings.supabase_service_role_key,
                    "Authorization": f"Bearer {settings.supabase_service_role_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "email": body.email,
                    "password": body.password,
                    "email_confirm": True,
                    "user_metadata": {"full_name": body.full_name},
                },
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("supabase_admin_unreachable", error=str(exc)[:200])
        raise BadRequest("Could not reach Supabase to create the login.") from exc

    if response.status_code >= 400:
        detail = ""
        try:
            body_json = response.json()
            detail = str(body_json.get("msg") or body_json.get("message") or "")
        except Exception:  # noqa: BLE001
            detail = response.text[:200]
        if response.status_code in (409, 422) or "already" in detail.lower():
            raise Conflict(f"That email already has an account. {detail}".strip())
        raise BadRequest(f"Supabase refused the signup. {detail}".strip())

    user_id = (response.json() or {}).get("id")
    if not user_id:
        raise BadRequest("Supabase created no user id.")

    await db.execute(
        """
        insert into profiles (id, full_name, role, ward_id, operator)
        values ($1::uuid,$2,$3,$4,$5)
        on conflict (id) do update
           set full_name = excluded.full_name,
               role = excluded.role,
               ward_id = excluded.ward_id,
               operator = excluded.operator
        """,
        user_id, body.full_name, body.role, body.ward_id, body.operator,
    )
    await ev.append(
        clock=clocks.WALL, kind="config.person_created",
        actor=f"officer:{principal.full_name or 'unknown'}",
        subject_type="profile", subject_id=str(user_id),
        ward_id=body.ward_id,
        # The email is not written to the audit log. It identifies a person and
        # the log is read by everyone with console access.
        payload={"role": body.role, "operator": body.operator},
    )
    return {
        "id": user_id, "fullName": body.full_name, "role": body.role,
        "wardId": body.ward_id, "operator": body.operator,
    }


class RoleIn(Camel):
    role: Literal["citizen", "field_operator", "ward_officer", "commissioner", "admin"]
    ward_id: str | None = None
    operator: str | None = None


@router.patch("/config/people/{user_id}")
async def set_role(user_id: str, body: RoleIn, principal: CommissionerPrincipal) -> dict:
    """Change what somebody is allowed to do. Same boundary as creating them."""
    updated = await db.execute(
        """
        update profiles
           set role = $2, ward_id = $3, operator = $4
         where id = $1::uuid
        """,
        user_id, body.role, body.ward_id, body.operator,
    )
    if updated.endswith("0"):
        raise NotFound("No such person.")
    await ev.append(
        clock=clocks.WALL, kind="config.role_changed",
        actor=f"officer:{principal.full_name or 'unknown'}",
        subject_type="profile", subject_id=user_id, ward_id=body.ward_id,
        payload={"role": body.role, "operator": body.operator},
    )
    return {"id": user_id, "role": body.role}


# ------------------------------------------------------------------ helper ---
async def _next_id(table: str, prefix: str) -> str:
    """A readable id that does not collide.

    Sequential rather than a uuid because these end up on a crew's screen and in
    a radio call: "unit-boat-7" can be said out loud and a uuid cannot.
    """
    n = await db.fetchval(
        f"select count(*) from {table} where id like $1", f"{prefix}%"
    )
    candidate = f"{prefix}{int(n or 0) + 1}"
    while await db.fetchval(f"select 1 from {table} where id = $1", candidate):
        n = int(n or 0) + 1
        candidate = f"{prefix}{n + 1}"
    return candidate


@router.post("/config/reload-taxonomy")
async def reload(_: StaffPrincipal) -> dict[str, Any]:
    """Re-read the reference tables without a restart.

    Adding a resource kind or a capability by SQL is a supported way to extend a
    deployment; it just needs the cache to notice.
    """
    await taxonomy_module.load(force=True)
    return {
        "resourceKinds": len(taxonomy.resource_kinds),
        "lifelineKinds": len(taxonomy.lifeline_kinds),
        "capabilities": len(taxonomy.capabilities),
        "categories": len(taxonomy.categories),
        "agencies": len(taxonomy.agencies),
    }
