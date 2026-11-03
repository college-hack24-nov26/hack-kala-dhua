"""The rest of the surge plan, on top of the ladder in app/surge/service.py.

Each step is an ordinary row the planner, the console and the public already
read - incidents with needs (the solver assigns the buses and police), lifelines
(guidance sends people to what is open), alerts (the citizen app and the mesh
broadcast them) - plus a `surge_actions` row that records what the ladder did
and why, and a `public_notices` row on the single public channel.

    escalation        each level names the authority that owns it:
                      local -> municipal -> district -> state -> national
    zones             wards graded A (evacuate now), B (prepare; evacuated
                      after A), C (shelter in place) from the latest ward risk
                      and the open flood incidents
    convoys           a zone-A ward's people move by bus convoy from an assembly
                      point to a named shelter on a fixed route (computed once on
                      the road graph, avoiding reported and predicted closures,
                      and published so drivers and residents use the same road);
                      the buses are assigned by the planner as `evacuation`
                      incident needs
    corridors         at mutual aid and above, police keep the roads of the
                      ambulances and relief trucks en route clear (a
                      `traffic_corridor` need at the route's midpoint)
    shelter options   schools (service.open_surge_shelters), shelters in
                      unaffected areas (transfer convoys from the fullest
                      shelter), requisitioned hotel rooms for elderly, disabled
                      and medical-needs families, registered host families, and
                      shelter-in-place advice for zone C
    crews             time on duty per unit; after a shift a unit rests (offline)
                      between jobs, and volunteers are asked for when several rest
    public channel    every instruction becomes alerts per ward, which the citizen
                      portal and the mesh broadcast already show (`public_notices`
                      keeps the order and what superseded what)
    demobilisation    stepping down closes what the step up opened: hotels and
                      host families when empty, aid units returned, surge
                      shelters closed when empty, an all-clear and a recovery
                      checklist (roads still closed, open incidents, people still
                      sheltered, stock to restock)

Times scale with the demo speed-up like aid response times. Everything is
guarded: if migration 035 is not applied the ladder still runs without these.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.logging import get_logger
from app.db import session as db
from app.world import clock as clocks
from app.world import events as ev

log = get_logger(__name__)

AUTHORITY = (
    {"tier": "local", "who": "Ward office and PCMC control room", "does": "routine dispatch"},
    {"tier": "municipal", "who": "PCMC Disaster Management Cell (Municipal Commissioner)",
     "does": "triage, rationing, redistribution, crew rotation"},
    {"tier": "district", "who": "District Collector, DDMA Pune",
     "does": "mutual aid from NGOs, volunteers and neighbouring corporations; priority corridors"},
    {"tier": "state", "who": "SDMA Maharashtra, SDRF, Relief & Rehabilitation",
     "does": "surge shelters, staged evacuation by convoy, requisitioned buses, hotels and host families"},
    {"tier": "national", "who": "NDMA, NDRF and the Armed Forces (through MHA)",
     "does": "declaration: NDRF, Army and helicopters (an officer approves)"},
)
SHIFT_H = 8.0
REST_H = 2.0
STAGE_GAP_MIN = 45
MAX_CORRIDORS = 2
BUS_SEATS = 45
_tables: bool | None = None


async def has_tables() -> bool:
    global _tables
    if _tables is None:
        try:
            n = await db.fetchval("select count(*) from pg_class where relname = any($1::text[]) and relkind = 'r'",
                                  ["surge_actions", "public_notices", "resource_shifts"])
            _tables = int(n or 0) == 3
        except Exception:  # noqa: BLE001
            _tables = False
    return _tables


def _speedup() -> float:
    from app.surge.service import _speedup as s
    return s()


def _region_sql(alias: str = "w") -> str:
    return f"(($1 = 'ncr') = ({alias}.id like 'w-gzb%'))"


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


# ------------------------------------------------------------- primitives ---
async def action(region: str, kind: str, title: str, detail: dict | None = None, *, ward_id: str | None = None,
                 ref: str | None = None, status: str = "active") -> int | None:
    if not await has_tables():
        return None
    aid = await db.fetchval(
        "insert into surge_actions (region, kind, status, title, detail, ward_id, ref) "
        "values ($1,$2,$3,$4,$5::jsonb,$6,$7) returning id",
        region, kind, status, title, json.dumps(detail or {}, default=str), ward_id, ref)
    await ev.append(clock=clocks.WALL, kind=f"surge.{kind}", actor="agent:surge", subject_type="region",
                    subject_id=region, ward_id=ward_id, payload={"title": title, **(detail or {}), "action_id": aid})
    return aid


async def close_action(aid: int, why: str, status: str = "done") -> None:
    await db.execute("update surge_actions set status = $2, closed_at = now(), "
                     "detail = detail || jsonb_build_object('closed_because', $3::text) where id = $1",
                     aid, status, why)


async def publish(region: str, level: int, kind: str, headline: str, body: str, wards: list[str] | None = None,
                  ref: str | None = None, severity: int = 3) -> int | None:
    """The single public channel. A new notice of the same kind for the same ref
    supersedes the old one, so the public never sees two live instructions for
    the same place that disagree."""
    if not await has_tables():
        return None
    wards = [w for w in (wards or []) if w]
    if not wards:                       # region-wide notice: every ward of the region gets the alert
        rows = await db.fetch("select id from wards where (($1 = 'ncr') = (id like 'w-gzb%')) "
                              "and ($1 = 'ncr' or id like 'w-pc-%')", region)
        wards = [r["id"] for r in rows]
    await db.execute("update public_notices set superseded_at = now() where region = $1 and kind = $2 "
                     "and coalesce(ref, '') = coalesce($3, '') and superseded_at is null", region, kind, ref)
    nid = await db.fetchval(
        "insert into public_notices (region, level, kind, headline, body, ward_ids, ref) "
        "values ($1,$2,$3,$4,$5,$6,$7) returning id", region, level, kind, headline, body, wards, ref)
    for w in wards[:30]:
        try:
            await db.execute(
                "insert into alerts (ward_id, hazard, severity, headline, action, by_time, channels, language) "
                "values ($1, 'flood', $2, $3, $4, now() + interval '1 hour', $5, 'English')",
                w, max(1, min(5, severity)), headline[:200], body[:500], ["app", "sms", "mesh", "radio"])
        except Exception as exc:  # noqa: BLE001 - an alert that cannot be written must not stop the notice
            log.warning("public_alert_failed", ward=w, error=str(exc)[:160])
    await ev.append(clock=clocks.WALL, kind="public.notice", actor="agent:surge", subject_type="region",
                    subject_id=region, payload={"kind": kind, "headline": headline, "wards": wards, "notice_id": nid})
    return nid


async def _active(region: str, kind: str) -> list[dict]:
    rows = await db.fetch("select * from surge_actions where region = $1 and kind = $2 and status = 'active' "
                          "order by id", region, kind)
    return [dict(r) for r in rows]


def _d(v) -> dict:
    return v if isinstance(v, dict) else json.loads(v or "{}")


# ------------------------------------------------------------------ zones ---
async def zones(region: str) -> dict[str, list[dict]]:
    rows = await db.fetch(
        f"""
        select w.id, w.name, w.population, coalesce(w.elderly_share, 0) elderly,
               extensions.ST_X(w.centroid::extensions.geometry) lng, extensions.ST_Y(w.centroid::extensions.geometry) lat,
               coalesce(r.severity, 0) risk, coalesce(r.population_at_risk, 0) par,
               coalesce(i.n, 0) open_flood, coalesce(i.worst, 0) worst
          from wards w
          left join lateral (select severity, population_at_risk from ward_risks
                              where ward_id = w.id order by created_at desc limit 1) r on true
          left join lateral (select count(*) n, max(severity) worst from incidents
                              where ward_id = w.id and status <> 'resolved'
                                and category in ('flooded_road','waterlogging','person_stranded')) i on true
         where (($1 = 'ncr') = (w.id like 'w-gzb%'))
        """, region)
    out: dict[str, list[dict]] = {"A": [], "B": [], "C": []}
    for r in rows:
        r = dict(r)
        if region == "pune" and not r["id"].startswith("w-pc-"):
            continue                       # the Pune corridor is the operation; the rest of Pune is "unaffected"
        if (r["worst"] >= 4) or r["risk"] >= 4:
            z, why = "A", ("open flood incident of severity %d" % r["worst"]) if r["worst"] >= 4 else "ward risk severity 4+"
        elif r["risk"] == 3 or r["open_flood"] > 0:
            z, why = "B", "ward risk severity 3" if r["risk"] == 3 else f"{r['open_flood']} open flood report(s)"
        elif r["risk"] == 2:
            z, why = "C", "ward risk severity 2"
        else:
            continue
        r["why"] = why
        out[z].append(r)
    for z in out:
        out[z].sort(key=lambda r: (-r["worst"], -r["risk"], -r["elderly"]))
    return out


# --------------------------------------------------------------- shelters ---
async def _destination(region: str, avoid_wards: set[str], people: int, near: tuple[float, float],
                       unaffected_only: bool = False) -> dict | None:
    rows = await db.fetch(
        """
        select l.id, l.name, l.kind, l.ward_id, l.capacity, coalesce(l.occupancy, 0) occ,
               extensions.ST_X(l.location::extensions.geometry) lng, extensions.ST_Y(l.location::extensions.geometry) lat,
               coalesce((select severity from ward_risks r where r.ward_id = l.ward_id order by created_at desc limit 1), 0) risk
          from lifelines l
         where l.kind in ('shelter', 'relief_centre') and l.status in ('open', 'limited') and coalesce(l.capacity, 0) > 0
           and (($1 = 'ncr') = (l.ward_id like 'w-gzb%'))
        """, region)
    best, bd = None, 1e18
    for r in rows:
        spare = int(r["capacity"]) - int(r["occ"])
        if spare < min(people, BUS_SEATS) or r["ward_id"] in avoid_wards:
            continue
        if unaffected_only and (r["ward_id"] or "").startswith("w-pc-"):
            continue
        if unaffected_only and r["risk"] > 1:
            continue
        d = math.hypot((r["lng"] - near[0]) * 105.5, (r["lat"] - near[1]) * 110.9)
        if d < bd:
            best, bd = dict(r, spare=spare, km=round(d, 1)), d
    return best


async def _fixed_route(a: tuple[float, float], b: tuple[float, float]) -> dict:
    from app.agents.replan import _blocked_points
    from app.solver import routing
    try:
        async with db.transaction() as conn:
            blocked = await _blocked_points(conn, "pune", None)
        line = await routing.route_line(a, b, blocked)
        return {"coordinates": line.coordinates, "minutes": round(line.minutes, 1), "km": round(line.km, 2),
                "engine": line.engine, "streets": [s.street for s in line.steps if s.street][:12],
                "avoids_blocks": len(blocked)}
    except Exception as exc:  # noqa: BLE001
        return {"coordinates": [list(a), list(b)], "engine": "straight-line", "error": str(exc)[:120]}


async def _incident(category: str, title: str, ward_id: str, lng: float, lat: float, severity: int,
                    needs: dict[str, int]) -> str:
    iid = await db.fetchval(
        "insert into incidents (title, category, hazard, ward_id, city_id, location, severity, status, report_count, "
        "confidence, trust_score, first_reported_at, created_at, updated_at) values ($1,$2,'flood',$3,'pune', "
        "extensions.ST_SetSRID(extensions.ST_MakePoint($4,$5),4326)::extensions.geography, $6, 'confirmed', 1, 1.0, 1.0, "
        "now(), now(), now()) returning id::text", title, category, ward_id, lng, lat, severity)
    for cap, n in needs.items():
        await db.execute("insert into incident_needs (incident_id, capability_id, required, updated_at) "
                         "values ($1::uuid, $2, $3, now()) on conflict (incident_id, capability_id) do update "
                         "set required = excluded.required, updated_at = now()", iid, cap, n)
    await ev.append(clock=clocks.WALL, kind="incident.opened", actor="agent:surge", subject_type="incident",
                    subject_id=iid, ward_id=ward_id,
                    payload={"category": category, "severity": severity, "title": title, "needs": needs})
    try:
        from app.demo import runner as demo_runner
        demo_runner.state.dirty = True
    except Exception:  # noqa: BLE001
        pass
    return iid


async def convoy(region: str, level: int, ward: dict, *, stage: str, avoid: set[str], people: int | None = None,
                 kind: str = "convoy", origin: dict | None = None) -> int | None:
    """Move a ward's (or a full shelter's) people by bus on a fixed route."""
    people = people or int(max(BUS_SEATS, min(400, (ward.get("par") or 0) * 0.15 or ward["population"] * 0.004)))
    src = (origin["lng"], origin["lat"]) if origin else (ward["lng"], ward["lat"])
    dst = await _destination(region, avoid, people, src, unaffected_only=(kind == "transfer"))
    if dst is None and kind != "transfer":
        dst = await _destination(region, avoid, people, src, unaffected_only=True)
    if dst is None:
        await action(region, kind, f"No shelter place for {ward['name']}", {"people": people, "stage": stage,
                     "reason": "every open shelter outside the evacuation zone is full; hotels, host families "
                               "and schools are being opened"}, ward_id=ward["id"], status="cancelled")
        return None
    buses = max(1, min(4, math.ceil(people / BUS_SEATS)))
    route = await _fixed_route(src, (dst["lng"], dst["lat"]))
    where = origin["name"] if origin else f"{ward['name']} assembly point (ward centre)"
    title = (f"Evacuation convoy {stage}: {ward['name']} -> {dst['name']}" if kind == "convoy"
             else f"Transfer: {origin['name']} -> {dst['name']} (unaffected area)")
    iid = await _incident("evacuation", title, ward["id"], src[0], src[1], 4, {"mass_transport": buses})
    await db.execute("update lifelines set occupancy = occupancy where id = $1", dst["id"])
    aid = await action(region, kind, title, {
        "stage": stage, "people": people, "buses": buses, "assembly_point": {"name": where, "lng": src[0], "lat": src[1]},
        "destination": {"id": dst["id"], "name": dst["name"], "lng": dst["lng"], "lat": dst["lat"], "spare": dst["spare"]},
        "route": route, "incident_id": iid,
        "why": ward.get("why") or "shelter full; people moved to an unaffected area"}, ward_id=ward["id"], ref=iid)
    streets = ", ".join(route.get("streets", [])[:4]) or "the published route"
    await publish(region, level, "evacuation" if kind == "convoy" else "transfer",
                  (f"{ward['name']}: evacuate now (stage {stage})" if kind == "convoy"
                   else f"Moving people from {origin['name']} to {dst['name']}"),
                  (f"Go to {where}. {buses} bus(es) will run to {dst['name']} via {streets}. Bring medicines, "
                   "documents, a phone charger and water. Help elderly neighbours. Do not drive through water."
                   if kind == "convoy" else
                   f"{origin['name']} is full. Buses run to {dst['name']} via {streets}; follow the volunteers."),
                  [ward["id"]], ref=ward["id"] if kind == "convoy" else origin["id"], severity=5 if stage == "A" else 4)
    return aid


# ------------------------------------------------------------- the steps ---
async def staged_evacuation(region: str, level: int) -> None:
    z = await zones(region)
    active = await _active(region, "convoy")
    busy = {a["ward_id"] for a in active}
    a_wards = {w["id"] for w in z["A"]}
    zone_rows = await _active(region, "evacuation_zone")
    zoned = {(a["ward_id"], _d(a["detail"]).get("zone")) for a in zone_rows}
    for zname, rows in z.items():
        for w in rows:
            if (w["id"], zname) not in zoned:
                for old in zone_rows:
                    if old["ward_id"] == w["id"]:
                        await close_action(old["id"], f"re-zoned to {zname}")
                await action(region, "evacuation_zone", f"{w['name']}: zone {zname}",
                             {"zone": zname, "why": w["why"], "risk": w["risk"], "open_flood": w["open_flood"]},
                             ward_id=w["id"])
    for w in z["A"][:3]:
        if w["id"] not in busy:
            await convoy(region, level, w, stage="A", avoid=a_wards)
    a_done = not any(_d(a["detail"]).get("stage") == "A" for a in active)
    first_a = min((a["created_at"] for a in active if _d(a["detail"]).get("stage") == "A"), default=None)
    gap_ok = first_a is None or (datetime.now(timezone.utc) - first_a) >= timedelta(minutes=STAGE_GAP_MIN / _speedup())
    for w in z["B"][:3]:
        if w["id"] in busy:
            continue
        if a_done or gap_ok:
            await convoy(region, level, w, stage="B", avoid=a_wards)
        else:
            await publish(region, level, "prepare", f"{w['name']}: prepare to evacuate (stage B)",
                          "Pack medicines and documents and wait for the next instruction on this channel. "
                          "Zone A is moving first so the roads stay clear.", [w["id"]], ref=w["id"], severity=3)
    sip = {a["ward_id"] for a in await _active(region, "shelter_in_place")}
    for w in z["C"]:
        if w["id"] in sip:
            continue
        await action(region, "shelter_in_place", f"{w['name']}: shelter in place",
                     {"why": w["why"], "advice": "stay indoors, upper floors, away from drains and the river"},
                     ward_id=w["id"])
        await publish(region, level, "shelter_in_place", f"{w['name']}: stay where you are",
                      "Stay indoors, on an upper floor if water enters. Keep phones charged and drinking water "
                      "stored. Do not walk or drive through water. Leave only if told to on this channel.",
                      [w["id"]], ref=w["id"], severity=3)


async def settle_convoys(region: str, level: int) -> None:
    """A convoy whose buses have finished has moved its people: they are in the
    destination shelter now, the action is done and the public is told."""
    for a in await _active(region, "convoy") + await _active(region, "transfer"):
        d = _d(a["detail"])
        st = await db.fetchval("select status::text from incidents where id = $1::uuid", d.get("incident_id"))
        if st != "resolved":
            continue
        dest = d["destination"]["id"]
        moved = int(d.get("people") or 0)
        await db.execute("update lifelines set occupancy = least(capacity, coalesce(occupancy, 0) + $2), "
                         "status = case when coalesce(occupancy, 0) + $2 >= capacity then 'full' else status end "
                         "where id = $1", dest, moved)
        if a["kind"] == "transfer":
            await db.execute("update lifelines set occupancy = greatest(0, coalesce(occupancy, 0) - $2), "
                             "status = case when status = 'full' then 'limited' else status end where id = $1",
                             (d.get("origin") or {}).get("id") or d["assembly_point"].get("id", ""), moved)
        await close_action(a["id"], f"{moved} people reached {d['destination']['name']}")
        await ev.append(clock=clocks.WALL, kind="surge.convoy_arrived", actor="agent:surge", subject_type="lifeline",
                        subject_id=dest, ward_id=a["ward_id"],
                        payload={"people": moved, "title": a["title"], "reason": "buses completed the fixed route"})


async def corridors(region: str, level: int) -> None:
    active = await _active(region, "priority_corridor")
    for c in active:                                  # close a corridor whose unit has arrived or finished
        d = _d(c["detail"])
        st = await db.fetchval("select status::text from assignments where id = $1::uuid", d.get("assignment_id"))
        if st not in ("proposed", "approved", "en_route"):
            await db.execute("update incidents set status = 'resolved', updated_at = now() where id = $1::uuid",
                             d.get("incident_id"))
            await close_action(c["id"], "the unit it protected has arrived")
    active = await _active(region, "priority_corridor")
    if len(active) >= MAX_CORRIDORS:
        return
    have = {_d(c["detail"]).get("assignment_id") for c in active}
    rows = await db.fetch(
        f"""
        select a.id::text aid, r.id rid, r.label, r.kind, a.ward_id,
               extensions.ST_X(extensions.ST_LineInterpolatePoint(a.route, 0.5)) lng,
               extensions.ST_Y(extensions.ST_LineInterpolatePoint(a.route, 0.5)) lat,
               a.eta_minutes, i.title
          from assignments a join resources r on r.id = a.resource_id join incidents i on i.id = a.incident_id
         where a.status in ('approved', 'en_route') and a.route is not null and r.kind in ('ambulance', 'supply_truck')
           and a.progress < 0.5 and coalesce(a.eta_minutes, 0) >= 8
           and (($1 = 'ncr') = (a.ward_id like 'w-gzb%'))
         order by (r.kind = 'ambulance') desc, a.eta_minutes desc limit 4
        """, region)
    for r in rows:
        if r["aid"] in have or len(have) >= MAX_CORRIDORS:
            continue
        iid = await _incident("traffic_corridor", f"Keep the road clear for {r['label']} ({r['title']})",
                              r["ward_id"], r["lng"], r["lat"], 3, {"traffic_control": 1})
        await action(region, "priority_corridor", f"Priority corridor for {r['label']}",
                     {"assignment_id": r["aid"], "unit": r["rid"], "kind": r["kind"], "incident_id": iid,
                      "point": {"lng": r["lng"], "lat": r["lat"]}, "eta_minutes": r["eta_minutes"],
                      "why": "ambulances and relief trucks get the road first; police hold the busiest junction"},
                     ward_id=r["ward_id"], ref=r["aid"])
        await publish(region, level, "corridor", "Keep the road clear for emergency vehicles",
                      f"An {'ambulance' if r['kind'] == 'ambulance' else 'relief truck'} is on its way through your "
                      "area. Pull over, do not park on the main road, follow police directions.",
                      [r["ward_id"]], ref=r["aid"], severity=3)
        have.add(r["aid"])


async def open_pools(region: str, level: int, kinds: tuple[str, ...] = ("hotel", "host_family")) -> list[str]:
    rows = await db.fetch(
        "select id, kind, name, capacity, ward_id from lifelines where kind = any($2::text[]) and status = 'closed' "
        "and (($1 = 'ncr') = (ward_id like 'w-gzb%')) order by capacity desc", region, list(kinds))
    opened = []
    for r in rows:
        await db.execute("update lifelines set status = 'open', opened_at = now(), last_reported_at = now() where id = $1",
                         r["id"])
        who = ("elderly, disabled and medical-needs families first" if r["kind"] == "hotel"
               else "families with children, matched by the volunteer desk")
        await action(region, "shelter_opened", f"Opened: {r['name']}",
                     {"lifeline": r["id"], "kind": r["kind"], "capacity": r["capacity"], "for": who,
                      "why": "shelters near capacity after schools were opened"}, ward_id=r["ward_id"], ref=r["id"])
        opened.append(r["id"])
    if opened:
        await publish(region, level, "shelter_options", "More places to stay are open",
                      "Requisitioned hotel rooms (for elderly, disabled and medical-needs families) and registered "
                      "host families are taking people. Ask at any shelter desk or reply to this message.",
                      [r["ward_id"] for r in rows if r["id"] in opened], ref="pools", severity=3)
    return opened


async def transfer_from_full(region: str, level: int) -> None:
    full = await db.fetch(
        "select id, name, ward_id, capacity, coalesce(occupancy,0) occ, "
        "extensions.ST_X(location::extensions.geometry) lng, extensions.ST_Y(location::extensions.geometry) lat "
        "from lifelines where kind in ('shelter','relief_centre') and capacity > 0 "
        "and coalesce(occupancy,0)::float / capacity >= 0.9 and (($1 = 'ncr') = (ward_id like 'w-gzb%')) "
        "order by coalesce(occupancy,0)::float / capacity desc limit 2", region)
    busy = {a["ref"] for a in await _active(region, "transfer")}
    for s in full:
        if s["id"] in busy:
            continue
        ward = {"id": s["ward_id"], "name": s["name"], "population": 0, "par": 0, "lng": s["lng"], "lat": s["lat"]}
        people = min(180, int(s["occ"] * 0.2))
        aid = await convoy(region, level, ward, stage="T", avoid=set(), people=max(BUS_SEATS, people),
                           kind="transfer", origin=dict(s))
        if aid:
            await db.execute("update surge_actions set ref = $2, detail = detail || jsonb_build_object('origin', "
                             "jsonb_build_object('id', $2::text, 'name', $3::text)) where id = $1", aid, s["id"], s["name"])


async def crews(region: str, level: int) -> None:
    """Shift tracking: a crew busy for a shift rests (offline) between jobs."""
    if not await has_tables():
        return
    now = datetime.now(timezone.utc)
    shift = timedelta(hours=SHIFT_H / _speedup())
    rest = timedelta(hours=REST_H / _speedup())
    cmp = "<" if region == "pune" else ">="
    from app.surge.service import SPLIT_LON
    await db.execute(
        f"""
        insert into resource_shifts (resource_id, on_duty_since, updated_at)
        select id, now(), now() from resources
         where status in ('assigned','en_route','on_site') and id not like 'AID-%'
           and extensions.ST_X(location::extensions.geometry) {cmp} {SPLIT_LON}
        on conflict (resource_id) do update set on_duty_since = coalesce(resource_shifts.on_duty_since, now()),
                                                updated_at = now()
        """)
    back = await db.fetch(
        "update resources r set status = 'available', unavailable_reason = null, status_note = null "
        "from resource_shifts s where s.resource_id = r.id and r.status = 'offline' "
        "and r.unavailable_reason = 'crew rest (rotation)' and s.rest_until <= $1 returning r.id", now)
    for b in back:
        await db.execute("update resource_shifts set on_duty_since = null, rest_until = null, updated_at = now() "
                         "where resource_id = $1", b["id"])
    if level < 1:
        return
    due = await db.fetch(
        f"""
        select r.id, r.label from resources r join resource_shifts s on s.resource_id = r.id
         where r.status = 'available' and s.on_duty_since <= $1 and s.rest_until is null
           and extensions.ST_X(r.location::extensions.geometry) {cmp} {SPLIT_LON}
         order by s.on_duty_since limit 3
        """, now - shift)
    for r in due:
        await db.execute("update resources set status = 'offline', unavailable_reason = 'crew rest (rotation)', "
                         "status_note = $2 where id = $1", r["id"], f"back at {(now + rest).strftime('%H:%M')} UTC")
        await db.execute("update resource_shifts set rest_until = $2, shifts = shifts + 1, updated_at = now() "
                         "where resource_id = $1", r["id"], now + rest)
        await action(region, "crew_rest", f"{r['label']}: crew rests",
                     {"unit": r["id"], "shift_hours": SHIFT_H, "rest_hours": REST_H,
                      "back_at": (now + rest).isoformat(), "why": "on duty for a full shift; rests between jobs"},
                     ref=r["id"])
    resting = await db.fetchval("select count(*) from resources where unavailable_reason = 'crew rest (rotation)'")
    if int(resting or 0) >= 3 and not await _active(region, "volunteers"):
        from app.surge import service
        made = await service.request_aid(region, {"unmet": {"by_capability": {"search_rescue": 3}}}, min_level=2)
        await action(region, "volunteers", "Volunteers called to cover resting crews",
                     {"resting": int(resting), "requested": made,
                      "why": "three or more crews are resting at once"})


# ---------------------------------------------------------------- driving ---
async def on_level_change(region: str, frm: int, to: int, m: dict) -> None:
    if not await has_tables():
        return
    a = AUTHORITY[to]
    await action(region, "escalation", f"Level {to}: {a['tier']} ({a['who']})",
                 {"from": frm, "to": to, "authority": a}, status="done")
    if to > frm:
        if to >= 2:
            await publish(region, to, "level", f"Flood response raised to {a['tier']} level",
                          f"{a['who']} now leads the response. Follow instructions on this channel only; "
                          "ignore forwarded messages that are not from it.", [], ref="level", severity=min(5, to + 1))
        if to >= 3:
            await staged_evacuation(region, to)
            await transfer_from_full(region, to)
            if m["shelters"]["occupancy_ratio"] >= 0.75:
                await open_pools(region, to)
    else:
        await demobilise(region, frm, to)


async def tick(region: str, level: int, m: dict) -> None:
    """Every evaluation: crews, corridors, convoys, and (at 3+) the next stage."""
    if not await has_tables():
        return
    try:
        await crews(region, level)
        await settle_convoys(region, level)
        if level >= 2:
            await corridors(region, level)
        if level >= 3:
            await staged_evacuation(region, level)
            if m["shelters"]["occupancy_ratio"] >= 0.9:
                await transfer_from_full(region, level)
                await open_pools(region, level)
    except Exception as exc:  # noqa: BLE001 - one failed step must not stop the ladder
        log.warning("surge_tick_failed", region=region, error=str(exc)[:240])


async def demobilise(region: str, frm: int, to: int) -> dict:
    """Stepping down undoes what stepping up opened, in reverse."""
    out: dict[str, Any] = {"from": frm, "to": to}
    if to < 3:
        pools = await db.fetch("update lifelines set status = 'closed' where kind in ('hotel','host_family') "
                               "and status <> 'closed' and coalesce(occupancy, 0) = 0 "
                               "and (($1 = 'ncr') = (ward_id like 'w-gzb%')) returning id", region)
        out["pools_closed"] = [p["id"] for p in pools]
        for kind in ("evacuation_zone", "shelter_in_place"):
            for a in await _active(region, kind):
                await close_action(a["id"], f"ladder stepped down to {to}")
        if pools:
            await action(region, "demobilise", "Hotels and host families closed (empty)", {"closed": out["pools_closed"]},
                         status="done")
    if to < 2:
        rel = await db.fetch(
            f"update resources set status = 'offline', unavailable_reason = 'returned to home agency', "
            f"status_note = 'demobilised' where id like 'AID-%' and status = 'available' "
            f"and extensions.ST_X(location::extensions.geometry) {'<' if region == 'pune' else '>='} 75.5 returning id")
        await db.execute("update surge_aid set outcome = 'cancelled', decided_at = now() where region = $1 "
                         "and outcome is null", region)
        out["aid_returned"] = [r["id"] for r in rel]
        for a in await _active(region, "priority_corridor"):
            await close_action(a["id"], "demobilised")
        if rel:
            await action(region, "demobilise", f"{len(rel)} mutual-aid unit(s) returned to their agencies",
                         {"units": out["aid_returned"]}, status="done")
    if to == 0:
        sh = await db.fetch("update lifelines set status = 'closed' where id like 'surge-%' and status <> 'closed' "
                            "and coalesce(occupancy, 0) = 0 and (($1 = 'ncr') = (ward_id like 'w-gzb%')) returning id",
                            region)
        out["surge_shelters_closed"] = [s["id"] for s in sh]
        chk = await recovery_checklist(region)
        out["recovery"] = chk
        await action(region, "recovery", "Recovery and demobilisation checklist", chk)
        await publish(region, 0, "level", "All clear: the flood response is standing down",
                      "Return home only when your ward is listed as clear. Do not touch fallen wires; report damage "
                      "on this channel. Shelters stay open for people who cannot go home yet.", [], ref="level",
                      severity=2)
    return out


async def recovery_checklist(region: str) -> dict:
    cmp = "<" if region == "pune" else ">="
    roads = await db.fetch(
        f"select reason, extensions.ST_X(location::extensions.geometry) lng, extensions.ST_Y(location::extensions.geometry) lat "
        f"from road_blocks where active and extensions.ST_X(location::extensions.geometry) {cmp} 75.5 limit 50")
    inc = await db.fetchval(f"select count(*) from incidents where status <> 'resolved' "
                            f"and extensions.ST_X(location::extensions.geometry) {cmp} 75.5")
    shel = await db.fetch(f"select id, name, occupancy, capacity from lifelines where coalesce(occupancy,0) > 0 "
                          f"and kind in ('shelter','relief_centre','hotel','host_family') "
                          f"and extensions.ST_X(location::extensions.geometry) {cmp} 75.5 order by occupancy desc")
    low = await db.fetch(
        f"select id, name from lifelines l where l.supplies <> '{{}}'::jsonb "
        f"and extensions.ST_X(l.location::extensions.geometry) {cmp} 75.5 and exists (select 1 from jsonb_each_text(l.supplies) s "
        f"where (s.value)::numeric < 0.5 * coalesce((l.supplies_baseline->>s.key)::numeric, 1))")
    aid = await db.fetchval("select count(*) from resources where id like 'AID-%' and status <> 'offline'")
    resting = await db.fetchval("select count(*) from resources where unavailable_reason = 'crew rest (rotation)'")
    return {
        "roads_still_closed": [dict(r) for r in roads], "open_incidents": int(inc or 0),
        "people_still_sheltered": sum(int(s["occupancy"] or 0) for s in shel),
        "shelters_occupied": [dict(s) for s in shel[:20]],
        "relief_sites_to_restock": [dict(r) for r in low],
        "aid_units_still_deployed": int(aid or 0), "crews_resting": int(resting or 0),
        "steps": ["reopen roads after a crew inspection (official reopen notice)", "close incidents with a field report",
                  "move people home ward by ward as wards are cleared", "restock relief sites to baseline",
                  "return mutual-aid units and settle requisitions", "debrief: export the surge log and unit logs"],
    }


async def reset() -> None:
    """World reset: nothing the surge opened survives into the next run."""
    if not await has_tables():
        return
    await db.execute("update surge_actions set status = 'cancelled', closed_at = now() where status = 'active'")
    await db.execute("update public_notices set superseded_at = now() where superseded_at is null")
    await db.execute("delete from resource_shifts")
    await db.execute("update lifelines set status = 'closed', occupancy = 0 "
                     "where kind in ('hotel','host_family','school') or id like 'surge-%'")


async def overview(region: str) -> dict:
    if not await has_tables():
        return {"available": False, "reason": "run migration 035"}
    acts = await db.fetch("select * from surge_actions where region = $1 order by id desc limit 200", region)
    notes = await db.fetch("select * from public_notices where region = $1 order by id desc limit 40", region)
    pools = await db.fetch(
        "select id, kind, name, capacity, coalesce(occupancy,0) occupancy, status from lifelines "
        "where kind in ('hotel','host_family','school') or id like 'surge-%' "
        "order by kind, id")
    shifts = await db.fetch(
        "select s.resource_id, r.label, s.on_duty_since, s.rest_until, s.shifts, r.status::text status "
        "from resource_shifts s join resources r on r.id = s.resource_id "
        "where s.on_duty_since is not null or s.rest_until is not null order by s.on_duty_since nulls last limit 60")
    z = await zones(region)
    by: dict[str, list] = {}
    for a in acts:
        d = dict(a)
        d["detail"] = _d(d["detail"])
        d = {k: _iso(v) for k, v in d.items()}
        by.setdefault(d["kind"], []).append(d)
    return {
        "available": True, "authority": list(AUTHORITY), "shiftHours": SHIFT_H, "restHours": REST_H,
        "zones": {k: [{"id": w["id"], "name": w["name"], "why": w["why"], "risk": w["risk"],
                       "openFlood": w["open_flood"]} for w in v] for k, v in z.items()},
        "actions": by,
        "notices": [{k: _iso(v) for k, v in dict(n).items()} for n in notes],
        "pools": [dict(p) for p in pools],
        "shifts": [{k: _iso(v) for k, v in dict(s).items()} for s in shifts],
    }
