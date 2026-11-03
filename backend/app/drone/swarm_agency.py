"""DisruptionOps -> drone swarm: ask the swarm (an external agency) for aerial help and ingest what it reports.

From the drone_swarm repo integration kit (swarm_ops/integrations/disruptionops_patch), installed into DisruptionOps.
Extended by app/drone/swarm_link.py (filtered log, panel tasking, recheck-and-replan reasoner).
Apply with migration 040_drone_swarm_agency.sql (registers the 'drone-swarm' agency and the capabilities
aerial_assessment / supply_drop / relay as data) and the two-line hook in main.py described in README.md.

What it does
------------
1. Every SCAN_S seconds it looks for open incidents that want aerial help:
     * the incident needs ``field_assessment`` (an ``unknown_report``)            -> swarm request kind ``assess``
     * its category is ``life_safety`` and nobody has confirmed it from the air   -> ``sos``
     * it needs ``supply_delivery`` (a relief point running out)                  -> ``supply``
   and sends one ``agency_request`` over the swarm's WebSocket (``/ops/ws``, protocol disruptionops.swarm.v2,
   integration token) with the situation: category, people, vulnerable people, hazards, location (lng/lat and
   the swarm's local metres), ward, and the ground units that are free right now.
   The request is also recorded with the existing inter-agency workflow (``reports.request_agency``) so the
   console shows it, and every transition is an event (actor ``sensor:drone-swarm``).
2. It reads the swarm's status events (accepted / declined / briefing / resource_request / supply_delivered /
   completed ...) from the same socket and writes them back ONLY through doors that already exist:
     * ``world.events.append``                 an audit event ``drone.<status>`` on the incident
     * ``agency_replies.record``               the swarm's ack becomes the agency's reply (accept_full / delayed /
                                              decline_capacity / decline_jurisdiction), not the random REPLY_SET
     * ``incidents.intake.receive(source='sensor')``  for a swarm ``resource_request`` the swarm saw on scene
       (trust, dedup, needs-from-words and the allocator take it from there; the CP-SAT planner still picks units)
   It never writes tables directly, never resolves an incident (a person does that) and never assigns a unit.
3. It stands down when a person wants it to:
     * ``autonomy.paused()`` (emergency stop, control_state key 'autonomy'): no new request is sent and no
       swarm resource_request is filed; the swarm's reports are still logged as events.
     * an active ``operator_overrides`` row on the incident, or a human having declined / cancelled the
       agency request: no new request, and a swarm resource_request is logged, not filed.

Environment (all optional; ``SWARM_AGENCY=1`` switches it on):
    SWARM_AGENCY             1 to start with the app (default 0)
    SWARM_WS_URL             wss://<swarm-host>/ops/ws            (the swarm's OPS WebSocket)
    SWARM_INTEGRATION_TOKEN  the swarm's OPS_INTEGRATION_TOKEN (never logged)
    SWARM_ANCHOR             "lat,lon" of the swarm map origin   (default 18.6526632,73.7624378, PCCOE)
    SWARM_SCAN_S             scan period seconds (default 10)
    SWARM_ORCHESTRATE        1 to also push planner assignments to the swarm (orchestrate_incident)
    SWARM_MAX_OPEN           most unanswered requests at once (default 6)

Everything here imports ``app.*`` lazily, so the pure helpers can be unit-tested without a DisruptionOps checkout.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

ANCHOR = (18.6526632, 73.7624378)          # lat, lon; same as the swarm engine and tools/build_agency_catalog.py
KM_LAT, KM_LNG = 110.57, 105.6              # the swarm's local-metre projection
ACTOR = "sensor:drone-swarm"
SCAN_S = float(os.environ.get("SWARM_SCAN_S", "10"))
MAX_OPEN = int(os.environ.get("SWARM_MAX_OPEN", "6"))
PROTOCOL = "disruptionops.swarm.v2"

#: swarm status -> (event kind we record, agency reply kind or None). Reply kinds are DisruptionOps' own
#: (ops/agency_replies.py REPLY_SET): the swarm is one of the agencies, so its answer replaces the random reply.
STATUS_MAP = {
    "accepted": ("drone.accepted", "accept_full"),
    "declined": ("drone.declined", None),            # decline kind comes from the reason, see reply_from_ack()
    "launched": ("drone.launched", None),
    "on_scene": ("drone.on_scene", None),
    "briefing": ("drone.briefing", None),
    "resource_request": ("drone.resource_request", None),
    "resource_assigned": ("drone.resource_assigned", None),
    "supply_delivered": ("drone.supply_delivered", None),
    "ground_arrived": ("drone.ground_arrived", None),
    "retasked": ("drone.retasked", None),
    "recalled": ("drone.recalled", None),
    "handed_over": ("drone.handed_over", None),
    "completed": ("drone.completed", None),
}
REPLY_KINDS = {"accept_full", "accept_partial", "delayed", "need_info", "decline_capacity", "decline_jurisdiction"}
#: statuses that turn an earlier "delayed" (queued) reply into a firm acceptance
FIRM_STATUSES = {"launched", "on_scene", "briefing", "supply_delivered", "ground_arrived", "handed_over", "completed"}
#: swarm needed_kind -> words intake's EXTRA_NEEDS (incidents/intake.py:321) turns into a need, or None
KIND_WORDS = {"ambulance": "injured person, needs an ambulance", "rescue": "people trapped, needs a rescue team",
              "bus": None, "boat": None}
#: swarm needed_kind -> capability for an agency request when no intake words exist
KIND_CAPABILITY = {"fire_engine": "fire_suppression", "boat": "water_rescue", "bus": "mass_transport",
                   "supply_truck": "supply_delivery", "pump": "dewatering", "engineering": "debris_clearance"}
VULNERABLE_WORDS = ("elderly", "old man", "old woman", "child", "children", "baby", "infant", "pregnant", "wheelchair",
                    "disabled", "bedridden", "patient", "वृद्ध", "बच्च", "गर्भवती")
CLOSED_AGENCY_STATES = {"declined", "cancelled", "fulfilled"}


try:  # orjson parses the swarm's 0.5 MB snapshots several times faster than json
    import orjson as _orjson

    def _loads(raw):
        return _orjson.loads(raw)
except ImportError:  # pragma: no cover
    def _loads(raw):
        return json.loads(raw)


# ------------------------------------------------------------------ geometry --
def to_local(lng: float, lat: float, anchor: tuple[float, float] = ANCHOR) -> list[float]:
    return [round((lng - anchor[1]) * KM_LNG * 1000, 1), round((lat - anchor[0]) * KM_LAT * 1000, 1)]


def to_geo(x: float, y: float, anchor: tuple[float, float] = ANCHOR) -> tuple[float, float]:
    return round(anchor[1] + x / (KM_LNG * 1000), 7), round(anchor[0] + y / (KM_LAT * 1000), 7)


def parse_anchor(raw: str | None) -> tuple[float, float]:
    try:
        lat, lon = (float(v) for v in (raw or "").split(","))
        return lat, lon
    except ValueError:
        return ANCHOR


# ------------------------------------------------------------ pure decisions --
def request_kind(category: str, needs: dict[str, int], life_safety: bool) -> str | None:
    """Which swarm request, if any, an incident warrants. Data driven: the needs come from the taxonomy."""
    if "field_assessment" in needs:
        return "assess"
    if needs.get("supply_delivery"):
        return "supply"
    if life_safety:
        return "sos"
    return None


def request_kind_for(c: dict) -> str | None:
    """request_kind plus: anything severity 4+ gets a drone to look, so the plan is built on what is really there."""
    return request_kind(c["category"], c["needs"], c["life_safety"]) or ("sos" if int(c.get("severity") or 0) >= 4 else None)


def vulnerable_count(notes: str, ward_elderly_share: float | None, people: int) -> int:
    t = (notes or "").lower()
    named = any(w in t for w in VULNERABLE_WORDS)
    if named:
        return max(1, min(people, math.ceil(people / 2)))
    return 0 if not ward_elderly_share or ward_elderly_share < 0.14 else 1


def build_payload(ctx: dict, anchor: tuple[float, float] = ANCHOR) -> dict:
    """The agency_request payload (swarm engine.command 'agency_request') plus a rich ``situation`` block.

    The swarm ignores keys it does not know (it reads request_id, kind, position, category, people, needs, priority,
    requested_by, sim_run_id), so ``situation`` is carried for the swarm's briefing and for audit.
    """
    lng, lat = ctx["lng"], ctx["lat"]
    people = max(1, min(int(ctx.get("people") or 1), 200))
    kind = ctx["kind"]
    return {
        "request_id": f"dops-{ctx['incident_id']}-{kind}"[:128],
        "kind": kind,
        "position": to_local(lng, lat, anchor),
        "category": ctx["category"],
        "people": people,
        # The swarm validates needs against first_aid, medical, food, assessment, rescue; omitted = its own default.
        **({"needs": ctx["swarm_needs"]} if ctx.get("swarm_needs") else
           {"needs": ["food", "first_aid"]} if kind == "supply" else {"needs": ["assessment"]} if kind == "assess" else {}),
        "priority": max(1, min(int(ctx.get("severity") or 3), 5)),
        "requested_by": f"disruptionops:{ctx['incident_id']}",
        "situation": {
            "incident_id": ctx["incident_id"], "title": ctx.get("title"), "category": ctx["category"],
            "severity": ctx.get("severity"), "life_safety": bool(ctx.get("life_safety")),
            "people": people, "vulnerable": ctx.get("vulnerable", 0), "hazards": ctx.get("hazards", []),
            "location": {"lng": lng, "lat": lat, "ward_id": ctx.get("ward_id"), "ward": ctx.get("ward_name")},
            "needs": ctx.get("needs", {}),
            "available_ground_units": ctx.get("ground_units", []),
            "report_count": ctx.get("report_count"),
        },
    }


def reply_from_ack(result: dict, ok: bool = True) -> tuple[str, str]:
    """Swarm ack -> (agency reply kind, text). Uses result['reply_kind'] when the swarm provides one."""
    reason = str(result.get("reason") or "")
    drones = result.get("assigned_drones") or []
    given = result.get("reply_kind")
    if given in REPLY_KINDS:
        kind = given
    elif not ok or not result.get("accepted"):
        kind = "decline_jurisdiction" if "operating area" in reason or "no-fly" in reason else "decline_capacity"
    elif "queued" in reason:
        kind = "delayed"
    else:
        kind = "accept_full"
    eta = result.get("eta_s")
    text = f"Drone swarm: {reason or kind}."
    if drones:
        text += f" Aircraft {', '.join(map(str, drones))}" + (f", ETA {round(eta)} s." if eta else ".")
    return kind, text[:400]


def classify_event(event: dict) -> dict | None:
    """One swarm status event -> what to record. Ignores anything without a machine-readable status."""
    status = event.get("status")
    if status not in STATUS_MAP:
        return None
    kind, action = STATUS_MAP[status]
    data = event.get("data") or {}
    return {"swarm_event_id": str(event.get("id")), "status": status, "kind": kind, "reply_kind": action,
            "request_id": event.get("request_id") or data.get("request_id"), "swarm_incident_id": event.get("incident_id"),
            "message": event.get("message", ""), "data": data, "time": event.get("time")}


# ------------------------------------------------------------------ the ports --
class AppPorts:
    """The DisruptionOps doors, imported lazily. Tests substitute this class."""

    def __init__(self, city_id: str = "pune"):
        self.city_id = city_id

    async def candidates(self) -> list[dict]:
        from app.db import session as db
        from app.taxonomy import cache as taxonomy
        rows = await db.fetch(
            """
            select i.id::text as id, i.title, i.category, i.hazard, i.ward_id, i.severity, i.report_count,
                   extensions.ST_X(i.location::extensions.geometry) as lng, extensions.ST_Y(i.location::extensions.geometry) as lat,
                   w.name as ward_name, w.elderly_share,
                   coalesce((select jsonb_object_agg(n.capability_id, n.required) from incident_needs n
                              where n.incident_id = i.id), '{}'::jsonb) as needs,
                   coalesce((select string_agg(r.note, ' | ') from citizen_reports r where r.incident_id = i.id), '') as notes
              from incidents i left join wards w on w.id = i.ward_id
             where i.status <> 'resolved' and i.city_id = $1
             order by i.severity desc, i.created_at limit 100
            """, self.city_id)
        out = []
        for r in rows:
            needs = r["needs"] if isinstance(r["needs"], dict) else json.loads(r["needs"] or "{}")
            ref = taxonomy.categories.get(r["category"])
            out.append({"incident_id": r["id"], "title": r["title"], "category": r["category"], "severity": r["severity"],
                        "lng": r["lng"], "lat": r["lat"], "ward_id": r["ward_id"], "ward_name": r["ward_name"],
                        "needs": needs, "report_count": r["report_count"], "life_safety": bool(ref and ref.life_safety),
                        "hazards": [r["hazard"]] if r["hazard"] else [], "notes": r["notes"],
                        "elderly_share": float(r["elderly_share"]) if r["elderly_share"] is not None else None})
        return out

    async def ground_units(self, needs: dict[str, int], lng: float, lat: float) -> list[dict]:
        """Free units that could serve this incident's needs, nearest first (read only; the planner decides)."""
        from app.db import session as db
        from app.taxonomy import cache as taxonomy
        kinds = sorted({k for cap in needs for k in taxonomy.kinds_providing(cap)})
        if not kinds:
            return []
        rows = await db.fetch(
            """
            select id, kind, label, extensions.ST_Distance(location, extensions.ST_SetSRID(extensions.ST_MakePoint($2,$3),4326)::extensions.geography) as m
              from resources where status = 'available' and kind = any($1::text[]) and city_id = $4 order by m limit 8
            """, kinds, lng, lat, self.city_id)
        return [{"id": r["id"], "kind": r["kind"], "label": r["label"], "distance_m": int(r["m"])} for r in rows]

    def autonomy_paused(self) -> bool:
        from app.ops import autonomy
        return autonomy.paused()

    async def officer_control(self, incident_id: str) -> bool:
        """True when an officer has an active pin / hold / forbid on this incident (operator_overrides)."""
        from app.db import session as db
        row = await db.fetchrow(
            "select 1 from operator_overrides where incident_id = $1::uuid and active and (expires_at is null or expires_at > now()) limit 1",
            incident_id)
        return row is not None

    async def sent_before(self, incident_id: str, kind: str) -> bool:
        from app.db import session as db
        row = await db.fetchrow(
            "select 1 from events where kind = 'drone.request_sent' and subject_id = $1 and payload->>'request_kind' = $2 limit 1",
            incident_id, kind)
        return row is not None

    async def record(self, kind: str, incident_id: str | None, ward_id: str | None, payload: dict) -> None:
        from app.world import events as ev
        from app.world.clock import WALL
        await ev.append(clock=WALL, kind=kind, actor=ACTOR, subject_type="incident" if incident_id else "swarm",
                        subject_id=str(incident_id or "drone-swarm"), city_id=self.city_id, ward_id=ward_id, payload=payload)

    def _principal(self):
        from app.core.security import Principal, Role
        return Principal(user_id=None, role=Role.ADMIN, full_name="Drone swarm bridge")

    async def open_agency_request(self, incident_id: str | None, ward_id: str | None, capability: str, quantity: int, note: str) -> str | None:
        from app.api.v1 import reports
        from app.db import session as db
        out = await reports.request_agency(
            reports.AgencyRequestIn(incident_id=incident_id, ward_id=ward_id, to_agency="drone-swarm"
                                    if capability in ("aerial_assessment", "supply_drop", "relay") else "pmc",
                                    capability_id=capability, quantity=quantity, note=note[:500]),
            self._principal())
        if capability in ("aerial_assessment", "supply_drop", "relay"):
            # request_agency() schedules the random REPLY_SET answer; the swarm answers over its own socket instead.
            await db.execute("update agency_requests set reply_due_at = null where id = $1::uuid", out.id)
        return out.id

    async def record_reply(self, agency_request_id: str, kind: str, text: str) -> str | None:
        """The swarm's answer, through DisruptionOps' own door (ops/agency_replies.record), then stop the random replier.

        Needs patches/agency_replies_external_agencies.diff so 'drone-swarm' is an external agency (accepting stages
        no AID- units and is not demoted to a decline).
        """
        from app.db import session as db
        from app.ops import agency_replies
        out = await agency_replies.record(agency_request_id, kind, text, by="Drone swarm")
        await db.execute("update agency_requests set reply_due_at = null where id = $1::uuid", agency_request_id)
        return out.get("status")

    async def planned_assignments(self) -> list[dict]:
        """Incidents with live planner assignments (ground units the CP-SAT planner chose), for orchestrated mode."""
        from app.db import session as db
        rows = await db.fetch(
            """
            select i.id::text as incident_id, i.category, i.ward_id, i.report_count, i.severity,
                   extensions.ST_X(i.location::extensions.geometry) as lng, extensions.ST_Y(i.location::extensions.geometry) as lat,
                   r.id as rid, r.kind, r.agency_id, a.eta_minutes,
                   extensions.ST_X(r.location::extensions.geometry) as rlng, extensions.ST_Y(r.location::extensions.geometry) as rlat
              from assignments a join incidents i on i.id = a.incident_id join resources r on r.id = a.resource_id
             where a.status::text not in ('complete','cancelled') and i.status <> 'resolved' and i.city_id = $1
            """, self.city_id)
        by: dict[str, dict] = {}
        for r in rows:
            d = by.setdefault(r["incident_id"], {"incident_id": r["incident_id"], "category": r["category"], "ward_id": r["ward_id"],
                                                 "people": r["report_count"], "severity": r["severity"], "lng": r["lng"],
                                                 "lat": r["lat"], "resources": []})
            d["resources"].append({"id": r["rid"], "kind": r["kind"], "agency": r["agency_id"], "lng": r["rlng"], "lat": r["rlat"],
                                   "eta_s": int((r["eta_minutes"] or 0) * 60) or None})
        return list(by.values())

    async def ground_state(self, anchor: tuple[float, float], bounds: list[float]) -> list[dict]:
        """DisruptionOps' real units that matter on the swarm's map: inside it (plus a margin) or assigned to an
        incident inside it, with their live position, active assignment, ETA, progress and road route."""
        from app.db import session as db
        reach = math.hypot(max(abs(bounds[0]), abs(bounds[2])), max(abs(bounds[1]), abs(bounds[3]))) + 300
        rows = await db.fetch(
            """
            with a as (
              select distinct on (resource_id) resource_id, id::text assignment_id, incident_id::text incident_id,
                     status::text assignment_status, eta_minutes, progress,
                     extensions.ST_AsGeoJSON(route::extensions.geometry) route
                from assignments
               where sim_run_id is null and status::text in ('proposed','approved','en_route','on_site')
               order by resource_id, created_at desc)
            select r.id, r.kind, r.label, r.agency_id, r.status::text status,
                   extensions.ST_X(r.location::extensions.geometry) lng, extensions.ST_Y(r.location::extensions.geometry) lat,
                   a.assignment_id, a.incident_id, a.assignment_status, a.eta_minutes, a.progress, a.route,
                   i.status::text incident_status
              from resources r
              left join a on a.resource_id = r.id
              left join incidents i on i.id::text = a.incident_id
             where r.city_id = $1 and r.location is not null
               and (extensions.ST_DWithin(r.location, extensions.ST_SetSRID(extensions.ST_MakePoint($2,$3),4326)::extensions.geography, $4)
                    or (i.id is not null and extensions.ST_DWithin(i.location,
                          extensions.ST_SetSRID(extensions.ST_MakePoint($2,$3),4326)::extensions.geography, $4)))
             order by (a.incident_id is null), r.id limit 60
            """, self.city_id, anchor[1], anchor[0], reach)
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["route"] = (json.loads(d["route"]) or {}).get("coordinates") if d.get("route") else []
            except (TypeError, ValueError):
                d["route"] = []
            out.append(d)
        return out

    async def add_needs(self, incident_id: str, caps: dict[str, int]) -> dict[str, int]:
        """A drone saw a need on scene: make sure the incident asks the planner for it (needs only grow)."""
        from app.db import session as db
        known = {r["id"] for r in await db.fetch("select id from capabilities where id = any($1::text[])", list(caps))}
        caps = {c: q for c, q in caps.items() if c in known}
        if caps:
            async with db.transaction() as conn:
                await conn.executemany(
                    """
                    insert into incident_needs (incident_id, capability_id, required, updated_at)
                    values ($1::uuid, $2, $3, now())
                    on conflict (incident_id, capability_id)
                    do update set required = greatest(incident_needs.required, excluded.required), updated_at = now()
                    """, [(incident_id, c, q) for c, q in caps.items()])
        return caps

    async def agency_request_status(self, agency_request_id: str) -> str | None:
        from app.db import session as db
        row = await db.fetchrow("select status from agency_requests where id = $1::uuid", agency_request_id)
        return row["status"] if row else None

    async def file_report(self, *, ward_id: str, category: str, lng: float, lat: float, note: str) -> dict:
        from app.incidents import intake
        res = await intake.receive(ward_id=ward_id, category=category, location=(lng, lat), note=note, source="sensor",
                                   reporter_key="device:sensor:drone-swarm", reporter_name="Drone swarm",
                                   device_id="drone-swarm", city_id=self.city_id)
        return {"incident_id": res.incident_id, "created": res.created_incident, "linked": res.linked}


