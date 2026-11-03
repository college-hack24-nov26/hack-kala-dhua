"""A virtual sensor fleet, so the command centre always has a sensor field.

The physical LoRa nodes are a bench demo: two Unos, one radio each, and a
radio that may not be answering today. Without them the analytics page is
empty and nothing on the sensor side feeds the response. This module runs a
fleet of virtual nodes inside the API process and pushes their readings
through exactly the same door a real gateway uses (`service.ingest`), so they
are scored by the same fusion code, learn their own baselines, escalate into
incidents through the same mesh/intake/trust path, and appear on the map,
the charts and the agent's tools like any other node.

Honesty rules:
  * every virtual node id starts with `V-` and is stored with simulated=true;
    the console labels them "simulated";
  * a real node posting through the bridge simply joins the field beside them.

Four kinds of node (see kinds.py): the full field module, a gas & heat
sentinel, a structural monitor and a rubble listening probe. Each node idles
around its own noisy baseline and occasionally lives through an episode:

    f  gas leak / fire      gas rises, temperature climbs
    c  structural movement  lean, shocks, vibration, tilt switch
    t  trapped person       repeated tapping, voices, warmth, CO2, slight lean

Episodes start three ways: the demo runner cues the node nearest a report
(`service.cue_near`), a scenario rhythm cues a random node (`cue_any`), or
the fleet itself starts a quiet one now and then. Some episodes are minor and
only show up as "watch" readings; major ones cross the escalation line.

New nodes are deployed over time (a probe "dropped" into the riskiest ward),
and staff can deploy one by hand, so the field visibly grows.

Environment:
    IOT_VIRTUAL_NODES       1 (default) / 0
    IOT_VIRTUAL_PERIOD_S    seconds between readings per node (default 6)
    IOT_VIRTUAL_DEPLOY_S    seconds between automatic deployments (default 420; 0 = never)
"""
from __future__ import annotations

import asyncio
import contextlib
import math
import os
import random
import time
from dataclasses import dataclass, field

from app import regions
from app.core.logging import get_logger
from app.db import session as db
from app.iot import kinds

log = get_logger(__name__)

ENABLED_DEFAULT = os.environ.get("IOT_VIRTUAL_NODES", "1").lower() not in ("0", "false", "no", "off")
PERIOD_S = max(2.0, float(os.environ.get("IOT_VIRTUAL_PERIOD_S", "6")))
DEPLOY_EVERY_S = float(os.environ.get("IOT_VIRTUAL_DEPLOY_S", "420"))
MAX_EXTRA_PER_REGION = 4
KEEP_HOURS = 6
#: Mean seconds between unprompted episodes in one region.
SPONTANEOUS_S = 200.0
CITY = "pune"
#: Readings written at once; the API's pool is 10 connections.
CONCURRENCY = 6
REGION_CODE = {"pune": "PUN", "ncr": "NCR"}
#: The fleet every region starts with.
BASE = ("field", "gas", "gas", "struct", "struct", "rescue", "rescue")


# ------------------------------------------------------------------ model ---
@dataclass
class Episode:
    code: str           # f / c / t
    major: bool
    start: float
    ramp: float
    hold: float
    fade: float
    strength: float

    def level(self, now: float) -> float:
        t = now - self.start
        if t < 0:
            return 0.0
        if t < self.ramp:
            return t / self.ramp
        t -= self.ramp
        if t < self.hold:
            return 1.0
        t -= self.hold
        return max(0.0, 1.0 - t / self.fade)

    def done(self, now: float) -> bool:
        return now - self.start > self.ramp + self.hold + self.fade


