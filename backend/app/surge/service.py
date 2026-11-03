"""Surge operations: what the system does when units and shelters run out.

`evaluate(region)` runs after every replan and every few demo ticks. It reads
the region's load and moves an escalation ladder one rung at a time, logging
each move with its reasons (`surge.level_changed`) and acting on entry:

    0 normal          -
    1 strained        triage on: life-safety needs x2 and vulnerable wards x1.3
                      in the solver's weights; relief stock rationed (x0.7 drawn
                      per person); stock moved from low-need to short centres
    2 mutual aid      for each capability still unmet, requests to the agencies
                      that offer it (Red Cross, volunteers, the neighbouring
                      corporation, SDRF), logged as agency_requests; each is
                      answered after its response time and either declined or
                      fulfilled - fulfilled aid becomes real units in the fleet
    3 surge shelters  schools and halls in wards that are not flooded open as
                      shelters (nearest to the fullest shelters first); buses and
                      private tankers are requested; evacuation staged by zone
    4 declaration     state/national declaration requested: NDRF and Army offers
                      are asked, but only after an officer approves (/surge/declare)

A rung is climbed only when the one below has been tried and the pressure
persists (counted over consecutive checks), and stepped down when the load has
been below the line for several checks. Nothing here bypasses the planner: aid
units and surge shelters are ordinary rows the solver and guidance already use.
"""
from __future__ import annotations

import json
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.logging import get_logger
from app.db import session as db
from app.world import clock as clocks
from app.world import events as ev

log = get_logger(__name__)

LEVELS = ("normal", "strained", "mutual_aid", "surge_shelters", "declaration")
LIFE_SAFETY = {"search_rescue", "water_rescue", "medical_transport", "medical_care", "fire_suppression"}
SPLIT_LON = 75.5
#: thresholds (assumptions; the drill page shows them)
STRAIN_UTIL, STRAIN_SHELTER = 0.80, 0.80
SURGE_SHELTER = 0.90
CLIMB_AFTER = {1: 1, 2: 2, 3: 2, 4: 3}       # consecutive checks under pressure before climbing to this level
DESCEND_AFTER = 4
RATION_FACTOR = 0.7

#: in-memory view for hot paths (solver weights, supply draw-down)
_LEVEL: dict[str, int] = {"pune": 0, "ncr": 0}
_rng = random.Random(20261010)


def _speedup() -> float:
    """Aid response times run compressed while the demo world is running
    (SURGE_DEMO_SPEEDUP, default 20x: a 25-minute response arrives in 75 s), and
    in real time otherwise."""
    import os
    try:
        from app.demo import runner as demo_runner
        if demo_runner.state.running:
            return max(1.0, float(os.getenv("SURGE_DEMO_SPEEDUP", "20")))
    except Exception:  # noqa: BLE001
        pass
    return 1.0


def region_of_lng(lng: float | None) -> str:
    return "ncr" if (lng is not None and lng >= SPLIT_LON) else "pune"


def region_of_ward(ward_id: str | None) -> str:
    return "ncr" if (ward_id or "").startswith("w-gzb") else "pune"


def level(region: str) -> int:
    return _LEVEL.get(region, 0)


def triage_factor(ward_id: str | None, capability: str, elderly_share: float | None) -> float:
    """Multiplier on a demand's weight while the region is strained or worse."""
    if level(region_of_ward(ward_id)) < 1:
        return 1.0
    f = 2.0 if capability in LIFE_SAFETY else 1.0
    if (elderly_share or 0) >= 0.10:
        f *= 1.3
    return f


def ration_factor(lng: float | None) -> float:
    return RATION_FACTOR if level(region_of_lng(lng)) >= 1 else 1.0


