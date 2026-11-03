"""Listener for the 12-drone rescue swarm's live feed, wired into routing.

The swarm (PyBullet physics, decisions by TypeSafe Jev) runs on a teammate's
laptop and broadcasts its state over a WebSocket (see docs/DRONE_SWARM.md).
This module is a read-only client of that feed. It sends nothing back.

The simulation flies a 32 x 25 m district in local metres. It is placed on the
real map by an **anchor** (lat/lon of the district centre) and a **scale**
(real metres per sim metre). The anchor is, in order: what an officer set, the
DRONE_SWARM_ANCHOR env var, the newest open life-safety incident in the region
(the swarm "was sent there"), or the riskiest ward. It stays fixed for the
process, so restarted runs land on the same streets and the intake's own
dedup links repeat sightings instead of multiplying incidents.

What the feed changes, all through doors that already exist:

  survivor located  ->  intake.receive(source='sensor')    an incident; trust,
                        dedup, the Commander and the allocator take it from
                        there, so a ground unit is routed to the survivor
  obstacle detour   ->  road_blocks (when the drone was over a street)
                        what the router, the allocator and citizen guidance
                        avoid; cleared automatically after BLOCK_TTL_MIN
  payload landed    ->  event on the survivor's incident (supplies are in;
                        extraction still needs the ground unit)
  drone positions   ->  mesh_nodes kind 'drone', on the Mesh & devices map

Environment:
    DRONE_SWARM             1 (default) / 0
    DRONE_SWARM_URL         wss://<tunnel>/ws (the tunnel host changes; also
                            settable at runtime via POST /drone/swarm)
    DRONE_SWARM_ANCHOR      "lat,lon" of the district centre (optional)
    DRONE_SWARM_SCALE       real metres per sim metre (default 8)
    DRONE_SWARM_REGION      pune | ncr (default pune), used to pick an anchor
    DRONE_SWARM_CATEGORY    incident category for a survivor (default person_stranded)
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)

DEFAULT_URL = "wss://pcs-albums-coating-pills.trycloudflare.com/ws"
CITY = "pune"
#: Same street, same run: one block is enough.
BLOCK_GAP_M = 200
#: A detour only says something about a road if the drone was over one.
BLOCK_SNAP_MAX_M = 60
BLOCKS_PER_RUN = 2
BLOCK_TTL_MIN = 20
#: A site already filed is not filed again for this long (the intake's own
#: dedup window for a stranded person is 15 minutes).
REFILE_S = 15 * 60
MESH_EVERY_S = 5.0
STALE_S = 10.0          # no message for this long while connected: say so

_DRONE_SITE = re.compile(r"D(\d+)\s+located site\s+(\d+)", re.I)
_AI = re.compile(r"^AI:\s*D(\d+)\s*->\s*Jev:\s*(.*)$")
_SECTOR = re.compile(r"sector\s+(\d+)\s+p=([\d.]+)")
_BID = re.compile(r"bid\s+(\d+)\s+p=([\d.]+)")


def _env_anchor() -> tuple[float, float] | None:
    raw = os.environ.get("DRONE_SWARM_ANCHOR", "")
    try:
        lat, lon = (float(x) for x in raw.split(","))
        return lat, lon
    except ValueError:
        return None


@dataclass
class Swarm:
    enabled: bool = os.environ.get("DRONE_SWARM", "1").lower() not in ("0", "false", "no", "off")
    url: str = os.environ.get("DRONE_SWARM_URL", DEFAULT_URL)
    scale: float = float(os.environ.get("DRONE_SWARM_SCALE", "8"))
    region: str = os.environ.get("DRONE_SWARM_REGION", "pune")
    category: str = os.environ.get("DRONE_SWARM_CATEGORY", "person_stranded")
    anchor: tuple[float, float] | None = None          # (lat, lon) of the sim origin
    anchor_source: str = ""
    # connection
    connected: bool = False
    last_message: float = 0.0
    last_error: str = ""
    connects: int = 0
    # run
    run: int = 0
    t: float = 0.0
    phase: str = ""
    mode: str = ""
    survivors: list[list[float]] = field(default_factory=list)   # sim metres
    launch_pad: list[float] = field(default_factory=list)
    found: list[int] = field(default_factory=list)
    delivered: list[int] = field(default_factory=list)
    drones: list[dict] = field(default_factory=list)
    finder: dict[int, int] = field(default_factory=dict)          # site -> drone number
    filed: dict[int, dict] = field(default_factory=dict)          # site -> {incident, at}
    blocks_this_run: int = 0
    last_block_at: float = 0.0
    last_mesh: float = 0.0
    decisions: deque = field(default_factory=lambda: deque(maxlen=30))
    log: deque = field(default_factory=lambda: deque(maxlen=60))
    actions: deque = field(default_factory=lambda: deque(maxlen=30))


swarm = Swarm()
_task: asyncio.Task | None = None
_jobs: set[asyncio.Task] = set()


# ---------------------------------------------------------------- geometry ---
def to_geo(x: float, y: float) -> tuple[float, float]:
    """Sim metres (x east, y north, origin at district centre) -> (lng, lat)."""
    lat0, lon0 = swarm.anchor or (0.0, 0.0)
    dn, de = y * swarm.scale, x * swarm.scale
    lat = lat0 + dn / 111_320.0
    lon = lon0 + de / (111_320.0 * math.cos(math.radians(lat0)))
    return round(lon, 7), round(lat, 7)


def _heading_deg(qx: float, qy: float, qz: float, qw: float) -> float:
    """Yaw from an x,y,z,w quaternion, as a compass bearing (0 = north)."""
    yaw = math.atan2(2 * (qw * qz + qx * qy), 1 - 2 * (qy * qy + qz * qz))   # from +x (east)
    return round((90 - math.degrees(yaw)) % 360, 1)


def _drone_rows(rows: list) -> list[dict]:
    out = []
    for i, r in enumerate(rows or []):
        try:
            x, y, z = float(r[0]), float(r[1]), float(r[2])
            lng, lat = to_geo(x, y)
            out.append({"n": i + 1, "x": round(x, 2), "y": round(y, 2), "alt_m": round(z * swarm.scale, 1),
                        "lng": lng, "lat": lat,
                        "heading": _heading_deg(*(float(v) for v in r[3:7])),
                        "state": str(r[7]), "battery": round(float(r[8]), 3),
                        "active": bool(int(r[9])) if len(r) > 9 else True})
        except (TypeError, ValueError, IndexError):
            continue
    return out


# ------------------------------------------------------------------ anchor ---
async def _choose_anchor() -> None:
    if swarm.anchor:
        return
    env = _env_anchor()
    if env:
        swarm.anchor, swarm.anchor_source = env, "DRONE_SWARM_ANCHOR"
        return
    from app import regions
    from app.db import session as db

    split = regions.SPLIT_LNG
    side = ">" if swarm.region == "ncr" else "<="
    try:
        row = await db.fetchrow(
            f"""
            select i.title,
                   extensions.ST_X(i.location::extensions.geometry) lng,
                   extensions.ST_Y(i.location::extensions.geometry) lat
              from incidents i join incident_categories c on c.id = i.category
             where i.city_id = $1 and i.status <> 'resolved' and c.life_safety
               and extensions.ST_X(i.location::extensions.geometry) {side} {split}
             order by i.created_at desc limit 1
            """,
            CITY,
        )
        if row:
            swarm.anchor = (float(row["lat"]), float(row["lng"]))
            swarm.anchor_source = f"open incident: {row['title']}"[:120]
            return
        row = await db.fetchrow(
            f"""
            select w.name,
                   extensions.ST_X(w.centroid::extensions.geometry) lng,
                   extensions.ST_Y(w.centroid::extensions.geometry) lat
              from wards w
              left join lateral (select score from ward_risks where ward_id = w.id
                                  order by created_at desc limit 1) r on true
             where w.city_id = $1 and extensions.ST_X(w.centroid::extensions.geometry) {side} {split}
             order by coalesce(r.score, 0) desc limit 1
            """,
            CITY,
        )
        if row:
            swarm.anchor = (float(row["lat"]), float(row["lng"]))
            swarm.anchor_source = f"riskiest ward: {row['name']}"
            return
    except Exception as exc:  # noqa: BLE001
        log.warning("swarm_anchor_failed", error=str(exc)[:160])
    lng, lat = regions.REGIONS.get(swarm.region, regions.REGIONS["pune"])["center"]
    swarm.anchor, swarm.anchor_source = (lat, lng), "region centre"


def _note(text: str) -> None:
    swarm.actions.appendleft({"at": time.time(), "text": text})
    log.info("swarm_action", text=text)


def _spawn(coro) -> None:
    """Database work off the reader, so a slow query never stalls the socket."""
    t = asyncio.create_task(coro)
    _jobs.add(t)
    t.add_done_callback(_jobs.discard)


# ---------------------------------------------------------------- messages ---
async def handle(m: dict) -> None:
    kind = m.get("type")
    swarm.last_message = time.time()
    if kind == "init":
        scene = m.get("scene") or {}
        swarm.survivors = [list(map(float, s[:3])) for s in scene.get("survivors") or []]
        swarm.launch_pad = list(scene.get("launch_pad") or [])
        swarm.mode = str(scene.get("mode") or "")
        await _choose_anchor()
        for line in (m.get("log") or [])[-40:]:
            _log_line(str(line), replay=True)
    elif kind == "frame":
        await _frame(m)
    elif kind == "log":
        _log_line(str(m.get("message") or ""))


def _new_run() -> None:
    swarm.run += 1
    swarm.found, swarm.delivered, swarm.finder = [], [], {}
    swarm.blocks_this_run = 0


async def _frame(m: dict) -> None:
    try:
        t = float(m.get("t") or 0.0)
    except (TypeError, ValueError):
        return
    if t + 5 < swarm.t or swarm.run == 0:
        _new_run()                      # t went backwards: the scenario restarted
    swarm.t, swarm.phase = t, str(m.get("phase") or "")
    if swarm.anchor is None:
        await _choose_anchor()
    swarm.drones = _drone_rows(m.get("drones") or [])

    found = sorted({int(i) for i in m.get("found") or [] if 0 <= int(i) < len(swarm.survivors)})
    for site in found:
        if site not in swarm.found:
            _spawn(_survivor_found(site))
    swarm.found = found

    delivered = sorted({int(i) for i in m.get("delivered") or []})
    for site in delivered:
        if site not in swarm.delivered:
            _spawn(_payload_delivered(site))
    swarm.delivered = delivered

    now = time.time()
    if now - swarm.last_mesh >= MESH_EVERY_S and swarm.drones:
        swarm.last_mesh = now
        _spawn(_publish_drones(list(swarm.drones)))


def _log_line(msg: str, *, replay: bool = False) -> None:
    if not msg:
        return
    swarm.log.appendleft(msg)
    if msg.startswith("SENSOR:"):
        hit = _DRONE_SITE.search(msg)
        if hit:
            swarm.finder[int(hit.group(2)) - 1] = int(hit.group(1))
    elif msg.startswith("AI:"):
        hit = _AI.match(msg)
        if hit and hit.group(2).strip():       # Jev often has nothing new to say
            body = hit.group(2).strip()
            sec = _SECTOR.search(body)
            swarm.decisions.appendleft({
                "drone": int(hit.group(1)), "t": swarm.t, "text": body[:160],
                "sector": int(sec.group(1)) if sec else None,
                "p": float(sec.group(2)) if sec else None,
                "bids": [{"site": int(b[0]) + 1, "p": float(b[1])} for b in _BID.findall(body)],
            })
    elif msg.startswith("AGENT:") and not replay:
        try:
            ev = json.loads(msg.split(":", 1)[1])
        except ValueError:
            return
        if ev.get("event") == "obstacle_detour":
            _spawn(_obstacle(ev))
    elif msg.startswith("MISSION COMPLETE"):
        _note(f"Run {swarm.run} complete: {len(swarm.found)} found, {len(swarm.delivered)} supplied.")


# ------------------------------------------------------------ integration ---
async def _survivor_found(site: int) -> None:
    from app.agents import commander
    from app.incidents import intake
    from app.mesh import service as mesh
    from app.world import events as ev
    from app.world.clock import WALL

    prev = swarm.filed.get(site)
    if prev and time.time() - prev["at"] < REFILE_S:
        return
    x, y, z = (swarm.survivors[site] + [0, 0, 0])[:3]
    lng, lat = to_geo(x, y)
    ward = await mesh.ward_at(lng, lat, CITY)
    if ward is None:
        _note(f"Site {site + 1} lies outside every ward; not filed.")
        return
    drone = swarm.finder.get(site)
    who = f"Drone D{drone}" if drone else "The drone swarm"
    note = (f"{who} located a survivor at rescue site {site + 1} "
            f"({'on a roof' if z > 2 else 'at street level'}, swarm run {swarm.run}). "
            f"Supplies will be dropped by drone; extraction needs a ground team.")
    try:
        res = await intake.receive(
            ward_id=ward, category=swarm.category, location=(lng, lat), note=note,
            source="sensor", reporter_key="device:sensor:drone-swarm",
            reporter_name="Drone swarm", device_id="drone-swarm", city_id=CITY, clock=WALL,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("swarm_intake_failed", site=site, error=str(exc)[:200])
        return
    swarm.filed[site] = {"incident": res.incident_id, "at": time.time(), "lng": lng, "lat": lat}
    if res.created_incident or res.linked:
        mesh._nudge_planner("drone swarm located a survivor")
        commander.nudge("sensor.escalation", note, key=f"swarm:site{site}", incidentId=res.incident_id)
        with contextlib.suppress(Exception):
            await ev.append(clock=WALL, kind="drone.survivor_located", actor="sensor:drone-swarm",
                            subject_type="incident", subject_id=str(res.incident_id), city_id=CITY,
                            ward_id=ward, payload={"site": site + 1, "drone": drone, "run": swarm.run})
    _note(f"Site {site + 1}: survivor {'filed as a new incident' if res.created_incident else 'linked to an open incident' if res.linked else 'held by intake'} in {ward}.")


async def _payload_delivered(site: int) -> None:
    from app.world import events as ev
    from app.world.clock import WALL

    # Joining mid-run, "found" and "delivered" arrive in the same frame: give
    # the survivor's filing a moment so the delivery lands on its incident.
    for _ in range(20):
        if site in swarm.filed:
            break
        await asyncio.sleep(0.5)
    filed = swarm.filed.get(site)
    _note(f"Site {site + 1}: drone supplies landed.")
    if not filed or not filed.get("incident"):
        return
    with contextlib.suppress(Exception):
        await ev.append(clock=WALL, kind="drone.payload_delivered", actor="sensor:drone-swarm",
                        subject_type="incident", subject_id=str(filed["incident"]), city_id=CITY,
                        payload={"site": site + 1, "run": swarm.run,
                                 "note": "Supplies delivered by drone; extraction still needs a ground team."})


async def _obstacle(evt: dict) -> None:
    """A detour over a street becomes a temporary road block for the router."""
    from app.core import cache
    from app.db import session as db
    from app.mesh import service as mesh
    from app.solver import routing
    from app.world import events as ev
    from app.world.clock import WALL

    now = time.time()
    if swarm.blocks_this_run >= BLOCKS_PER_RUN or now - swarm.last_block_at < 30:
        return
    try:
        n = int(evt.get("drone"))
    except (TypeError, ValueError):
        return
    d = next((r for r in swarm.drones if r["n"] == n), None)   # log numbers are 1-based
    if d is None:
        return
    snap = await routing.snap_to_road(d["lng"], d["lat"])
    if not snap.on_road or snap.moved_m > BLOCK_SNAP_MAX_M:
        return      # over rooftops, not over a road: says nothing about traffic
    near = await db.fetchval(
        """select 1 from road_blocks where active and city_id = $1
              and extensions.ST_DWithin(location, extensions.ST_SetSRID(
                  extensions.ST_MakePoint($2,$3),4326)::extensions.geography, $4) limit 1""",
        CITY, snap.lng, snap.lat, BLOCK_GAP_M)
    if near:
        return
    swarm.blocks_this_run += 1
    swarm.last_block_at = now
    reason = (f"Drone D{n} had to detour around an obstruction over "
              f"{snap.street or 'this street'} (seen from the air; temporary, {BLOCK_TTL_MIN} min)")
    async with db.transaction() as conn:
        block_id = await conn.fetchval(
            """insert into road_blocks (city_id, location, radius_m, reason, reported_by)
               values ($1, extensions.ST_SetSRID(extensions.ST_MakePoint($2,$3),4326)::extensions.geography,
                       150, $4, 'drone-swarm') returning id::text""",
            CITY, snap.lng, snap.lat, reason)
        await ev.append(clock=WALL, kind=ev.Kind.ROAD_BLOCKED, actor="sensor:drone-swarm",
                        subject_type="road_block", subject_id=block_id, city_id=CITY,
                        payload={"reason": reason, "via": "drone-swarm", "drone": n}, conn=conn)
    cache.citizen_state.clear()
    mesh._nudge_planner("drone swarm saw a road obstruction")
    _note(f"Road block from D{n} over {snap.street or 'an unnamed street'}; routes now avoid it.")


async def _expire_blocks() -> None:
    from app.core import cache
    from app.db import session as db
    from app.world import events as ev
    from app.world.clock import WALL

    rows = await db.fetch(
        f"""update road_blocks set active = false
             where active and reported_by = 'drone-swarm'
               and created_at < now() - interval '{BLOCK_TTL_MIN} minutes'
         returning id::text""")
    for r in rows:
        with contextlib.suppress(Exception):
            await ev.append(clock=WALL, kind=ev.Kind.ROAD_CLEARED, actor="sensor:drone-swarm",
                            subject_type="road_block", subject_id=r["id"], city_id=CITY,
                            payload={"reason": "drone-reported obstruction expired"})
    if rows:
        cache.citizen_state.clear()


async def _publish_drones(rows: list[dict]) -> None:
    from app.db import session as db

    with contextlib.suppress(Exception):
        await db.pool().executemany(
            """
            insert into mesh_nodes (id, kind, label, lat, lon, last_seen, meta)
            values ($1, 'drone', $2, $3, $4, now(), $5)
            on conflict (id) do update
               set kind = 'drone', lat = excluded.lat, lon = excluded.lon,
                   last_seen = now(), meta = excluded.meta
            """,
            [(f"swarm:D{r['n']}", f"Swarm drone D{r['n']}", r["lat"], r["lng"],
              {"transport": "drone-swarm", "state": r["state"], "battery": r["battery"],
               "active": r["active"], "alt_m": r["alt_m"], "heading": r["heading"]})
             for r in rows])


# -------------------------------------------------------------------- loop ---
async def _run() -> None:
    from websockets.asyncio.client import connect

    backoff = 2.0
    last_expire = 0.0
    while True:
        if not swarm.enabled:
            await asyncio.sleep(5)
            continue
        url = swarm.url
        try:
            async with connect(url, open_timeout=15, ping_interval=20, max_size=8 * 2**20) as ws:
                swarm.connected, swarm.connects, swarm.last_error, backoff = True, swarm.connects + 1, "", 2.0
                log.info("swarm_connected", url=url)
                async for raw in ws:
                    if not swarm.enabled or swarm.url != url:
                        break
                    try:
                        await handle(json.loads(raw))
                    except Exception as exc:  # noqa: BLE001 - one bad frame must not drop the feed
                        log.warning("swarm_message_failed", error=str(exc)[:160])
                    if time.time() - last_expire > 60:
                        last_expire = time.time()
                        _spawn(_expire_blocks())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            swarm.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
        swarm.connected = False
        # The scenario restart drops the socket on purpose: come back quickly.
        # A tunnel that is down gets a slower retry, so the logs stay readable.
        await asyncio.sleep(backoff)
        backoff = min(60.0, backoff * 2) if swarm.last_error else 2.0


def status() -> dict:
    stale = swarm.connected and time.time() - swarm.last_message > STALE_S
    return {
        "enabled": swarm.enabled, "url": swarm.url, "connected": swarm.connected and not stale,
        "lastMessageAgoS": round(time.time() - swarm.last_message, 1) if swarm.last_message else None,
        "lastError": swarm.last_error or None,
        "anchor": {"lat": swarm.anchor[0], "lon": swarm.anchor[1]} if swarm.anchor else None,
        "anchorSource": swarm.anchor_source, "scale": swarm.scale, "category": swarm.category,
        "run": swarm.run, "t": swarm.t, "phase": swarm.phase, "mode": swarm.mode,
        "survivors": [{"site": i + 1, "lng": to_geo(s[0], s[1])[0], "lat": to_geo(s[0], s[1])[1],
                       "found": i in swarm.found, "delivered": i in swarm.delivered,
                       "incidentId": (swarm.filed.get(i) or {}).get("incident")}
                      for i, s in enumerate(swarm.survivors)] if swarm.anchor else [],
        "drones": swarm.drones,
        "decisions": list(swarm.decisions)[:12],
        "actions": list(swarm.actions)[:15],
        "log": list(swarm.log)[:20],
    }


async def configure(*, url: str | None = None, enabled: bool | None = None,
                    anchor: tuple[float, float] | None = None, scale: float | None = None) -> dict:
    if url:
        swarm.url = url
    if enabled is not None:
        swarm.enabled = enabled
    if scale:
        swarm.scale = scale
    if anchor:
        swarm.anchor, swarm.anchor_source = anchor, "set by an officer"
        swarm.filed.clear()           # new streets: sightings there are new reports
    with contextlib.suppress(Exception):
        await _save()
    return status()


async def _save() -> None:
    from app.db import session as db

    await db.execute(
        """insert into mesh_state (key, value) values ('drone_swarm', $1)
           on conflict (key) do update set value = excluded.value""",
        {"url": swarm.url, "enabled": swarm.enabled, "scale": swarm.scale,
         "anchor": list(swarm.anchor) if swarm.anchor and swarm.anchor_source == "set by an officer" else None})


async def _load() -> None:
    """What an officer set survives a redeploy (the tunnel URL above all)."""
    from app.db import session as db

    with contextlib.suppress(Exception):
        v = await db.fetchval("select value from mesh_state where key = 'drone_swarm'")
        v = json.loads(v) if isinstance(v, str) else v
        if v:
            swarm.url = v.get("url") or swarm.url
            swarm.enabled = bool(v.get("enabled", swarm.enabled))
            swarm.scale = float(v.get("scale") or swarm.scale)
            if v.get("anchor"):
                swarm.anchor, swarm.anchor_source = tuple(v["anchor"]), "set by an officer"


async def start() -> None:
    global _task
    await _load()
    if _task is None or _task.done():
        _task = asyncio.create_task(_run(), name="drone-swarm-listener")


async def stop() -> None:
    global _task
    if _task:
        _task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await _task
        _task = None