@dataclass
class VNode:
    id: str
    kind: str
    region: str
    lat: float
    lon: float
    place: str
    deployed_at: float = field(default_factory=time.time)
    seq: int = 0
    episode: Episode | None = None
    rng: random.Random = field(default_factory=random.Random)
    base: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        r = random.Random(self.id)        # stable personality per node id
        warm = 31.0 if self.region == "ncr" else 28.5
        self.base = {
            "mq2": r.uniform(140, 220), "mq135": r.uniform(170, 260),
            "temp_c": warm + r.uniform(-1.5, 1.5), "mic": r.uniform(45, 80),
            "piezo": r.uniform(10, 30), "tilt_deg": r.uniform(0.5, 4.0),
            "dist_km": r.uniform(0.4, 3.5),
        }
        self.rng = random.Random(f"{self.id}:{time.time()}")

    @property
    def label(self) -> str:
        return f"{kinds.KINDS[self.kind]['label']} · {self.place}"[:80]

    def start(self, code: str, *, major: bool, delay: float = 0.0) -> bool:
        if code == "0":
            self.episode = None
            return True
        if not kinds.can_cue(self.id, code):
            return False
        r = self.rng
        self.episode = Episode(code=code, major=major, start=time.time() + delay,
                               ramp=r.uniform(10, 20), hold=r.uniform(40, 80) if major else r.uniform(25, 45),
                               fade=r.uniform(25, 40), strength=r.uniform(0.8, 1.1) if major else r.uniform(0.3, 0.5))
        return True

    def reading(self, now: float, uptime: int) -> dict:
        r, b = self.rng, self.base
        ep = self.episode
        if ep and ep.done(now):
            self.episode = ep = None
        lvl = ep.level(now) if ep else 0.0
        code = ep.code if ep else ""
        s = ep.strength if ep else 0.0
        day = math.sin(2 * math.pi * (now % 86400) / 86400)

        v = {
            "mq2": b["mq2"] + r.gauss(0, 5), "mq135": b["mq135"] + r.gauss(0, 7),
            "temp_c": b["temp_c"] + 1.2 * day + r.gauss(0, 0.15),
            "mic": b["mic"] + abs(r.gauss(0, 5)), "piezo": b["piezo"] + abs(r.gauss(0, 3)),
            "knocks": 0, "tilt_deg": b["tilt_deg"] + r.gauss(0, 0.06),
            "gyro_dps": abs(r.gauss(1.5, 0.8)), "vib_g": abs(r.gauss(0.008, 0.003)),
            "tilt_sw": 0, "pir": 0,
        }
        if code == "f":
            if ep.major:
                v["mq2"] += 380 * s * lvl
                v["mq135"] += 280 * s * lvl
                v["temp_c"] += 30 * s * lvl
            else:
                v["mq2"] += 70 * s * lvl
                v["mq135"] += 70 * s * lvl
                v["temp_c"] += 3 * lvl
            v["mic"] += 25 * lvl * r.random()               # crackle, fans, people shouting
        elif code == "c":
            v["tilt_deg"] += (10 if ep.major else 5) * s * lvl
            if r.random() < 0.5 * lvl:
                v["gyro_dps"] += r.uniform(30, 110) * s
            v["vib_g"] += 0.25 * s * lvl * r.random()
            v["piezo"] += 300 * s * lvl * r.random()
            v["tilt_sw"] = 1 if ep.major and lvl > 0.6 else 0
        elif code == "t":
            tapping = lvl > 0.3 and r.random() < (0.85 if ep.major else 0.35)
            v["knocks"] = r.choice((2, 3, 4)) if tapping and ep.major else (1 if tapping else 0)
            v["piezo"] += 110 * v["knocks"]
            if r.random() < lvl:
                v["mic"] += r.uniform(60, 160) * s                # voices
            v["temp_c"] += 3.0 * lvl
            v["mq135"] += 45 * s * lvl                            # breath in a void
            v["tilt_deg"] += (6.5 if ep.major else 2.5) * lvl     # the rubble settled
            v["pir"] = 1 if ep.major and lvl > 0.5 and r.random() < 0.6 else 0

        self.seq += 1
        chans = kinds.KINDS[self.kind]["channels"]
        obs: dict = {"node": self.id, "seq": self.seq, "up": uptime, "virtual": True,
                     "label": self.label, "lat": self.lat, "lon": self.lon}
        for k, val in v.items():
            if k in chans or (k == "pir" and self.kind == "rescue"):
                obs[k] = round(val, 3) if isinstance(val, float) else val
        obs["rssi"] = int(max(-122, min(-55, -62 - b["dist_km"] * 12 + r.gauss(0, 2.5))))
        obs["snr"] = round(max(-12.0, 10.5 - b["dist_km"] * 3 + r.gauss(0, 1.0)), 1)
        return obs


# ------------------------------------------------------------------ fleet ---
@dataclass
class _Fleet:
    enabled: bool = ENABLED_DEFAULT
    nodes: dict[str, VNode] = field(default_factory=dict)
    started: float = field(default_factory=time.time)
    last_deploy: float = field(default_factory=time.time)
    last_prune: float = 0.0
    ticks: int = 0
    built: bool = False
    deployed: list[dict] = field(default_factory=list)


fleet = _Fleet()
_task: asyncio.Task | None = None
_rng = random.Random()
_lock = asyncio.Lock()


