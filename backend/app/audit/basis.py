"""Audit trail: for any agent decision, everything it was based on.

`decisions()` lists what the agents did (planner, triage, fusion, evidence sweep, drone link, satellite, policy
gate, guardrails, commander). `basis(event_id)` assembles, for one decision, the actual material behind it:

* the decision itself (what, who, when, the reasoning it recorded) and its causal chain back to the root event
* the incident(s) it touched, the fused belief **at the time** (last `incident.belief_changed` before it) and now
* every report with its photo, source, trust and verification state
* edge-camera frames and clips near the place around that time, with the VLM's words
* drone observations (situation reports, plans, needs) and map-matched drone frames
* satellite passes (Copernicus GFM / Sentinel-1): the extracted pixel values and the actual images
* LoRa sensor readings near the place around that time, channel by channel
* the units assigned, their status, ETA and road route, and the planner's stated reason

Media are listed as metadata (`mediaId`, mime, what sensor and processing level); the bytes are served by
`/audit/media/{id}` so a page loads fast and only fetches what is opened. Read only.
"""
from __future__ import annotations

import math
from datetime import timedelta
from typing import Any

from app.db import session as db

#: Kinds that are an agent deciding or concluding something (actor agent:* is included as well).
DECISION_KINDS = (
    "plan.generated", "assignment.created", "assignment.changed", "assignment.cancelled", "assignment.rerouted",
    "incident.opened", "report.linked", "report.rejected", "report.held", "incident.belief_changed", "evidence.swept",
    "satellite.observed", "drone.plan", "field.drone_need", "field.drone_situation", "drone.request_sent",
    "drone.orchestrated", "agent.graph_sector", "guardrail.tripped", "decision.proposed", "decision.approved",
    "decision.rejected", "decision.executed", "agency.requested", "agency.replied", "agency.escalated",
    "resource.status_changed", "camera.frame",
)
GROUPS = {
    "planner": ("plan.generated", "assignment.%"),
    "triage": ("incident.opened", "report.%"),
    "belief": ("incident.belief_changed", "evidence.swept"),
    "satellite": ("satellite.%",),
    "drones": ("drone.%", "field.drone_%"),
    "cameras": ("camera.%",),
    "gate": ("decision.%", "guardrail.%"),
    "agencies": ("agency.%",),
}
NEAR_M = 600