# ----------------------------------------------------------------- the agency --
@dataclass
class SwarmAgency:
    url: str
    token: str = field(repr=False)
    ports: Any = None
    anchor: tuple[float, float] = ANCHOR
    connect: Callable | None = None                 # injectable for tests
    connected: bool = False
    last_error: str = ""
    sent: dict = field(default_factory=dict)        # request_id -> {incident_id, ward_id, agency_request_id, category, lng, lat, kind}
    seen: set = field(default_factory=set)          # swarm event ids already ingested
    held: int = 0                                   # requests withheld by emergency stop / officer control
    stats: dict = field(default_factory=lambda: {"sent": 0, "events": 0, "filed": 0, "logged_only": 0})
    _ws: Any = None
    _pending: dict = field(default_factory=dict)
    _primed: bool = False
    orchestrate: bool = False
    orchestrate_disabled: bool = False
    orchestrated: dict = field(default_factory=dict)
    _done: set = field(default_factory=set)

    def __post_init__(self):
        self.ports = self.ports or AppPorts()

    # --- sending -----------------------------------------------------------------
    async def command(self, action: str, payload: dict, timeout: float = 8.0) -> dict:
        if self._ws is None:
            raise ConnectionError("swarm socket not connected")
        cid = f"dops-{uuid.uuid4().hex[:16]}"
        fut = asyncio.get_running_loop().create_future()
        self._pending[cid] = fut
        await self._ws.send(json.dumps({"type": "command", "id": cid, "action": action, "payload": payload}))
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(cid, None)

    async def scan_once(self) -> int:
        """Send requests for incidents that warrant aerial help. Returns how many were sent."""
        if self.ports.autonomy_paused():
            self.held += 1
            return 0
        unanswered = sum(1 for r in self.sent.values() if not r.get("answered"))
        count = 0
        for c in await self.ports.candidates():
            kind = request_kind_for(c)
            if not kind or unanswered + count >= MAX_OPEN:
                continue
            rid = f"dops-{c['incident_id']}-{kind}"[:128]
            if rid in self.sent or await self.ports.sent_before(c["incident_id"], kind):
                continue
            if await self.ports.officer_control(c["incident_id"]):
                self.held += 1
                continue
            people = max(1, c.get("report_count") or 1)
            ctx = {**c, "kind": kind, "people": people,
                   "vulnerable": vulnerable_count(c.get("notes", ""), c.get("elderly_share"), people),
                   "ground_units": await self.ports.ground_units(c["needs"], c["lng"], c["lat"])}
            payload = build_payload(ctx, self.anchor)
            capability = {"assess": "aerial_assessment", "sos": "aerial_assessment", "supply": "supply_drop"}[kind]
            self.sent[rid] = {"incident_id": c["incident_id"], "ward_id": c["ward_id"], "agency_request_id": None, "ready": False,
                              "category": c["category"], "lng": c["lng"], "lat": c["lat"], "kind": kind}
            try:
                ack = await self.command("agency_request", payload)
            except (ConnectionError, asyncio.TimeoutError) as exc:
                self.sent.pop(rid, None)       # not delivered: the next scan retries
                self.last_error = f"send failed: {type(exc).__name__}"
                break
            arid = await self.ports.open_agency_request(c["incident_id"], c["ward_id"], capability, 1,
                                                        f"{kind}: {c['title']} (swarm request {rid})")
            self.sent[rid].update(agency_request_id=arid, ready=True)
            await self.ports.record("drone.request_sent", c["incident_id"], c["ward_id"],
                                    {"request_id": rid, "request_kind": kind, "agency_request_id": arid,
                                     "situation": payload["situation"]})
            self.stats["sent"] += 1
            count += 1
            await self.handle_ack(rid, ack)
            await asyncio.sleep(0.15)          # the swarm closes sockets that exceed 10 commands/s
        return count

    async def handle_ack(self, rid: str, ack: dict) -> None:
        info = self.sent.get(rid)
        if not info:
            return
        result = ack.get("result") or {}
        if not ack.get("ok"):
            result = {"accepted": False, "reason": ack.get("error", "rejected")}
        status = "accepted" if result.get("accepted") else "declined"
        info["answered"] = True
        info["swarm_incident_id"] = result.get("incident_id")
        kind, text = reply_from_ack(result, ok=bool(ack.get("ok")))
        await self._ingest({"swarm_event_id": f"ack:{rid}", "status": status, "kind": STATUS_MAP[status][0],
                            "reply_kind": kind, "request_id": rid, "swarm_incident_id": result.get("incident_id"),
                            "message": text, "data": {k: result.get(k) for k in ("reason", "eta_s", "assigned_drones", "plan", "merged")},
                            "time": None})

    # --- orchestrated mode ---------------------------------------------------------
    async def orchestrate_once(self) -> int:
        """DisruptionOps' planner chose ground units for an incident: tell the swarm so it shows (and briefs) them.

        Only incidents are sent (held, unclassified reports never are). Re-sent only when the assignment set changes.
        Disables itself if the swarm does not know the action.
        """
        if self.orchestrate_disabled or self.ports.autonomy_paused():
            return 0
        sent = 0
        for d in await self.ports.planned_assignments():
            if d["category"] == "unknown_report":
                continue
            signature = json.dumps(sorted((r["id"], r.get("eta_s")) for r in d["resources"]))
            if self.orchestrated.get(d["incident_id"]) == signature:
                continue
            ref = f"dops-orch-{d['incident_id']}"[:128]
            drones = [{"count": 1, "role": "supply" if d["category"] == "supply_shortage" else "assess", "payload": []}]
            payload = {
                "request_id": ref, "external_id": d["incident_id"], "position": to_local(d["lng"], d["lat"], self.anchor),
                "category": d["category"], "people": max(1, d.get("people") or 1),
                "situation": {"severity": d.get("severity"), "ward_id": d.get("ward_id")},
                "assignments": {"drones": drones, "resources": [
                    {"id": r["id"], "kind": r["kind"], "agency": r.get("agency"),
                     "position": to_local(r["lng"], r["lat"], self.anchor) if r.get("lng") is not None else None,
                     "eta_s": r.get("eta_s")} for r in d["resources"]]}}
            try:
                ack = await self.command("orchestrate_incident", payload)
            except (ConnectionError, asyncio.TimeoutError) as exc:
                self.last_error = f"orchestrate send failed: {type(exc).__name__}"
                break
            if not ack.get("ok"):
                self.last_error = f"orchestrate rejected: {ack.get('error')}"
                if "Unknown action" in str(ack.get("error")):
                    self.orchestrate_disabled = True
                    break
                continue
            self.orchestrated[d["incident_id"]] = signature
            sent += 1
            await self.ports.record("drone.orchestrated", d["incident_id"], d.get("ward_id"),
                                    {"request_id": ref, "resources": [r["id"] for r in d["resources"]]})
            await asyncio.sleep(0.15)
        return sent

    # --- ingesting ---------------------------------------------------------------
    async def on_snapshot(self, packet: dict) -> None:
        events = ((packet.get("state") or {}).get("events")) or []
        if not self._primed:                       # first frame after (re)connect: remember history, ingest only new
            self.seen.update(str(e.get("id")) for e in events)
            self._primed = True
            return
        for event in events:
            if str(event.get("id")) in self.seen:
                continue
            self.seen.add(str(event.get("id")))
            action = classify_event(event)
            if action and action["request_id"] in self.sent:
                if not self.sent[action["request_id"]].get("ready"):
                    self.seen.discard(str(event.get("id")))      # request not yet on record: pick this up next frame
                    continue
                await self._ingest(action)
        if len(self.seen) > 5000:
            self.seen = set(list(self.seen)[-2500:])

    async def _ingest(self, ev: dict) -> None:
        info = self.sent.get(ev["request_id"])
        if info is None:
            return
        if ev["swarm_event_id"] in self._done:
            return
        self._done.add(ev["swarm_event_id"])
        self.stats["events"] += 1
        payload = {"request_id": ev["request_id"], "swarm_event_id": ev["swarm_event_id"], "status": ev["status"],
                   "message": ev["message"], "data": ev["data"], "simulation_only": True}
        await self.ports.record(ev["kind"], info["incident_id"], info["ward_id"], payload)
        reply = ev.get("reply_kind")
        if not reply and info.get("reply") == "delayed" and ev["status"] in FIRM_STATUSES:
            reply, ev["message"] = "accept_full", "Drone swarm: aircraft airborne; the queued request is now firm."
        final = ("accept_full", "decline_capacity", "decline_jurisdiction")
        if reply and info.get("agency_request_id") and info.get("reply") != reply and info.get("reply") not in final:
            info["reply"] = reply
            await self.ports.record_reply(info["agency_request_id"], reply, ev["message"])
        if ev["status"] == "resource_request":
            await self._resource_request(ev, info)

    async def _resource_request(self, ev: dict, info: dict) -> None:
        """The swarm saw a need on scene. File it through intake unless a person or the emergency stop says no."""
        data = ev["data"]
        needed = str(data.get("needed_kind", ""))
        paused = self.ports.autonomy_paused()
        officer = await self.ports.officer_control(info["incident_id"])
        agency_status = await self.ports.agency_request_status(info["agency_request_id"]) if info.get("agency_request_id") else None
        if paused or officer or agency_status in ("declined", "cancelled"):
            self.stats["logged_only"] += 1
            await self.ports.record("drone.resource_request_held", info["incident_id"], info["ward_id"],
                                    {"needed_kind": needed, "qty": data.get("qty"), "paused": paused, "officer_control": officer,
                                     "agency_request_status": agency_status})
            return
        words = KIND_WORDS.get(needed)
        if words and info.get("ward_id"):
            note = f"Drone swarm on scene: {words}. {data.get('rationale', '')}".strip()[:400]
            out = await self.ports.file_report(ward_id=info["ward_id"], category=info["category"], lng=info["lng"],
                                               lat=info["lat"], note=note)
            self.stats["filed"] += 1
            await self.ports.record("drone.resource_request_filed", info["incident_id"], info["ward_id"],
                                    {"needed_kind": needed, "intake": out})
            return
        capability = KIND_CAPABILITY.get(needed)
        if capability:
            arid = await self.ports.open_agency_request(
                info["incident_id"], info["ward_id"], capability, int(data.get("qty") or 1),
                f"Drone swarm asks for {data.get('qty', 1)}x {needed}: {data.get('rationale', '')}")
            await self.ports.record("drone.resource_request_filed", info["incident_id"], info["ward_id"],
                                    {"needed_kind": needed, "agency_request_id": arid})

    # --- connection loop ---------------------------------------------------------
    async def _reader(self, ws) -> None:
        """Read the socket as fast as it sends; never let snapshots delay an ack.

        The swarm streams ~10 snapshots a second of ~0.5 MB each. Parsing every one starved the API process on a
        small instance (HTTP requests timed out, command acks waited behind snapshots). Acks are parsed at once;
        snapshots are only stashed (newest wins; a frame carrying the world is always kept) and a separate task
        processes at most SWARM_SNAPSHOT_HZ of them. Messages and events are windows in every snapshot, so a
        skipped frame loses nothing that the next processed one does not carry.
        """
        self._snap_latest = None
        self._snap_world = None
        self._snap_wake = asyncio.Event()
        worker = asyncio.create_task(self._snapshot_loop())
        try:
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    continue
                head = raw[:48]
                if '"type":"snapshot"' in head or '"type": "snapshot"' in head:
                    if '"world":{' in raw or '"world": {' in raw:
                        self._snap_world = raw
                    self._snap_latest = raw
                    self._snap_wake.set()
                    self.stats["snapshots_in"] = self.stats.get("snapshots_in", 0) + 1
                    continue
                try:
                    packet = _loads(raw)
                except ValueError:
                    continue
                if packet.get("type") == "ack":
                    fut = self._pending.get(packet.get("id"))
                    if fut and not fut.done():
                        fut.set_result(packet)
                elif packet.get("type") == "snapshot":
                    await self.on_snapshot(packet)
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await worker

    async def _snapshot_loop(self) -> None:
        gap = 1.0 / max(0.2, float(os.environ.get("SWARM_SNAPSHOT_HZ", "2")))
        while True:
            await self._snap_wake.wait()
            self._snap_wake.clear()
            if self._snap_world is not None:       # a frame with the world (bounds, anchor) is never skipped
                raw, self._snap_world = self._snap_world, None
                if self._snap_latest is raw:
                    self._snap_latest = None
                elif self._snap_latest is not None:
                    self._snap_wake.set()          # the newest plain frame is processed next
            else:
                raw, self._snap_latest = self._snap_latest, None
            if raw is None:
                continue
            try:
                packet = _loads(raw)
                await self.on_snapshot(packet)
                self.stats["snapshots_used"] = self.stats.get("snapshots_used", 0) + 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad frame never stops the link
                self.last_error = f"snapshot: {type(exc).__name__}: {str(exc)[:120]}"
            await asyncio.sleep(gap)

    async def run(self) -> None:
        from urllib.parse import quote
        backoff = 1.0
        while True:
            try:
                connect = self.connect
                if connect is None:
                    from websockets.asyncio.client import connect
                async with connect(f"{self.url}?token={quote(self.token)}", max_size=4_000_000) as ws:
                    self._ws, self.connected, self._primed, backoff = ws, True, False, 1.0
                    reader = asyncio.create_task(self._reader(ws))
                    try:
                        while not reader.done():
                            try:
                                await self.scan_once()
                                if self.orchestrate:
                                    await self.orchestrate_once()
                            except Exception as exc:    # noqa: BLE001 - one bad incident must not drop the socket
                                self.last_error = f"scan: {type(exc).__name__}: {str(exc)[:120]}"
                            await asyncio.sleep(SCAN_S)
                    finally:
                        reader.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:    # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
            finally:
                self._ws, self.connected = None, False
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 2)

    def status(self) -> dict:
        return {"enabled": True, "connected": self.connected, "protocol": PROTOCOL, "open_requests": sum(1 for r in self.sent.values() if not r.get("answered")),
                "sent_total": self.stats["sent"], "events_ingested": self.stats["events"], "reports_filed": self.stats["filed"],
                "logged_only": self.stats["logged_only"], "held_by_stop_or_officer": self.held, "last_error": self.last_error}


# ------------------------------------------------------------------ app wiring --
_agency: SwarmAgency | None = None
_task: asyncio.Task | None = None


def enabled() -> bool:
    return os.environ.get("SWARM_AGENCY", "0").lower() in ("1", "true", "yes", "on") and bool(os.environ.get("SWARM_WS_URL"))


async def start() -> None:
    """Call from app.main lifespan next to drone_swarm.start()."""
    global _agency, _task
    if not enabled() or _task:
        return
    _agency = SwarmAgency(url=os.environ["SWARM_WS_URL"], token=os.environ.get("SWARM_INTEGRATION_TOKEN", ""),
                          anchor=parse_anchor(os.environ.get("SWARM_ANCHOR")),
                          orchestrate=os.environ.get("SWARM_ORCHESTRATE", "0").lower() in ("1", "true", "yes", "on"))
    _task = asyncio.create_task(_agency.run())


async def stop() -> None:
    global _task
    if _task:
        _task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _task
        _task = None


def status() -> dict:
    return _agency.status() if _agency else {"enabled": False}
