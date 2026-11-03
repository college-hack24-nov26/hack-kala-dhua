"""Area evidence sweep: everything the edge network has seen around one incident.

A report says "people trapped at X". This module answers "what do our edge
devices know about X right now?" by gathering, within a radius and a time
window around the incident:

* **cameras**      edge-AI camera nodes (VLM laptops, CCTV with a detector) and
                   their latest scene descriptions, with the frame they sent;
* **reports**      citizen / mesh / sensor reports, with their photos;
* **drones**       the drone swarm's incidents nearby, the aircraft on them and
                   their latest situation report; map-matched drone frames;
* **sensors**      LoRa field nodes nearby and their last reading;
* **units**        ground units already assigned to the incident.

Read only. It never dispatches anything: the agent graph's ``aerial`` node and
the planner act on the incident; this is the picture an officer (and the event
log) gets of what backs it. ``on_incident`` writes the result as one
``evidence.swept`` event so the trail is replayable.
"""
from __future__ import annotations

import asyncio
import math
import time
from typing import Any

from app.core.logging import get_logger
from app.db import session as db

log = get_logger(__name__)

RADIUS_M = 600
WINDOW_H = 3
_last_sweep: dict[str, float] = {}
SWEEP_COOLDOWN_S = 120.0
#: Automatic sweeps run one at a time, each holding at most one pooled connection, so a burst of reports
#: (the demo runner files them constantly) can never starve the event router or the HTTP requests.
MAX_PENDING = 12
_gate: asyncio.Semaphore | None = None
_pending = 0