# ------------------------------------------------------------------ metrics --
async def metrics(region: str) -> dict[str, Any]:
    cmp = "<" if region == "pune" else ">="
    fleet = await db.fetchrow(
        f"""
        select count(*) filter (where status = 'available') available,
               count(*) filter (where status in ('assigned','en_route','on_site')) busy,
               count(*) filter (where status = 'offline') offline
          from resources where extensions.ST_X(location::extensions.geometry) {cmp} {SPLIT_LON}
        """)
    shel = await db.fetchrow(
        f"""
        select coalesce(sum(capacity), 0) cap, coalesce(sum(occupancy), 0) occ,
               count(*) filter (where status = 'full') full_sites,
               count(*) filter (where status in ('open','limited')) open_sites
          from lifelines
         where kind in ('shelter','relief_centre') and status <> 'closed' and capacity > 0
           and extensions.ST_X(location::extensions.geometry) {cmp} {SPLIT_LON}
        """)
    plan = await db.fetchrow("select uncovered from allocation_plans order by generated_at desc limit 1")
    unc = plan["uncovered"] if plan else []
    unc = json.loads(unc) if isinstance(unc, str) else (unc or [])
    unmet = [u for u in unc if region_of_ward(u.get("ward_id") or u.get("wardId")) == region]
    caps: dict[str, int] = {}
    for u in unmet:
        c = u.get("capability") or u.get("need") or u.get("capability_id") or "unknown"
        caps[c] = caps.get(c, 0) + int(u.get("shortfall") or 1)
    low = await db.fetchval(
        f"""
        select count(*) from lifelines l
         where l.kind in ('relief_centre','food_kitchen','medical_camp','water_point') and l.supplies <> '{{}}'::jsonb
           and extensions.ST_X(l.location::extensions.geometry) {cmp} {SPLIT_LON}
           and exists (select 1 from jsonb_each_text(l.supplies) s
                        where (s.value)::numeric < 0.25 * coalesce((l.supplies_baseline->>s.key)::numeric, 1))
        """)
    a, b = int(fleet["available"] or 0), int(fleet["busy"] or 0)
    cap, occ = float(shel["cap"] or 0), float(shel["occ"] or 0)
    return {
        "fleet": {"available": a, "busy": b, "offline": int(fleet["offline"] or 0),
                  "utilisation": round(b / max(a + b, 1), 3)},
        "shelters": {"capacity": int(cap), "occupancy": int(occ), "occupancy_ratio": round(occ / cap, 3) if cap else 0.0,
                     "full_sites": int(shel["full_sites"] or 0), "open_sites": int(shel["open_sites"] or 0)},
        "unmet": {"count": sum(caps.values()), "by_capability": caps,
                  "life_safety": sum(v for k, v in caps.items() if k in LIFE_SAFETY),
                  "items": unmet[:30]},
        "supplies_low_sites": int(low or 0),
    }


def _pressure(m: dict, target: int) -> list[str]:
    """Reasons the region is under pressure for climbing to `target` (empty = none)."""
    r: list[str] = []
    util, occ = m["fleet"]["utilisation"], m["shelters"]["occupancy_ratio"]
    unmet, ls = m["unmet"]["count"], m["unmet"]["life_safety"]
    if target == 1:
        if util >= STRAIN_UTIL:
            r.append(f"{util:.0%} of the fleet is busy (line {STRAIN_UTIL:.0%})")
        if occ >= STRAIN_SHELTER:
            r.append(f"shelters are {occ:.0%} full (line {STRAIN_SHELTER:.0%})")
        if unmet:
            r.append(f"{unmet} need(s) have no unit")
        if m["supplies_low_sites"]:
            r.append(f"{m['supplies_low_sites']} relief site(s) below 25% stock")
    elif target == 2:
        if unmet:
            r.append(f"{unmet} need(s) still unmet after triage ({', '.join(m['unmet']['by_capability'])})")
    elif target == 3:
        if occ >= SURGE_SHELTER:
            r.append(f"shelters {occ:.0%} full (line {SURGE_SHELTER:.0%})")
        if m["shelters"]["full_sites"] >= 2:
            r.append(f"{m['shelters']['full_sites']} shelters full")
        if unmet and target == 3:
            r.append(f"{unmet} need(s) still unmet after mutual aid was asked")
    elif target == 4:
        if ls:
            r.append(f"{ls} life-safety need(s) unmet with mutual aid and surge shelters in play")
    return r