async def _wards(region: str) -> list[dict]:
    try:
        rows = await db.fetch(
            f"""
            select w.id, w.name,
                   extensions.ST_X(w.centroid::extensions.geometry) lng,
                   extensions.ST_Y(w.centroid::extensions.geometry) lat,
                   coalesce(r.score, 0.2) score
              from wards w
              left join lateral (select score from ward_risks where ward_id = w.id
                                  order by created_at desc limit 1) r on true
             where w.city_id = $1
               and {regions.sql('extensions.ST_X(w.centroid::extensions.geometry)')} = $2
             order by w.id
            """,
            CITY, region,
        )
        return [{"id": r["id"], "name": r["name"], "lng": float(r["lng"]), "lat": float(r["lat"]),
                 "score": float(r["score"])} for r in rows if r["lng"] is not None]
    except Exception as exc:  # noqa: BLE001
        log.warning("iot_virtual_wards_failed", region=region, error=str(exc)[:160])
        return []


def _spot(region: str, wards: list[dict], r: random.Random) -> tuple[float, float, str]:
    if wards:
        w = r.choice(wards)
        return (w["lat"] + r.uniform(-0.0025, 0.0025), w["lng"] + r.uniform(-0.0025, 0.0025), w["name"])
    clng, clat = regions.REGIONS[region]["center"]
    return clat + r.uniform(-0.03, 0.03), clng + r.uniform(-0.03, 0.03), regions.REGIONS[region]["name"]


def _next_id(region: str, kind: str) -> str:
    prefix = f"{kinds.VIRTUAL_PREFIX}{REGION_CODE[region]}-{kinds.CODE_OF[kind]}"
    n = 1
    while f"{prefix}{n}" in fleet.nodes:
        n += 1
    return f"{prefix}{n}"


async def _build() -> None:
    for region in regions.REGIONS:
        wards = await _wards(region)
        r = random.Random(f"fleet:{region}")             # same places every restart
        picks = r.sample(wards, k=min(len(wards), len(BASE))) if wards else []
        for i, kind in enumerate(BASE):
            nid = _next_id(region, kind)
            if i < len(picks):
                w = picks[i]
                lat, lon, place = (w["lat"] + r.uniform(-0.002, 0.002),
                                   w["lng"] + r.uniform(-0.002, 0.002), w["name"])
            else:
                lat, lon, place = _spot(region, wards, r)
            fleet.nodes[nid] = VNode(nid, kind, region, lat, lon, place, deployed_at=0)
    # Nodes deployed before a restart keep reporting.
    try:
        rows = await db.fetch(
            """select id, lat, lon, label from sensor_nodes
                where id like 'V-%' and lat is not null and last_seen > now() - interval '24 hours'""")
        for row in rows:
            nid = row["id"]
            if nid in fleet.nodes:
                continue
            region = regions.region_of(row["lon"], row["lat"]) or "pune"
            place = (row["label"] or "").split(" · ")[-1] or regions.REGIONS[region]["name"]
            fleet.nodes[nid] = VNode(nid, kinds.kind_of(nid), region, row["lat"], row["lon"], place,
                                     deployed_at=0)
    except Exception as exc:  # noqa: BLE001
        log.warning("iot_virtual_resume_failed", error=str(exc)[:160])
    fleet.built = True
    log.info("iot_virtual_fleet", nodes=len(fleet.nodes), period_s=PERIOD_S)


async def deploy(kind: str = "rescue", region: str = "pune", *, lat: float | None = None,
                 lon: float | None = None, reason: str = "deployed by an officer",
                 episode: str | None = None) -> dict:
    """Add a node to the field. It reports on the next tick."""
    if kind not in kinds.KINDS:
        raise ValueError(f"kind must be one of {', '.join(kinds.KINDS)}")
    region = regions.valid(region) or "pune"
    async with _lock:
        if not fleet.built:
            await _build()
        if lat is not None and lon is not None:
            place = regions.REGIONS[region]["name"]
            try:
                from app.mesh import service as mesh
                ward = await mesh.ward_at(lon, lat, CITY)
                if ward:
                    place = await db.fetchval("select name from wards where id = $1", ward) or place
            except Exception:  # noqa: BLE001
                pass
        else:
            wards = await _wards(region)
            # The riskiest wards are where a responder would drop a probe.
            if wards:
                w = _rng.choices(wards, weights=[max(0.05, x["score"]) ** 2 for x in wards], k=1)[0]
                lat, lon, place = (w["lat"] + _rng.uniform(-0.002, 0.002),
                                   w["lng"] + _rng.uniform(-0.002, 0.002), w["name"])
            else:
                lat, lon, place = _spot(region, [], _rng)
        nid = _next_id(region, kind)
        node = VNode(nid, kind, region, lat, lon, place)
        if episode:
            node.start(episode, major=True, delay=_rng.uniform(20, 45))
        fleet.nodes[nid] = node
        info = {"id": nid, "kind": kind, "label": node.label, "lat": lat, "lon": lon,
                "region": region, "reason": reason, "at": time.time()}
        fleet.deployed = (fleet.deployed + [info])[-20:]
    log.info("iot_virtual_deployed", **{k: v for k, v in info.items() if k != "at"})
    return info