def _m(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    k = 111_320.0
    return math.hypot((lng2 - lng1) * k * math.cos(math.radians((lat1 + lat2) / 2)), (lat2 - lat1) * k)


async def _safe(coro, default):
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001 - one missing source never hides the others
        log.warning("evidence_source_failed", error=f"{type(exc).__name__}: {str(exc)[:160]}")
        return default


async def _incident(incident_id: str) -> dict | None:
    row = await db.fetchrow(
        """
        select i.id::text id, i.title, i.category, i.severity, i.status::text status, i.ward_id, i.city_id,
               i.created_at, extensions.ST_X(i.location::extensions.geometry) lng,
               extensions.ST_Y(i.location::extensions.geometry) lat
          from incidents i where i.id = $1::uuid
        """, incident_id)
    return dict(row) if row else None


async def _reports(inc: dict, radius: float, hours: float) -> list[dict]:
    rows = await db.fetch(
        """
        select r.id::text id, r.incident_id::text incident_id, r.source, r.category, r.note, r.reporter_name,
               r.reporter_key, r.device_id, r.created_at, r.trust_score, r.verification_status,
               r.photo_path, r.photo_evidence,
               extensions.ST_X(r.location::extensions.geometry) lng, extensions.ST_Y(r.location::extensions.geometry) lat,
               extensions.ST_Distance(r.location, extensions.ST_SetSRID(extensions.ST_MakePoint($2,$3),4326)::extensions.geography) m
          from citizen_reports r
         where (r.incident_id = $1::uuid
                or (r.sim_run_id is null and r.created_at > now() - make_interval(hours => $5::int)
                    and extensions.ST_DWithin(r.location,
                          extensions.ST_SetSRID(extensions.ST_MakePoint($2,$3),4326)::extensions.geography, $4)))
         order by r.created_at desc limit 40
        """, inc["id"], inc["lng"], inc["lat"], radius, int(math.ceil(hours)))
    out = []
    for r in rows:
        path = r["photo_path"] or ""
        key = r["reporter_key"] or ""
        out.append({
            "id": r["id"], "linked": r["incident_id"] == inc["id"], "source": r["source"], "category": r["category"],
            "note": (r["note"] or "")[:400], "reporter": r["reporter_name"],
            "edge": "camera" if key.startswith("device:sensor:") and "drone" not in key else
                    "drone" if "drone" in key else "mesh" if key.startswith("device:mesh:") else "person",
            "at": r["created_at"].isoformat() if r["created_at"] else None,
            "trust": float(r["trust_score"]) if r["trust_score"] is not None else None,
            "verification": r["verification_status"], "distance_m": int(r["m"] or 0),
            "photo_url": path if path.startswith(("http://", "https://", "data:image")) else None,
            "lng": r["lng"], "lat": r["lat"],
        })
    return out


async def _cameras(inc: dict, radius: float, hours: float) -> list[dict]:
    nodes = await db.fetch(
        """
        select id, kind, label, lat, lon, last_seen, meta from mesh_nodes
         where kind in ('camera','drone') and lat is not null and lon is not null
           and last_seen > now() - make_interval(hours => $1::int)
        """, int(math.ceil(hours)))
    near = []
    for n in nodes:
        d = _m(inc["lng"], inc["lat"], float(n["lon"]), float(n["lat"]))
        if d <= radius:
            near.append({"id": n["id"], "kind": n["kind"], "label": n["label"], "lat": n["lat"], "lon": n["lon"],
                         "last_seen": n["last_seen"].isoformat() if n["last_seen"] else None, "distance_m": int(d)})
    if not near:
        return []
    ids = [n["id"] for n in near]
    frames = await db.fetch(
        """
        select distinct on (subject_id) subject_id, id, occurred_at, payload from events
         where kind = 'camera.frame' and subject_id = any($1::text[])
           and occurred_at > now() - make_interval(hours => $2::int)
         order by subject_id, id desc
        """, ids, int(math.ceil(hours)))
    by = {f["subject_id"]: f for f in frames}
    for n in near:
        f = by.get(n["id"])
        if f:
            p = f["payload"] if isinstance(f["payload"], dict) else {}
            n["latest"] = {"event_id": f["id"], "at": f["occurred_at"].isoformat() if f["occurred_at"] else None,
                           "caption": p.get("caption"), "alert": p.get("alertLevel"), "hazards": p.get("hazards"),
                           "kind": p.get("kind"), "media_id": p.get("mediaId"), "demo_position": p.get("demoPosition")}
    return sorted(near, key=lambda n: n["distance_m"])


async def _media(report_ids: list[str], media_ids: list[int]) -> list[dict]:
    if not report_ids and not media_ids:
        return []
    import base64
    rows = await db.fetch(
        """
        select id, source, mime, bytes, caption, created_at, report_id::text report_id from feed_media
         where (report_id = any($1::uuid[])) or id = any($2::bigint[])
         order by id desc limit 8
        """, report_ids or [], media_ids or [])
    return [{"id": r["id"], "source": r["source"], "caption": r["caption"], "report_id": r["report_id"],
             "at": r["created_at"].isoformat() if r["created_at"] else None,
             "url": f"data:{r['mime']};base64," + base64.b64encode(bytes(r["bytes"])).decode()} for r in rows]


def _drones(inc: dict, radius: float) -> dict:
    from app.drone import service as drone_service
    from app.drone import swarm_link
    out: dict[str, Any] = {"linked": False, "incidents": [], "localized": []}
    link = swarm_link.link()
    if link is not None and link.connected:
        out["linked"] = True
        v = link.view()
        for i in v.get("incidents") or []:
            if i.get("lng") is None or i.get("status") == "resolved":
                continue
            d = _m(inc["lng"], inc["lat"], i["lng"], i["lat"])
            if d <= radius or i.get("dops_incident_id") == inc["id"]:
                out["incidents"].append({"swarm_incident_id": i["id"], "title": i.get("title"), "status": i.get("status"),
                                         "drones": i.get("drones"), "units": i.get("units"), "distance_m": int(d),
                                         "report": i.get("report"), "same_incident": i.get("dops_incident_id") == inc["id"]})
    for e in list(getattr(drone_service, "_recent", []))[:30]:
        if e.get("accepted") and e.get("lat") is not None:
            d = _m(inc["lng"], inc["lat"], e["lon"], e["lat"])
            if d <= radius:
                out["localized"].append({"drone": e.get("drone"), "at": e.get("at"), "distance_m": int(d),
                                         "place": e.get("place"), "inliers": e.get("inliers")})
    return out


async def _sensors(inc: dict, radius: float) -> list[dict]:
    rows = await db.fetch(
        "select id, label, lat, lon, last_seen, simulated, state from sensor_nodes "
        "where lat is not null and lon is not null and last_seen > now() - interval '1 hour'")
    out = []
    for r in rows:
        d = _m(inc["lng"], inc["lat"], float(r["lon"]), float(r["lat"]))
        if d <= radius:
            st = r["state"] if isinstance(r["state"], dict) else {}
            out.append({"id": r["id"], "label": r["label"], "distance_m": int(d), "simulated": r["simulated"],
                        "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
                        "overall": st.get("overall"), "flags": st.get("flags")})
    return sorted(out, key=lambda s: s["distance_m"])


async def _units(inc: dict) -> list[dict]:
    rows = await db.fetch(
        """
        select r.id, r.kind, r.label, a.status::text status, a.eta_minutes, a.progress
          from assignments a join resources r on r.id = a.resource_id
         where a.incident_id = $1::uuid and a.sim_run_id is null
           and a.status::text not in ('complete','cancelled')
        """, inc["id"])
    return [{"id": r["id"], "kind": r["kind"], "label": r["label"], "status": r["status"],
             "eta_minutes": r["eta_minutes"], "progress": float(r["progress"] or 0)} for r in rows]


def summarise(ev: dict) -> str:
    cams = ev["cameras"]
    briefs = sum(1 for c in cams if c.get("latest"))
    reps = ev["reports"]
    photos = sum(1 for r in reps if r.get("photo_url")) + len(ev["media"])
    dr = ev["drones"]
    on = sum(len(i.get("drones") or []) for i in dr["incidents"])
    parts = [f"{len(cams)} edge camera(s) within {ev['radius_m']} m ({briefs} with a recent VLM brief)",
             f"{len(reps)} report(s) ({sum(1 for r in reps if r['linked'])} on this incident, {photos} image(s))",
             (f"{on} drone(s) on {len(dr['incidents'])} swarm task(s) nearby" if dr["linked"] else "drone swarm not linked"),
             f"{len(ev['sensors'])} field sensor(s)", f"{len(ev['units'])} ground unit(s) assigned"]
    return "; ".join(parts)


async def sweep(incident_id: str, *, radius_m: float = RADIUS_M, hours: float = WINDOW_H) -> dict | None:
    inc = await _incident(incident_id)
    if inc is None or inc.get("lng") is None:
        return None
    # Sequential on purpose: one connection at a time, however many sweeps are waiting.
    reports = await _safe(_reports(inc, radius_m, hours), [])
    cameras = await _safe(_cameras(inc, radius_m, hours), [])
    sensors = await _safe(_sensors(inc, radius_m), [])
    units = await _safe(_units(inc), [])
    media_ids = [int(c["latest"]["media_id"]) for c in cameras if c.get("latest") and c["latest"].get("media_id")]
    media = await _safe(_media([r["id"] for r in reports], media_ids), [])
    try:
        drones = _drones(inc, radius_m)
    except Exception:  # noqa: BLE001
        drones = {"linked": False, "incidents": [], "localized": []}
    out = {"incident": {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in inc.items()},
           "radius_m": int(radius_m), "window_h": hours, "cameras": cameras, "reports": reports, "media": media,
           "drones": drones, "sensors": sensors, "units": units, "swept_at": time.time()}
    out["summary"] = summarise(out)
    return out


async def on_incident(incident_id: str, *, trigger: str, caused_by: int | None = None, city_id: str = "pune") -> None:
    """Sweep and log, at most once per SWEEP_COOLDOWN_S per incident, one sweep at a time. Never raises."""
    global _gate, _pending
    now = time.monotonic()
    if now - _last_sweep.get(incident_id, 0.0) < SWEEP_COOLDOWN_S:
        return
    if _pending >= MAX_PENDING:
        return                       # backlog full: this incident is swept on its next change
    _last_sweep[incident_id] = now
    _gate = _gate or asyncio.Semaphore(1)
    _pending += 1
    try:
        async with _gate:
            await _sweep_and_log(incident_id, trigger=trigger, caused_by=caused_by, city_id=city_id)
    finally:
        _pending -= 1


async def _sweep_and_log(incident_id: str, *, trigger: str, caused_by: int | None, city_id: str) -> None:
    if len(_last_sweep) > 2000:
        for k in sorted(_last_sweep, key=_last_sweep.get)[:1000]:
            _last_sweep.pop(k, None)
    try:
        ev = await sweep(incident_id)
        if ev is None:
            return
        from app.world import events
        from app.world.clock import WALL
        await events.append(
            clock=WALL, kind="evidence.swept", actor="agent:evidence", subject_type="incident", subject_id=incident_id,
            city_id=city_id, ward_id=ev["incident"].get("ward_id"), caused_by=caused_by,
            payload={"trigger": trigger, "summary": ev["summary"], "radius_m": ev["radius_m"],
                     "cameras": [{"id": c["id"], "distance_m": c["distance_m"], "caption": (c.get("latest") or {}).get("caption")}
                                 for c in ev["cameras"]][:8],
                     "reports": len(ev["reports"]), "images": len(ev["media"]) + sum(1 for r in ev["reports"] if r.get("photo_url")),
                     "drones": [{"swarm_incident_id": i["swarm_incident_id"], "drones": i.get("drones")} for i in ev["drones"]["incidents"]][:6],
                     "sensors": len(ev["sensors"]), "units": [u["id"] for u in ev["units"]]})
    except Exception as exc:  # noqa: BLE001
        log.warning("evidence_sweep_failed", incident=incident_id, error=f"{type(exc).__name__}: {str(exc)[:160]}")