# ---------------------------------------------------------------- evaluate --
async def evaluate(region: str = "pune", *, actor: str = "agent:surge") -> dict[str, Any]:
    st = await db.fetchrow("select level, counters, since from surge_state where region = $1", region)
    if st is None:
        return {"available": False, "reason": "surge tables missing (migration 034)"}
    lvl = int(st["level"])
    counters = st["counters"] if isinstance(st["counters"], dict) else json.loads(st["counters"] or "{}")
    m = await metrics(region)
    await resolve_due_aid(region)

    nxt = min(4, lvl + 1)
    up = _pressure(m, nxt) if lvl < 4 else []
    # at the current level, is the pressure that put us here still there?
    here = _pressure(m, lvl) if lvl >= 1 else []
    counters["up"] = counters.get("up", 0) + 1 if up else 0
    counters["calm"] = counters.get("calm", 0) + 1 if (not here and not up) else 0
    changed = None
    if up and counters["up"] >= CLIMB_AFTER.get(nxt, 2) and nxt != 4:
        changed = (lvl, nxt, up)
    elif up and nxt == 4 and counters["up"] >= CLIMB_AFTER[4]:
        # never automatic: an officer approves the declaration
        if not counters.get("declaration_asked"):
            await ev.append(clock=clocks.WALL, kind="surge.declaration_requested", actor=actor,
                            subject_type="region", subject_id=region,
                            payload={"reason": "; ".join(up), "hazardTag": "flood",
                                     "asks": "NDRF and Army requisition; state declaration"})
            counters["declaration_asked"] = True
    elif lvl > 0 and counters["calm"] >= DESCEND_AFTER:
        changed = (lvl, lvl - 1, [f"load below the lines for {counters['calm']} checks"])

    reasons = up if up else here
    if changed:
        frm, to, why = changed
        lvl = to
        counters = {"up": 0, "calm": 0}
        await ev.append(clock=clocks.WALL, kind="surge.level_changed", actor=actor, subject_type="region",
                        subject_id=region,
                        payload={"from": LEVELS[frm], "to": LEVELS[to], "level": to, "reason": "; ".join(why),
                                 "metrics": {k: v for k, v in m.items() if k != "unmet"} | {
                                     "unmet": {k: v for k, v in m["unmet"].items() if k != "items"}}})
        if to > frm:
            await _on_enter(region, to, m)
        try:
            from app.surge import operations
            await operations.on_level_change(region, frm, to, m)
        except Exception as exc:  # noqa: BLE001
            log.warning("surge_operations_failed", error=str(exc)[:200])
    await db.execute(
        "update surge_state set level = $2, reasons = $3::jsonb, metrics = $4::jsonb, counters = $5::jsonb, "
        "since = case when level <> $2 then now() else since end, updated_at = now() where region = $1",
        region, lvl, json.dumps(reasons), json.dumps(m, default=str), json.dumps(counters))
    _LEVEL[region] = lvl
    from app.surge import operations
    await operations.tick(region, lvl, m)
    return {"region": region, "level": lvl, "name": LEVELS[lvl], "reasons": reasons, "metrics": m,
            "changed": bool(changed)}


async def _on_enter(region: str, to: int, m: dict) -> None:
    if to == 1:
        await redistribute(region)
    if to == 2:
        await request_aid(region, m, min_level=2)
    if to == 3:
        await open_surge_shelters(region, m)
        await request_aid(region, m, min_level=3)