def set_enabled(on: bool) -> dict:
    fleet.enabled = bool(on)
    log.info("iot_virtual_enabled", enabled=fleet.enabled)
    return status()


def status() -> dict:
    by_kind: dict[str, int] = {}
    for n in fleet.nodes.values():
        by_kind[n.kind] = by_kind.get(n.kind, 0) + 1
    return {
        "enabled": fleet.enabled, "period_s": PERIOD_S, "nodes": len(fleet.nodes),
        "by_kind": by_kind,
        "episodes": [{"node": n.id, "code": n.episode.code, "major": n.episode.major}
                     for n in fleet.nodes.values() if n.episode],
        "deployed": fleet.deployed[-5:],
        "kinds": {k: {"label": v["label"], "detail": v["detail"]} for k, v in kinds.KINDS.items()},
    }


# ------------------------------------------------------------------- loop ---
async def _tick() -> None:
    from app.iot import service

    now = time.time()
    # 1. cues from the demo runner and the scenario rhythm
    for c in service.take_virtual_cues():
        n = fleet.nodes.get(c["node"])
        if n and n.start(c["event"], major=True):
            log.info("iot_virtual_cued", node=n.id, event=c["event"])

    # 2. the field's own rhythm: now and then something happens unprompted
    for region in regions.REGIONS:
        if _rng.random() < PERIOD_S / SPONTANEOUS_S:
            # only nodes that have learned their quiet baseline (fusion.LEARN_N)
            idle = [n for n in fleet.nodes.values()
                    if n.region == region and n.episode is None and n.seq >= 20]
            if idle:
                n = _rng.choice(idle)
                code = _rng.choice(kinds.KINDS[n.kind]["cues"])
                n.start(code, major=_rng.random() < 0.35)

    # 3. the field grows
    extras = lambda reg: sum(1 for n in fleet.nodes.values() if n.region == reg and n.deployed_at)  # noqa: E731
    if DEPLOY_EVERY_S > 0 and now - fleet.last_deploy > DEPLOY_EVERY_S:
        fleet.last_deploy = now
        room = [reg for reg in regions.REGIONS if extras(reg) < MAX_EXTRA_PER_REGION]
        if room:
            kind = _rng.choice(("rescue", "rescue", "gas", "struct"))
            with contextlib.suppress(Exception):
                await deploy(kind, _rng.choice(room), reason="auto-deployed to the riskiest ward",
                             episode=_rng.choice(kinds.KINDS[kind]["cues"]) if _rng.random() < 0.5 else None)

    # 4. readings, through the same door as the real gateway
    # Nodes are independent, so they are written side by side: one after the
    # other, a remote database turns a 6 s period into 20 s.
    uptime = int(now - fleet.started) + 900       # gas heaters long since warm
    gate = asyncio.Semaphore(CONCURRENCY)

    async def send(n: VNode) -> None:
        async with gate:
            try:
                await service.ingest([n.reading(now, uptime)], gateway_id=f"virtual-gw-{n.region}",
                                     city_id=CITY)
            except Exception as exc:  # noqa: BLE001
                log.warning("iot_virtual_ingest_failed", node=n.id, error=str(exc)[:200])

    await asyncio.gather(*(send(n) for n in list(fleet.nodes.values())))

    # 5. keep the table small
    if now - fleet.last_prune > 600:
        fleet.last_prune = now
        with contextlib.suppress(Exception):
            await db.execute(
                f"""delete from sensor_readings where node_id like 'V-%'
                     and observed_at < now() - interval '{KEEP_HOURS} hours'""")
    fleet.ticks += 1


async def _run() -> None:
    await asyncio.sleep(3)                     # let startup finish first
    while True:
        t0 = time.monotonic()
        try:
            if fleet.enabled:
                async with _lock:
                    if not fleet.built:
                        await _build()
                await _tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the fleet must never take the API down
            log.warning("iot_virtual_tick_failed", error=str(exc)[:200])
        await asyncio.sleep(max(0.5, PERIOD_S - (time.monotonic() - t0)))


async def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_run(), name="iot-virtual-fleet")


async def stop() -> None:
    global _task
    if _task:
        _task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await _task
        _task = None