def _m(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    k = 111_320.0
    return math.hypot((lng2 - lng1) * k * math.cos(math.radians((lat1 + lat2) / 2)), (lat2 - lat1) * k)


def _row(r: Any) -> dict:
    d = dict(r)
    for k, v in list(d.items()):
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
    return d


def _like_any(col: str, pats: tuple[str, ...]) -> str:
    return "(" + " or ".join(f"{col} like '{p}'" if "%" in p else f"{col} = '{p}'" for p in pats) + ")"


async def decisions(*, city_id: str = "pune", group: str | None = None, incident_id: str | None = None,
                    q: str | None = None, before: int | None = None, limit: int = 80) -> list[dict]:
    where = ["sim_run_id is null", "city_id = $1",
             f"(actor like 'agent:%' or kind = any($2::text[]))", "kind <> 'agent.step'"]
    args: list[Any] = [city_id, list(DECISION_KINDS)]
    if group in GROUPS:
        where.append(_like_any("kind", GROUPS[group]))
    if incident_id:
        args.append(incident_id)
        where.append(f"(subject_id = ${len(args)} or payload->>'incidentId' = ${len(args)} or payload->>'incident_id' = ${len(args)}"
                     f" or subject_id like ${len(args)} || ':%')")
    if q:
        args.append(f"%{q.lower()}%")
        where.append(f"(lower(kind) like ${len(args)} or lower(actor) like ${len(args)} or lower(payload::text) like ${len(args)})")
    if before:
        args.append(before)
        where.append(f"id < ${len(args)}")
    args.append(max(1, min(limit, 300)))
    rows = await db.fetch(
        f"""select id, occurred_at, kind, actor, subject_type, subject_id, ward_id, payload, causation_id
              from events where {' and '.join(where)} order by id desc limit ${len(args)}""", *args)
    return [_summarise(_row(r)) for r in rows]


def _summarise(e: dict) -> dict:
    p = e.get("payload") if isinstance(e.get("payload"), dict) else {}
    text = (p.get("reason") or p.get("summary") or p.get("text") or p.get("rationale") or p.get("why")
            or p.get("caption") or p.get("trigger") or "")
    if e["kind"] == "incident.belief_changed":
        text = f"Belief {p.get('before', 0):.0%} → {p.get('p', 0):.0%} ({p.get('status')}). " + " ".join(p.get("reasons") or [])[:200]
    has_media = bool(p.get("mediaId") or p.get("clipMediaId") or p.get("images"))
    return {**e, "title": e["kind"].replace("_", " ").replace(".", " · "), "text": str(text)[:300],
            "agent": (e.get("actor") or "").replace("agent:", ""), "hasMedia": has_media,
            "incidentId": _incident_of(e)}


def _incident_of(e: dict) -> str | None:
    p = e.get("payload") if isinstance(e.get("payload"), dict) else {}
    for c in (p.get("incidentId"), p.get("incident_id"), e.get("subject_id") if e.get("subject_type") == "incident" else None):
        if c and len(str(c)) == 36:
            return str(c)
    sid = str(e.get("subject_id") or "")
    if e.get("subject_type") == "assignment" and len(sid) >= 36 and sid[:36].count("-") == 4:
        return sid[:36]
    return None


async def basis(event_id: int) -> dict | None:
    row = await db.fetchrow(
        "select id, occurred_at, recorded_at, kind, actor, subject_type, subject_id, ward_id, city_id, payload, causation_id "
        "from events where id = $1", event_id)
    if row is None:
        return None
    e = _row(row)
    from app.world import events as ev
    chain = [_summarise(_row(c)) for c in await ev.chain(event_id)]
    effects = [_summarise(_row(r)) for r in await db.fetch(
        "select id, occurred_at, kind, actor, subject_type, subject_id, ward_id, payload, causation_id from events "
        "where causation_id = $1 order by id limit 40", event_id)]
    incidents = []
    for c in [e] + chain + effects:
        iid = _incident_of(c)
        if iid and iid not in incidents:
            incidents.append(iid)
    at = row["occurred_at"]
    out = {"decision": _summarise(e), "chain": chain, "effects": effects, "incidents": []}
    for iid in incidents[:3]:
        out["incidents"].append(await _incident_basis(iid, at, event_id))
    out["media"] = [m for inc in out["incidents"] for m in inc.get("media", [])]
    p = e.get("payload") if isinstance(e.get("payload"), dict) else {}
    out["media"] += [m for m in _payload_media(e["kind"], p, e.get("occurred_at")) if m not in out["media"]]
    return out


def _payload_media(kind: str, p: dict, at: str | None) -> list[dict]:
    media = []
    if kind == "camera.frame":
        if p.get("mediaId"):
            media.append({"mediaId": p["mediaId"], "type": "image", "sensor": "Edge-AI camera frame (RGB)",
                          "level": "raw frame + VLM description", "caption": p.get("caption"), "at": at,
                          "extracted": {"hazard": p.get("kind"), "alert": p.get("alertLevel"), "hazards": p.get("hazards")}})
        if p.get("clipMediaId"):
            media.append({"mediaId": p["clipMediaId"], "type": "video", "sensor": "Edge-AI camera clip",
                          "level": "seconds before the alert", "caption": p.get("caption"), "at": at})
    if kind == "satellite.observed":
        media += _sat_media(p, at)
    return media


def _sat_media(p: dict, at: str | None) -> list[dict]:
    return [{"mediaId": im.get("mediaId"), "url": None if im.get("mediaId") else im.get("url"), "type": "image",
             "sensor": im.get("label"), "level": im.get("level"), "caption": im.get("explain"),
             "at": p.get("acquired_at") or at, "extracted": p.get("values"), "source": p.get("source")}
            for im in p.get("images") or []]


async def _incident_basis(iid: str, at: Any, event_id: int) -> dict:
    inc = await db.fetchrow(
        """
        select i.id::text id, i.title, i.category, i.status::text status, i.severity, i.confidence, i.report_count,
               i.ward_id, i.street, i.created_at,
               extensions.ST_X(i.location::extensions.geometry) lng, extensions.ST_Y(i.location::extensions.geometry) lat
          from incidents i where i.id = $1::uuid
        """, iid)
    if inc is None:
        return {"id": iid, "missing": True, "media": []}
    inc = _row(inc)
    lng, lat = inc["lng"], inc["lat"]
    lo, hi = at - timedelta(hours=3), at + timedelta(minutes=30)
    media: list[dict] = []

    # belief then and now
    then = await db.fetchrow(
        "select id, occurred_at, payload from events where kind = 'incident.belief_changed' and subject_id = $1 and id <= $2 "
        "order by id desc limit 1", iid, event_id)
    from app.incidents import fusion
    _, now_b = await fusion.assess(iid)
    belief = {"atDecision": _row(then) if then else None, "now": now_b.as_dict() if now_b else None}

    # reports and their photos
    reps = [_row(r) for r in await db.fetch(
        """
        select r.id::text id, r.source, r.reporter_name, r.reporter_key, r.note, r.created_at, r.trust_score,
               r.verification_status, r.outcome, r.photo_path, r.photo_evidence, r.classification_confidence
          from citizen_reports r where r.incident_id = $1::uuid order by r.created_at limit 60
        """, iid)]
    for r in reps:
        r["modality"] = fusion.modality_of(r.get("source"), r.get("reporter_key"))
        path = r.pop("photo_path", None) or ""
        if path.startswith(("http", "data:image")):
            media.append({"url": path, "type": "image", "sensor": f"Photo with {r['modality']} report",
                          "level": "as sent", "caption": r.get("note"), "at": r.get("created_at"), "reportId": r["id"]})
    rep_ids = [r["id"] for r in reps]
    for m in await db.fetch(
            "select id, source, mime, caption, created_at, event_ref from feed_media where report_id = any($1::uuid[]) order by id limit 30",
            rep_ids):
        media.append({"mediaId": m["id"], "type": "video" if m["mime"].startswith("video") else "image",
                      "sensor": {"camera": "Edge-AI camera frame", "citizen": "Citizen photo", "drone": "Drone frame",
                                 "crew": "Crew photo"}.get(m["source"], m["source"]),
                      "level": m["mime"], "caption": m["caption"], "at": m["created_at"].isoformat()})

    # edge cameras near it around that time
    frames = []
    if lng is not None:
        for f in await db.fetch(
                "select id, subject_id, occurred_at, payload from events where kind = 'camera.frame' and occurred_at between $1 and $2 "
                "order by id desc limit 300", lo, hi):
            p = f["payload"] if isinstance(f["payload"], dict) else {}
            if p.get("lat") is None:
                continue
            d = _m(lng, lat, float(p["lon"]), float(p["lat"]))
            if d <= NEAR_M:
                frames.append({"eventId": f["id"], "camera": f["subject_id"], "at": f["occurred_at"].isoformat(), "distance_m": int(d),
                               "caption": p.get("caption"), "hazard": p.get("kind"), "alert": p.get("alertLevel"),
                               "demoPosition": p.get("demoPosition"), "mediaId": p.get("mediaId"), "clipMediaId": p.get("clipMediaId")})
                media += _payload_media("camera.frame", p, f["occurred_at"].isoformat())
        frames = frames[:30]

    # drones
    drone_events = [_summarise(_row(r)) for r in await db.fetch(
        """
        select id, occurred_at, kind, actor, subject_type, subject_id, ward_id, payload, causation_id from events
         where subject_id = $1 and (kind like 'drone.%' or kind like 'field.drone_%') and occurred_at <= $2
         order by id desc limit 25
        """, iid, hi)]
    localized = []
    if lng is not None:
        for r in await db.fetch("select id, occurred_at, payload from events where kind = 'drone.localized' and occurred_at between $1 and $2 "
                                "order by id desc limit 50", lo, hi):
            p = r["payload"] if isinstance(r["payload"], dict) else {}
            if p.get("lat") is not None and _m(lng, lat, float(p.get("lon") or p.get("lng") or 0), float(p["lat"])) <= NEAR_M:
                localized.append({"eventId": r["id"], "at": r["occurred_at"].isoformat(), **{k: p.get(k) for k in ("drone", "lat", "lon", "place", "inliers", "processingMs")}})

    # satellite
    sat = []
    for r in await db.fetch("select id, occurred_at, payload from events where kind = 'satellite.observed' and subject_id = $1 "
                            "order by id desc limit 3", iid):
        p = r["payload"] if isinstance(r["payload"], dict) else {}
        sat.append({"eventId": r["id"], "at": r["occurred_at"].isoformat(), **{k: v for k, v in p.items() if k != "images"},
                    "images": _sat_media(p, r["occurred_at"].isoformat())})
        media += _sat_media(p, r["occurred_at"].isoformat())

    # LoRa readings near it around that time
    readings = []
    if lng is not None:
        nodes = [n for n in await db.fetch("select id, label, lat, lon, simulated from sensor_nodes where lat is not null")
                 if _m(lng, lat, float(n["lon"]), float(n["lat"])) <= NEAR_M]
        if nodes:
            rows = await db.fetch(
                """
                select node_id, observed_at, mq2, mq135, temp_c, tilt_deg, gyro_dps, vib_g, mic, piezo, pir,
                       human, structural, environmental, overall, flags
                  from sensor_readings where node_id = any($1::text[]) and observed_at between $2 and $3
                 order by observed_at desc limit 60
                """, [n["id"] for n in nodes], at - timedelta(minutes=30), hi)
            meta = {n["id"]: {"label": n["label"], "simulated": n["simulated"],
                              "distance_m": int(_m(lng, lat, float(n["lon"]), float(n["lat"])))} for n in nodes}
            readings = [{**_row(r), **meta.get(r["node_id"], {})} for r in rows]

    # units, routes and the planner's reasons
    units = [_row(r) for r in await db.fetch(
        """
        select a.id::text assignment_id, a.resource_id, r.kind, r.label, a.status::text status, a.eta_minutes, a.progress,
               a.created_at, a.route_engine, extensions.ST_AsGeoJSON(a.route::extensions.geometry)::json route
          from assignments a join resources r on r.id = a.resource_id
         where a.incident_id = $1::uuid and a.sim_run_id is null order by a.created_at desc limit 12
        """, iid)]
    reasons = {}
    for r in await db.fetch("select subject_id, payload from events where kind = 'assignment.created' and subject_id like $1 || '%' "
                            "order by id desc limit 20", iid):
        p = r["payload"] if isinstance(r["payload"], dict) else {}
        if p.get("resource_id"):
            reasons.setdefault(p["resource_id"], p.get("reason"))
    for u in units:
        u["reason"] = reasons.get(u["resource_id"])
    needs = [_row(r) for r in await db.fetch("select capability_id, required, met from incident_needs where incident_id = $1::uuid", iid)]
    sweep = await db.fetchrow("select id, occurred_at, payload from events where kind = 'evidence.swept' and subject_id = $1 "
                              "and id <= $2 order by id desc limit 1", iid, event_id)
    return {**inc, "belief": belief, "reports": reps, "cameras": frames, "drones": {"events": drone_events, "localized": localized},
            "satellite": sat, "readings": readings, "units": units, "needs": needs,
            "sweep": _row(sweep) if sweep else None, "media": media}


async def media_bytes(media_id: int) -> tuple[bytes, str] | None:
    row = await db.fetchrow("select bytes, mime from feed_media where id = $1", media_id)
    return (bytes(row["bytes"]), row["mime"]) if row else None