# --------------------------------------------------------------- mutual aid --
async def request_aid(region: str, m: dict, *, min_level: int, approved_by: str | None = None) -> list[dict]:
    """Ask the agencies that offer what is short. One request per offer; an offer
    already asked and not yet answered is not asked again."""
    short = dict(m["unmet"]["by_capability"])
    if min_level == 3:
        short.setdefault("mass_transport", 1)
        short.setdefault("water_supply", 1)
    if min_level == 4:
        for c in ("search_rescue", "water_rescue", "supply_delivery"):
            short.setdefault(c, 1)
    if not short:
        return []
    offers = await db.fetch(
        """
        select o.*, array_agg(kc.capability_id) caps
          from aid_offers o join resource_kind_capabilities kc on kc.kind_id = o.kind
         where o.region = $1 and o.min_level <= $2
           and not exists (select 1 from surge_aid a where a.offer_id = o.id and a.outcome is null)
         group by o.id
         order by o.response_minutes
        """, region, min_level)
    ward = await db.fetchval("select id from wards where ($1 = 'ncr') = (id like 'w-gzb%') order by id limit 1", region)
    made = []
    for o in offers:
        cover = [c for c in o["caps"] if short.get(c, 0) > 0]
        if not cover:
            continue
        cap = cover[0]
        qty = int(o["quantity"])
        rid = await db.fetchval(
            "insert into agency_requests (city_id, ward_id, from_agency, to_agency, capability_id, quantity, status, note) "
            "values ('pune', $1, $2, $3, $4, $5, 'requested', $6) returning id::text",
            ward, "pcmc" if region == "pune" else "gmc", o["agency_id"], cap, qty,
            f"Surge {LEVELS[min_level]}: {o['label']} requested by the surge ladder"
            + (f", approved by {approved_by}" if approved_by else ""))
        due = datetime.now(timezone.utc) + timedelta(minutes=int(o["response_minutes"]) / _speedup())
        await db.execute(
            "insert into surge_aid (region, offer_id, request_id, capability_id, quantity, due_at) "
            "values ($1,$2,$3::uuid,$4,$5,$6)", region, o["id"], rid, cap, qty, due)
        await ev.append(clock=clocks.WALL, kind="surge.aid_requested", actor="agent:surge", subject_type="region",
                        subject_id=region,
                        payload={"agency": o["agency_id"], "offer": o["id"], "label": o["label"], "capability": cap,
                                 "quantity": qty, "eta_minutes": int(o["response_minutes"]), "request_id": rid,
                                 "reason": f"{short[cap]} {cap.replace('_', ' ')} need(s) unmet"})
        short[cap] = max(0, short[cap] - qty)
        made.append({"offer": o["id"], "capability": cap, "quantity": qty, "due": due.isoformat()})
    return made


async def resolve_due_aid(region: str, *, now: datetime | None = None) -> list[dict]:
    """Answer requests whose response time has passed: declined, or fulfilled with
    real units staged at the agency's base (they join the next plan)."""
    now = now or datetime.now(timezone.utc)
    due = await db.fetch(
        "select a.*, o.agency_id, o.kind, o.accept_p, o.stage_lng, o.stage_lat, o.label "
        "from surge_aid a join aid_offers o on o.id = a.offer_id "
        "where a.region = $1 and a.outcome is null and a.due_at <= $2", region, now)
    out = []
    for a in due:
        ok = _rng.random() < float(a["accept_p"])
        units: list[str] = []
        if ok:
            for k in range(int(a["quantity"])):
                uid = f"AID-{a['offer_id']}-{a['id']}-{k + 1}"[:60]
                await db.execute(
                    "insert into resources (id, kind, label, operator, base_location, location, capacity, status, "
                    "city_id, agency_id, crew_available, fuel_pct, last_reported_at, updated_at, status_note) "
                    "select $1, $2, $3, $4, g, g, rk.default_capacity, 'available', 'pune', $5, true, 90, now(), now(), $6 "
                    "from (select extensions.ST_SetSRID(extensions.ST_MakePoint($7,$8),4326)::extensions.geography g) x, "
                    "resource_kinds rk where rk.id = $2 on conflict (id) do nothing",
                    uid, a["kind"], f"{a['label']} #{k + 1}", a["label"], a["agency_id"],
                    "Mutual aid (surge)", float(a["stage_lng"]) + 0.002 * k, float(a["stage_lat"]))
                units.append(uid)
        await db.execute("update surge_aid set outcome = $2, decided_at = now(), units = $3 where id = $1",
                         a["id"], "arrived" if ok else "declined", units)
        if a["request_id"]:
            await db.execute("update agency_requests set status = $2, responded_at = now(), responded_by = $3 "
                             "where id = $1::uuid and status in ('requested','acknowledged')",
                             str(a["request_id"]), "fulfilled" if ok else "declined", a["agency_id"])
        await ev.append(clock=clocks.WALL, kind="surge.aid_arrived" if ok else "surge.aid_declined",
                        actor=f"agency:{a['agency_id']}", subject_type="region", subject_id=region,
                        payload={"offer": a["offer_id"], "label": a["label"], "units": units,
                                 "capability": a["capability_id"],
                                 "reason": (f"{len(units)} unit(s) staged and in the fleet" if ok
                                            else "declined: no capacity to spare (the ladder asks the next agency)")})
        out.append({"offer": a["offer_id"], "arrived": ok, "units": units})
    if out:
        from app.demo import runner as demo_runner
        demo_runner.state.dirty = True
    return out


# ----------------------------------------------------------- surge shelters --
async def open_surge_shelters(region: str, m: dict, limit: int = 3) -> list[str]:
    """Open schools/halls as shelters: not in a ward with open flood incidents,
    nearest to the fullest shelters first."""
    if region != "pune":
        candidates_sql_extra = "and l.ward_id like 'w-gzb%'"
    else:
        candidates_sql_extra = "and l.ward_id not like 'w-gzb%'"
    rows = await db.fetch(
        f"""
        with fullest as (
          select location from lifelines
           where kind in ('shelter','relief_centre') and capacity > 0 and occupancy::float / capacity >= 0.7
           {candidates_sql_extra.replace('l.', '')}
        )
        select l.id, l.name, l.ward_id, coalesce(l.capacity, 300) cap,
               coalesce((select min(extensions.ST_Distance(l.location, f.location)) from fullest f), 1e9) d
          from lifelines l
         where l.kind = 'school' and l.status = 'closed' {candidates_sql_extra}
           and not exists (select 1 from incidents i where i.ward_id = l.ward_id and i.status <> 'resolved'
                            and i.category in ('flooded_road','waterlogging','person_stranded'))
           and not exists (select 1 from lifelines s where s.id = 'surge-' || l.id and s.status <> 'closed')
         order by d limit $1
        """, limit)
    opened = []
    for r in rows:
        sid = f"surge-{r['id']}"
        await db.execute(
            "insert into lifelines (id, kind, name, ward_id, location, capacity, occupancy, city_id, accepts_casualties, "
            "status, specialities, supplies, supplies_baseline, people_served_per_hour, occupancy_baseline, "
            "last_reported_at, opened_at) "
            "select $1, 'shelter', 'Surge shelter: ' || name, ward_id, location, $2, 0, city_id, false, 'open', '{}', "
            "'{\"blankets\":200,\"food_packets\":400,\"medical_kits\":20,\"water_litres\":3000}'::jsonb, "
            "'{\"blankets\":200,\"food_packets\":400,\"medical_kits\":20,\"water_litres\":3000}'::jsonb, null, 0, now(), now() "
            "from lifelines where id = $3 on conflict (id) do update set status = 'open', capacity = excluded.capacity, "
            "occupancy = 0, opened_at = now(), supplies = excluded.supplies", sid, int(r["cap"]), r["id"])
        await ev.append(clock=clocks.WALL, kind="surge.shelter_opened", actor="agent:surge", subject_type="lifeline",
                        subject_id=sid, ward_id=r["ward_id"],
                        payload={"name": r["name"], "capacity": int(r["cap"]),
                                 "reason": "shelters near capacity; this school's ward has no open flooding, "
                                           "so it is opened as a shelter and guidance starts sending people here"})
        opened.append(sid)
    return opened


# ----------------------------------------------------------- redistribution --
async def redistribute(region: str) -> list[dict]:
    """Move part of the stock of well-stocked, low-need centres to centres below 25%."""
    cmp = "<" if region == "pune" else ">="
    rows = await db.fetch(
        f"""
        select id, name, supplies, supplies_baseline from lifelines
         where kind in ('relief_centre','food_kitchen','medical_camp') and supplies <> '{{}}'::jsonb
           and extensions.ST_X(location::extensions.geometry) {cmp} {SPLIT_LON}
        """)
    moves = []
    for line in ("food_packets", "water_litres", "medical_kits", "blankets"):
        def frac(r):
            s, b = _j(r["supplies"]), _j(r["supplies_baseline"])
            return float(s.get(line, 0)) / max(float(b.get(line, 1) or 1), 1)
        low = [r for r in rows if line in _j(r["supplies"]) and frac(r) < 0.25]
        high = sorted([r for r in rows if line in _j(r["supplies"]) and frac(r) > 0.75], key=frac, reverse=True)
        for lo, hi in zip(low, high):
            give = int(float(_j(hi["supplies"])[line]) * 0.3)
            if give <= 0:
                continue
            await db.execute("update lifelines set supplies = jsonb_set(supplies, array[$2::text], to_jsonb((supplies->>$2::text)::numeric - $3::int)) where id = $1",
                             hi["id"], line, give)
            await db.execute("update lifelines set supplies = jsonb_set(supplies, array[$2::text], to_jsonb((supplies->>$2::text)::numeric + $3::int)) where id = $1",
                             lo["id"], line, give)
            await ev.append(clock=clocks.WALL, kind="surge.redistributed", actor="agent:surge", subject_type="lifeline",
                            subject_id=lo["id"],
                            payload={"line": line, "amount": give, "from": hi["name"], "to": lo["name"],
                                     "reason": f"{lo['name']} below 25% of {line.replace('_', ' ')}; "
                                               f"{hi['name']} above 75% with less demand"})
            moves.append({"line": line, "from": hi["id"], "to": lo["id"], "amount": give})
    return moves


def _j(v):
    return v if isinstance(v, dict) else json.loads(v or "{}")


# ------------------------------------------------------------ scarcity drill --
async def scarcity_drill(region: str, fleet_keep: float = 0.35, shelter_scale: float = 0.3) -> dict:
    """Make resources scarce so the ladder is exercised: hold most of the region's
    fleet in reserve (offline, 'held in reserve for the drill') and shrink shelter
    capacity, backing up what was there so `end_drill` restores it."""
    cmp = "<" if region == "pune" else ">="
    units = await db.fetch(
        f"select id, status::text status, status_note, unavailable_reason from resources "
        f"where status = 'available' and id not like 'AID-%' "
        f"and extensions.ST_X(location::extensions.geometry) {cmp} {SPLIT_LON} order by id")
    rng = random.Random(7)
    hold = [u for u in units if rng.random() > fleet_keep]
    for u in hold:
        await db.execute("insert into surge_drill (region, subject, subject_id, before) values ($1,'resource',$2,$3::jsonb)",
                         region, u["id"], json.dumps(dict(u)))
        await db.execute("update resources set status = 'offline', unavailable_reason = 'held in reserve (scarcity drill)', "
                         "status_note = 'scarcity drill' where id = $1", u["id"])
    shel = await db.fetch(
        f"select id, capacity, occupancy from lifelines where kind in ('shelter','relief_centre') and capacity > 0 "
        f"and extensions.ST_X(location::extensions.geometry) {cmp} {SPLIT_LON}")
    for s in shel:
        await db.execute("insert into surge_drill (region, subject, subject_id, before) values ($1,'lifeline',$2,$3::jsonb)",
                         region, s["id"], json.dumps({"capacity": s["capacity"], "occupancy": s["occupancy"]}))
        newcap = max(20, int(s["capacity"] * shelter_scale))
        await db.execute("update lifelines set capacity = $2, occupancy = least(occupancy, $2) where id = $1", s["id"], newcap)
    await ev.append(clock=clocks.WALL, kind="surge.drill_started", actor="officer", subject_type="region", subject_id=region,
                    payload={"held_units": len(hold), "kept_units": len(units) - len(hold),
                             "shelter_scale": shelter_scale,
                             "reason": f"scarcity drill: {len(hold)} of {len(units)} units held in reserve, "
                                       f"shelters cut to {shelter_scale:.0%} of capacity"})
    from app.demo import runner as demo_runner
    demo_runner.state.dirty = True
    return {"held": len(hold), "kept": len(units) - len(hold), "shelters_scaled": len(shel)}


async def end_drill(region: str) -> dict:
    rows = await db.fetch("select id, subject, subject_id, before from surge_drill where region = $1 and restored_at is null",
                          region)
    for r in rows:
        b = _j(r["before"])
        if r["subject"] == "resource":
            await db.execute("update resources set status = 'available', unavailable_reason = null, status_note = null "
                             "where id = $1 and status = 'offline'", r["subject_id"])
        else:
            await db.execute("update lifelines set capacity = $2 where id = $1", r["subject_id"], b.get("capacity"))
        await db.execute("update surge_drill set restored_at = now() where id = $1", r["id"])
    await ev.append(clock=clocks.WALL, kind="surge.drill_ended", actor="officer", subject_type="region", subject_id=region,
                    payload={"restored": len(rows), "reason": "scarcity drill ended; reserve units and shelter capacity restored"})
    return {"restored": len(rows)}


async def overview(region: str) -> dict:
    st = await db.fetchrow("select * from surge_state where region = $1", region)
    if st is None:
        return {"available": False, "reason": "run migration 034"}
    aid = await db.fetch(
        "select a.id, a.offer_id, o.label, o.agency_id, ag.name agency, a.capability_id, a.quantity, a.requested_at, "
        "a.due_at, a.outcome, a.decided_at, a.units from surge_aid a join aid_offers o on o.id = a.offer_id "
        "join agencies ag on ag.id = o.agency_id where a.region = $1 order by a.requested_at desc limit 50", region)
    shelters = await db.fetch(
        "select id, name, capacity, occupancy, status, opened_at from lifelines where id like 'surge-%' "
        "and ($1 = 'ncr') = (ward_id like 'w-gzb%') order by opened_at desc", region)
    log_rows = await db.fetch(
        "select id, occurred_at, kind, actor, payload from events where kind like 'surge.%' "
        "and (subject_id = $1 or subject_type = 'lifeline') order by id desc limit 60", region)
    offers = await db.fetch("select o.*, ag.name agency from aid_offers o join agencies ag on ag.id = o.agency_id "
                            "where o.region = $1 order by o.min_level, o.response_minutes", region)
    drill = await db.fetchval("select count(*) from surge_drill where region = $1 and restored_at is null", region)
    iso = lambda v: v.isoformat() if hasattr(v, "isoformat") else v  # noqa: E731
    from app.surge import operations
    try:
        ops = await operations.overview(region)
    except Exception as exc:  # noqa: BLE001
        ops = {"available": False, "reason": str(exc)[:160]}
    return {
        "operations": ops,
        "available": True, "region": region, "level": st["level"], "name": LEVELS[st["level"]], "levels": LEVELS,
        "reasons": _j(st["reasons"]) if not isinstance(st["reasons"], list) else st["reasons"],
        "metrics": _j(st["metrics"]), "since": iso(st["since"]), "updatedAt": iso(st["updated_at"]),
        "drillActive": bool(drill),
        "thresholds": {"strainedUtilisation": STRAIN_UTIL, "strainedShelter": STRAIN_SHELTER,
                       "surgeShelter": SURGE_SHELTER, "climbAfterChecks": CLIMB_AFTER, "descendAfterChecks": DESCEND_AFTER,
                       "rationFactor": RATION_FACTOR},
        "aid": [{k: iso(v) for k, v in dict(a).items()} for a in aid],
        "surgeShelters": [{k: iso(v) for k, v in dict(s).items()} for s in shelters],
        "offers": [{k: iso(v) for k, v in dict(o).items()} for o in offers],
        "log": [{"id": r["id"], "at": iso(r["occurred_at"]), "kind": r["kind"], "actor": r["actor"],
                 "payload": _j(r["payload"])} for r in log_rows],
    }


async def refresh_levels() -> None:
    try:
        for r in await db.fetch("select region, level from surge_state"):
            _LEVEL[r["region"]] = int(r["level"])
    except Exception:  # noqa: BLE001 - table not there yet
        pass


_last_eval = 0.0


async def maybe_evaluate(min_interval_s: float = 20.0) -> None:
    """Called after replans and from the demo tick; cheap, never raises."""
    global _last_eval
    if time.time() - _last_eval < min_interval_s:
        return
    _last_eval = time.time()
    for region in ("pune", "ncr"):
        try:
            await evaluate(region)
        except Exception as exc:  # noqa: BLE001
            log.warning("surge_evaluate_failed", region=region, error=str(exc)[:200])
            return


async def reset_world() -> None:
    """Demo world reset: the ladder back to normal, pending aid cancelled, aid
    units returned, drills restored, everything the surge opened closed."""
    try:
        for region in ("pune", "ncr"):
            if await db.fetchval("select count(*) from surge_drill where region = $1 and restored_at is null", region):
                await end_drill(region)
        await db.execute("update surge_state set level = 0, reasons = '[]', counters = '{}', since = now(), "
                         "updated_at = now()")
        await db.execute("update surge_aid set outcome = 'cancelled', decided_at = now() where outcome is null")
        await db.execute("update resources set status = 'offline', unavailable_reason = 'returned to home agency', "
                         "status_note = 'demobilised' where id like 'AID-%'")
        for k in _LEVEL:
            _LEVEL[k] = 0
        from app.surge import operations
        await operations.reset()
    except Exception as exc:  # noqa: BLE001 - surge tables may not exist
        log.warning("surge_reset_failed", error=str(exc)[:200])
