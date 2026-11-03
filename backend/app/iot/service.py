"""LoRa sensor nodes: store readings, score them, escalate, answer analytics.

A reading's path:

    gateway Arduino -> lora_bridge.py -> POST /iot/observations -> ingest()
        node row (location, baseline, state)   sensor_nodes
        the reading with its scores            sensor_readings
        the node on the Mesh & devices screen  mesh_nodes (kind 'sensor')
        a hazard that persists                 an S packet through the mesh
                                               service, i.e. the same intake,
                                               trust scoring, dedup and
                                               commander nudge as a camera
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.db import session as db
from app.iot import fusion, kinds

log = get_logger(__name__)

LIVE_S = 60                 # a node heard within this is "online"
NEW_S = 180                 # a node first heard within this is shown as new
ESCALATE_SIMULATED = os.environ.get("IOT_ESCALATE_SIMULATED", "") in ("1", "true", "yes")

# Channels a node can report as MIMICKED rather than measured (node.ino `sim`
# bitmask), and which evidence each one feeds. A score driven only by mimicked
# channels is labelled simulated and, unless IOT_ESCALATE_SIMULATED is set,
# never files an incident.
SIM_BITS = {1: "mq2", 2: "mq135", 4: "temp_c", 8: "imu", 0x10: "mic", 0x20: "piezo", 0x40: "tilt_sw"}
ALL_SIM = 0x7F
EVIDENCE_CHANNEL = {"gas_mq2": 1, "gas_mq135": 2, "breath": 2, "heat": 4, "warmth": 4,
                    "lean": 8, "shock": 8, "shaking": 8, "audio": 0x10, "tapping": 0x20,
                    "tilt_switch": 0x40, "pir": 0}


def simulated_channels(mask: int | None) -> list[str]:
    return [n for b, n in SIM_BITS.items() if (mask or 0) & b]


def driven_by_simulation(s: fusion.Scored, mask: int | None) -> bool:
    """True when every piece of evidence that matters came from a mimicked channel."""
    if not mask:
        return False
    strong = [k for k, v in s.evidence.items() if v >= 0.3]
    return bool(strong) and all(EVIDENCE_CHANNEL.get(k, 0) & mask for k in strong)

# Columns a client may ask the analytics endpoints for, raw and derived.
METRICS = {
    "overall": "overall", "human": "human", "structural": "structural",
    "environmental": "environmental", "mq2": "mq2", "mq135": "mq135",
    "temp_c": "temp_c", "tilt_deg": "tilt_deg", "gyro_dps": "gyro_dps",
    "vib_g": "vib_g", "mic": "mic", "piezo": "piezo", "knocks": "knocks",
}
_COLS = ("mq2", "mq135", "temp_c", "tilt_deg", "gyro_dps", "vib_g", "mic",
         "piezo", "knocks", "tilt_sw", "pir")


def _json(v: Any) -> Any:
    import json
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return {}
    return v or {}


def _when(v: Any) -> datetime:
    now = datetime.now(timezone.utc)
    if isinstance(v, str):
        try:
            t = datetime.fromisoformat(v.replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            # backlog from a bridge that was offline: keep its time, within reason
            if now - timedelta(days=2) <= t <= now + timedelta(seconds=30):
                return min(t, now)
        except ValueError:
            pass
    return now


def _f(v: Any) -> float | None:
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------- ingest ---
async def ingest(observations: list[dict], *, gateway_id: str, city_id: str = "pune") -> dict:
    scored: list[dict] = []
    errors = 0
    for obs in observations:
        node = str(obs.get("node") or "").strip()[:40]
        if not node:
            errors += 1
            continue
        try:
            scored.append(await _one(node, obs, gateway_id=gateway_id, city_id=city_id))
        except Exception as exc:  # noqa: BLE001 - one bad reading must not drop the batch
            errors += 1
            log.warning("iot_reading_failed", node=node, error=str(exc))
    await _touch_gateway(gateway_id)
    return {"accepted": len(scored), "errors": errors, "scored": scored}


async def _one(node: str, obs: dict, *, gateway_id: str, city_id: str) -> dict:
    from app.mesh import service as mesh

    lat, lon = _f(obs.get("lat")), _f(obs.get("lon"))
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        lat = lon = None
    observed = _when(obs.get("received_at") or obs.get("receivedAt"))
    seq = obs.get("seq")
    sim_mask = _int(obs.get("sim"))
    # Every node is treated as live: mimicked channels score, flag and escalate
    # exactly like measured ones. The mask is kept in `extra` for the bench only.
    # Nodes of the virtual fleet (app.iot.virtual) are stored as simulated so
    # the console can say so; they still score and escalate like any other.
    simulated = bool(obs.get("virtual")) and kinds.is_virtual(node)
    label = str(obs["label"])[:80] if obs.get("label") else None

    async with db.transaction() as conn:
        row = await conn.fetchrow(
            """
            insert into sensor_nodes (id, city_id, label, lat, lon, gateway_id, simulated)
            values ($1, $2, coalesce($7::text, $1), $3, $4, $5, $6)
            on conflict (id) do update
               set lat = coalesce(excluded.lat, sensor_nodes.lat),
                   lon = coalesce(excluded.lon, sensor_nodes.lon),
                   gateway_id = excluded.gateway_id,
                   simulated = excluded.simulated,
                   label = coalesce($7::text, sensor_nodes.label)
            returning lat, lon, ward_id, last_seq, baseline, state, escalated,
                      (xmax = 0) as created
            """,
            node, city_id, lat, lon, gateway_id, simulated, label,
        )
        lat, lon = row["lat"], row["lon"]
        ward_id = row["ward_id"]
        # The same reading can arrive twice: once from its own laptop over USB,
        # once from the other side over LoRa. Seq + uptime identify it.
        if seq is not None and obs.get("up") is not None:
            dup = await conn.fetchval(
                """select 1 from sensor_readings
                    where node_id = $1 and seq = $2 and uptime_s = $3
                      and observed_at > now() - interval '1 hour' limit 1""",
                node, _int(seq), _int(obs.get("up")))
            if dup:
                return {"node": node, "duplicate": True}
        if lat is not None and lon is not None and (ward_id is None or obs.get("lat") is not None):
            ward_id = await mesh.ward_at(lon, lat, city_id)

        s = fusion.score(obs, _json(row["baseline"]), _json(row["state"]))

        lost = 0
        try:
            if row["last_seq"] is not None and seq is not None and int(seq) > row["last_seq"] + 1:
                lost = int(seq) - row["last_seq"] - 1
        except (TypeError, ValueError):
            pass

        last_esc = _json(row["escalated"])
        found = fusion.escalations(s, s.state, observed.timestamp(), last_esc)
        flags = list(s.flags) + (["packet_loss"] if lost else [])
        flags += [f"escalated:{e['kind']}" for e in found]

        await conn.execute(
            """
            insert into sensor_readings
              (node_id, city_id, observed_at, seq, uptime_s, lat, lon,
               mq2, mq135, temp_c, tilt_deg, gyro_dps, vib_g, mic, piezo, knocks, tilt_sw, pir,
               rssi, snr, human, structural, environmental, overall, evidence, flags, raw, extra)
            values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,
                    $19,$20,$21,$22,$23,$24,$25,$26,$27,$28)
            """,
            node, city_id, observed, _int(seq), _int(obs.get("up")), lat, lon,
            *[_f(obs.get(c)) if c not in ("knocks", "tilt_sw", "pir") else _int(obs.get(c))
              for c in _COLS],
            _int(obs.get("rssi")), _f(obs.get("snr")),
            s.human, s.structural, s.environmental, s.overall, s.evidence, flags,
            (str(obs.get("raw")) if obs.get("raw") else None),
            {"sim_mask": sim_mask} if sim_mask else {},
        )

        state = s.state | {"latest": s.as_dict() | {"at": observed.isoformat()}}
        for e in found:
            last_esc[e["kind"]] = observed.timestamp()
        await conn.execute(
            """
            update sensor_nodes
               set ward_id = $2, last_seen = greatest(last_seen, $3), readings = readings + 1,
                   last_seq = coalesce($4, last_seq), lost = lost + $5,
                   rssi = coalesce($6, rssi), snr = coalesce($7, snr),
                   baseline = $8, state = $9, escalated = $10
             where id = $1
            """,
            node, ward_id, observed, _int(seq), lost, _int(obs.get("rssi")), _f(obs.get("snr")),
            s.baseline, state, last_esc,
        )

    if row["created"]:
        log.info("iot_node_joined", node=node, kind=kinds.kind_of(node), simulated=simulated)
    await _touch_mesh_node(node, lat, lon, ward_id, s, obs)

    filed = []
    if found and lat is not None and lon is not None:
        for e in found:
            filed.append(await _escalate(node, e, lat, lon, s, city_id))
    elif found:
        log.info("iot_escalation_not_filed", node=node, kinds=[e["kind"] for e in found],
                 reason="node has no location")

    return {"node": node, **s.as_dict(), "escalated": [f["kind"] for f in filed] or None,
            "lost": lost}


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


async def _escalate(node: str, e: dict, lat: float, lon: float, s: fusion.Scored,
                    city_id: str) -> dict:
    """File the hazard exactly as a camera's S packet is filed."""
    from app.mesh import envelope
    from app.mesh import service as mesh

    kind = e["kind"]
    packet_kind = ("fire" if s.evidence.get("heat", 0) >= 0.5 else "smoke") if kind == "fire" \
        else "collapse"
    t = int(time.time())
    text = envelope.encode(
        "S",
        {"id": f"{node}-{kind}-{t}"[:60], "n": node, "k": packet_kind,
         "c": e["confidence"], "la": round(lat, 6), "lo": round(lon, 6),
         "f": (f"Simulated {kinds.KINDS[kinds.kind_of(node)]['label'].lower()}"
               if kinds.is_virtual(node) else "LoRa sensor node"),
         "x": e["why"][:160], "t": t},
        key=settings.mesh_hmac_key,
    )
    try:
        result = (await mesh.receive([text], gateway_id=f"lora:{node}", city_id=city_id))[0]
    except Exception as exc:  # noqa: BLE001
        log.warning("iot_escalation_failed", node=node, kind=kind, error=str(exc))
        result = {"outcome": "failed", "error": str(exc)}
    log.info("iot_escalated", node=node, kind=kind, confidence=e["confidence"],
             outcome=result.get("outcome"))
    return {"kind": kind, **result}


async def _touch_mesh_node(node: str, lat, lon, ward_id, s: fusion.Scored, obs: dict) -> None:
    """Show the node on the existing Mesh & devices screen."""
    try:
        await db.execute(
            """
            insert into mesh_nodes (id, kind, label, lat, lon, ward_id, last_seen, meta)
            values ($1, 'sensor', $2, $3, $4, $5, now(), $6)
            on conflict (id) do update
               set kind = 'sensor', last_seen = now(),
                   lat = coalesce(excluded.lat, mesh_nodes.lat),
                   lon = coalesce(excluded.lon, mesh_nodes.lon),
                   ward_id = coalesce(excluded.ward_id, mesh_nodes.ward_id),
                   meta = mesh_nodes.meta || excluded.meta
            """,
            f"lora:{node}",
            (f"{kinds.KINDS[kinds.kind_of(node)]['label']} {node} (simulated)"
             if kinds.is_virtual(node) else f"LoRa node {node}"),
            lat, lon, ward_id,
            {"transport": "virtual" if kinds.is_virtual(node) else "lora",
             "kind": kinds.kind_of(node), "rssi": obs.get("rssi"), "overall": round(s.overall, 2)},
        )
    except Exception as exc:  # noqa: BLE001 - cosmetic
        log.debug("iot_mesh_node_touch_failed", error=str(exc))


async def _touch_gateway(gateway_id: str) -> None:
    try:
        await db.execute(
            """
            insert into mesh_nodes (id, kind, label, last_seen, meta)
            values ($1, 'gateway', $2, now(), '{"transport":"lora"}'::jsonb)
            on conflict (id) do update set last_seen = now()
            """,
            gateway_id, f"LoRa gateway {gateway_id}",
        )
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ analytics ---
def _reading(r: Any) -> dict:
    d = dict(r)
    d["observed_at"] = d["observed_at"].isoformat()
    if "evidence" in d:
        d["evidence"] = _json(d["evidence"])
    return d


async def overview(city_id: str = "pune", minutes: int = 30) -> dict:
    """Everything the analytics page polls: nodes with their latest reading,
    headline numbers, and the recent flagged readings."""
    nodes = await db.fetch(
        """
        select n.id, n.label, n.lat, n.lon, n.ward_id, n.gateway_id, n.simulated,
               n.first_seen, extract(epoch from now() - n.first_seen)::int first_age_s,
               n.last_seen, n.readings, n.lost, n.rssi, n.snr, n.baseline,
               extract(epoch from now() - n.last_seen)::int age_s,
               r.observed_at, r.seq, r.uptime_s, r.mq2, r.mq135, r.temp_c, r.tilt_deg,
               r.gyro_dps, r.vib_g, r.mic, r.piezo, r.knocks, r.tilt_sw, r.pir,
               r.human, r.structural, r.environmental, r.overall, r.evidence, r.flags
          from sensor_nodes n
          left join lateral (
                select * from sensor_readings x
                 where x.node_id = n.id order by x.observed_at desc limit 1) r on true
         where n.city_id = $1
         order by n.last_seen desc
        """,
        city_id,
    )
    events = await db.fetch(
        """
        select id, node_id, observed_at, human, structural, environmental, overall,
               mq2, mq135, temp_c, tilt_deg, mic, piezo, knocks, flags
          from sensor_readings
         where city_id = $1 and observed_at > now() - make_interval(mins => $2)
           and exists (select 1 from unnest(flags) f
                        where f not in ('warming_up', 'learning'))
         order by observed_at desc limit 60
        """,
        city_id, max(1, min(minutes, 24 * 60)),
    )
    out_nodes = []
    for n in nodes:
        d = dict(n)
        d["last_seen"] = d["last_seen"].isoformat()
        d["first_seen"] = d["first_seen"].isoformat()
        d.update(kinds.describe(d["id"]))
        d["is_new"] = (d.pop("first_age_s") or 0) < NEW_S
        d["observed_at"] = d["observed_at"].isoformat() if d["observed_at"] else None
        d["evidence"] = _json(d["evidence"])
        d["baseline"] = {k: round(v, 2) for k, v in _json(d["baseline"]).items()
                         if not k.startswith("n_") and isinstance(v, (int, float))}
        d["online"] = d["age_s"] is not None and d["age_s"] < LIVE_S
        out_nodes.append(d)
    live = [n for n in out_nodes if n["online"]]
    by_kind: dict[str, dict] = {}
    for n in out_nodes:
        k = by_kind.setdefault(n["kind"], {"label": n["kind_label"], "total": 0, "online": 0})
        k["total"] += 1
        k["online"] += int(n["online"])

    def peak(key: str) -> dict | None:
        best = max(live, key=lambda n: n.get(key) or 0, default=None)
        return {"node": best["id"], "value": best.get(key) or 0} if best else None

    escalated = await db.fetchval(
        """
        select count(*) from sensor_readings, unnest(flags) f
         where city_id = $1 and observed_at > now() - interval '1 hour'
           and f like 'escalated:%'
        """,
        city_id,
    )
    return {
        "nodes": out_nodes,
        "summary": {"total": len(out_nodes), "online": len(live),
                    "human": peak("human"), "structural": peak("structural"),
                    "environmental": peak("environmental"), "overall": peak("overall"),
                    "escalated_1h": int(escalated or 0),
                    "simulated": sum(1 for n in out_nodes if n["simulated"]),
                    "real": sum(1 for n in out_nodes if not n["simulated"]),
                    "new": [n["id"] for n in out_nodes if n["is_new"] and n["online"]],
                    "by_kind": by_kind},
        "events": [_reading(e) for e in events],
        "metrics": list(METRICS),
    }


async def spatial(metric: str, city_id: str = "pune", minutes: int = 10) -> dict:
    """Latest value of one metric per located node, for a heatmap layer."""
    col = METRICS.get(metric)
    if col is None:
        raise ValueError(f"unknown metric {metric!r}; one of {', '.join(METRICS)}")
    rows = await db.fetch(
        f"""
        select distinct on (r.node_id) r.node_id, coalesce(r.lat, n.lat) lat,
               coalesce(r.lon, n.lon) lon, r.{col} as value, r.observed_at
          from sensor_readings r join sensor_nodes n on n.id = r.node_id
         where r.city_id = $1 and r.observed_at > now() - make_interval(mins => $2)
           and coalesce(r.lat, n.lat) is not null and r.{col} is not null
         order by r.node_id, r.observed_at desc
        """,
        city_id, max(1, min(minutes, 24 * 60)),
    )
    return {"metric": metric, "points": [_reading(r) for r in rows]}


async def timeseries(node: str, minutes: int = 30, points: int = 300) -> dict:
    """One node's history, averaged into at most `points` buckets."""
    minutes = max(1, min(minutes, 7 * 24 * 60))
    bucket_s = max(2, int(minutes * 60 / max(10, min(points, 1000))))
    rows = await db.fetch(
        """
        with b as (
            select date_bin(make_interval(secs => $3), observed_at, timestamptz 'epoch') bk, *
              from sensor_readings
             where node_id = $1 and observed_at > now() - make_interval(mins => $2)
        )
        select bk as observed_at,
               avg(mq2) mq2, avg(mq135) mq135, avg(temp_c) temp_c, avg(tilt_deg) tilt_deg,
               max(gyro_dps) gyro_dps, max(vib_g) vib_g, avg(mic) mic, max(piezo) piezo,
               sum(knocks)::int knocks, max(human) human, max(structural) structural,
               max(environmental) environmental, max(overall) overall,
               (select array_agg(distinct f) from b b2, unnest(b2.flags) f
                 where b2.bk = b.bk) flags
          from b group by bk order by bk
        """,
        node, minutes, bucket_s,
    )
    latest = await db.fetchrow(
        "select evidence from sensor_readings where node_id = $1 order by observed_at desc limit 1",
        node,
    )
    return {"node": node, "bucket_s": bucket_s,
            "points": [{k: (round(v, 3) if isinstance(v, float) else v)
                        for k, v in _reading(r).items()} for r in rows],
            "evidence": _json(latest["evidence"]) if latest else {}}


async def field_summary(city_id: str = "pune") -> list[dict]:
    """For the copilot: every node's latest scores and what drove them."""
    data = await overview(city_id, minutes=15)
    out = []
    for n in data["nodes"]:
        out.append({
            "node": n["id"], "kind": n["kind_label"], "label": n["label"],
            "online": n["online"], "age_s": n["age_s"],
            "ward_id": n["ward_id"], "lat": n["lat"], "lon": n["lon"],
            "simulated": n["simulated"],
            "human": n["human"], "structural": n["structural"],
            "environmental": n["environmental"], "overall": n["overall"],
            "flags": [f for f in (n["flags"] or []) if f not in ("learning",)],
            "evidence": {k: v for k, v in (n["evidence"] or {}).items() if v and v >= 0.2},
            "raw": {k: n[k] for k in ("mq2", "mq135", "temp_c", "tilt_deg", "mic",
                                      "piezo", "knocks")},
        })
    return out


# ------------------------------------------------------------ event cues ---
# The demo runner cues a sensor event on a node near what it is reporting, so
# the node's readings rise with the scenario. The command-centre LoRa link pulls
# these (GET /iot/commands) and runs them on its own Uno or sends them over LoRa.
# One API worker (see Dockerfile), so an in-process queue is enough.
_cues: list[dict] = []
_last_cue: dict[str, float] = {}
CUE_FOR = {"power_line": "f", "heat_casualty": "f", "fire": "f", "smoke": "f",
           "structural_damage": "c", "fallen_tree": "c", "collapse": "c",
           "person_stranded": "t", "trapped": "t"}
CUE_GAP_S = 75


_vcues: list[dict] = []     # cues for the virtual fleet, which runs in this process


def cue(node: str, event: str) -> bool:
    now = time.time()
    if event != "0" and now - _last_cue.get(node, 0) < CUE_GAP_S:
        return False
    if not kinds.can_cue(node, event):
        return False
    _last_cue[node] = now
    q = _vcues if kinds.is_virtual(node) else _cues
    q.append({"node": node, "event": event, "at": now})
    del q[:-20]
    return True


def take_cues() -> list[dict]:
    """Cues for the real nodes, pulled by the command-centre LoRa link."""
    now = time.time()
    out = [c for c in _cues if now - c["at"] < 120]
    _cues.clear()
    return [{"node": c["node"], "event": c["event"]} for c in out]


def take_virtual_cues() -> list[dict]:
    now = time.time()
    out = [c for c in _vcues if now - c["at"] < 120]
    _vcues.clear()
    return [{"node": c["node"], "event": c["event"]} for c in out]


async def online_nodes(city_id: str, region: str | None = None) -> list[dict]:
    from app import regions
    rows = await db.fetch(
        f"""select id, lat, lon from sensor_nodes
             where city_id = $1 and lat is not null and lon is not null
               and last_seen > now() - interval '{LIVE_S * 2} seconds'""",
        city_id,
    )
    return [dict(r) for r in rows
            if region is None or regions.region_of(r["lon"], r["lat"]) == region]


async def cue_near(category: str, lng: float, lat: float, *, city_id: str,
                   region: str | None = None, within_km: float = 6.0) -> str | None:
    """Cue the nearest online node if the report is close enough to it."""
    import math
    code = CUE_FOR.get(category)
    if not code:
        return None
    best, best_km = None, within_km
    for n in await online_nodes(city_id, region):
        if not kinds.can_cue(n["id"], code):
            continue        # a gas sentinel cannot hear tapping
        km = 111.0 * math.hypot(n["lat"] - lat, (n["lon"] - lng) * math.cos(math.radians(lat)))
        if km <= best_km:
            best, best_km = n["id"], km
    return best if best and cue(best, code) else None


async def cue_any(city_id: str, region: str | None, rng) -> tuple[str, str] | None:
    """A sensor-led event on some online node, for the scenario's own rhythm."""
    nodes = await online_nodes(city_id, region)
    if not nodes:
        return None
    node = rng.choice(nodes)["id"]
    code = rng.choice(kinds.KINDS[kinds.kind_of(node)]["cues"])
    return (node, code) if cue(node, code) else None


# ------------------------------------------------------- for orchestration ---
#: Which sensor score speaks to which hazard model, and how much it may lift a
#: ward's modelled score. Flood has no water sensor in the field: the overall
#: score is weak evidence there.
HAZARD_SENSOR = {"fire": ("environmental", 0.45), "wildfire": ("environmental", 0.45),
                 "air": ("environmental", 0.4), "air_quality": ("environmental", 0.4),
                 "heat": ("environmental", 0.3), "heatwave": ("environmental", 0.3),
                 "seismic": ("structural", 0.45), "earthquake": ("structural", 0.45),
                 "flood": ("overall", 0.15)}


async def ward_field(city_id: str = "pune") -> dict[str, dict]:
    """Per ward, the strongest live sensor scores and which node gave them."""
    rows = await db.fetch(
        f"""
        select n.id, n.ward_id, n.simulated, n.state -> 'latest' latest
          from sensor_nodes n
         where n.city_id = $1 and n.ward_id is not null
           and n.last_seen > now() - interval '{LIVE_S} seconds'
        """,
        city_id,
    )
    out: dict[str, dict] = {}
    for r in rows:
        latest = _json(r["latest"])
        w = out.setdefault(r["ward_id"], {"nodes": 0, "human": 0.0, "structural": 0.0,
                                          "environmental": 0.0, "overall": 0.0, "top": None,
                                          "flags": set()})
        w["nodes"] += 1
        for k in ("human", "structural", "environmental", "overall"):
            v = float(latest.get(k) or 0.0)
            if v > w[k] or (k == "overall" and w["top"] is None):
                w[k] = max(v, w[k])
                if k == "overall":
                    w["top"] = r["id"]
        w["flags"].update(f for f in (latest.get("flags") or [])
                          if f not in ("learning", "warming_up"))
    for w in out.values():
        w["flags"] = sorted(w["flags"])
    return out


def hazard_lift(hazard_id: str, field_w: dict | None) -> tuple[float, str] | None:
    """(how much to add to a 0..1 ward score, why) for one ward, or None."""
    if not field_w:
        return None
    key = next((k for k in HAZARD_SENSOR if k in hazard_id.lower()), None)
    if key is None:
        return None
    metric, weight = HAZARD_SENSOR[key]
    v = float(field_w.get(metric) or 0.0)
    if v < 0.3:
        return None
    flags = ", ".join(field_w.get("flags") or []) or "rising readings"
    return weight * v, (f"{field_w['nodes']} live field sensor(s); {metric} {v:.0%} "
                        f"at {field_w.get('top')} ({flags})")


# ---------------------------------------------------------------- bulk ingest ---
def _prepare(obs: dict) -> dict | None:
    node = str(obs.get("node") or "").strip()[:40]
    if not node:
        return None
    lat, lon = _f(obs.get("lat")), _f(obs.get("lon"))
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        lat = lon = None
    stamp = obs.get("received_at") or obs.get("receivedAt")
    return {"node": node, "obs": obs, "lat": lat, "lon": lon,
            "observed": _when(stamp), "stamped": bool(stamp),
            "seq": _int(obs.get("seq")), "up": _int(obs.get("up")),
            "sim_mask": _int(obs.get("sim")),
            "simulated": bool(obs.get("virtual")) and kinds.is_virtual(node),
            "label": str(obs["label"])[:80] if obs.get("label") else None}


async def ingest_bulk(observations: list[dict], *, gateway_id: str, city_id: str = "pune") -> dict:
    """The same pipeline as `ingest`, for a batch, in a fixed number of round
    trips instead of about five per reading.

    Same rules: duplicate (node, seq, uptime) readings are dropped, every
    reading is scored against its node's running baseline *in order*, the
    node's state is carried from one reading to the next, and escalations go
    through the mesh intake exactly as before. What changes is only the shape
    of the database work:

        1 upsert of every distinct node      (unnest, one statement)
        1 duplicate lookup for the batch     (unnest join)
        1 ward lookup per node that moved    (not per reading)
        1 pipelined insert of all readings   (executemany)
        1 pipelined update of all nodes      (executemany)

    Results come back in input order, one dict per observation.
    """
    from app.mesh import service as mesh

    prepared = [_prepare(o) for o in observations]
    results: list[dict] = [{"error": "no node"} if p is None else {} for p in prepared]
    valid = [(i, p) for i, p in enumerate(prepared) if p is not None]
    if not valid:
        return {"accepted": 0, "errors": len(results), "duplicates": 0, "results": results}

    # Readings without their own timestamp arrived "now": one instant for the
    # batch, so the order within it is the node's sequence number, not the
    # microsecond at which this loop happened to look at each one.
    batch_now = datetime.now(timezone.utc)
    for _, p in valid:
        if not p["stamped"]:
            p["observed"] = batch_now
    by_node: dict[str, list[tuple[int, dict]]] = {}
    for i, p in valid:
        by_node.setdefault(p["node"], []).append((i, p))
    for items in by_node.values():
        items.sort(key=lambda ip: (ip[1]["observed"], ip[1]["seq"] if ip[1]["seq"] is not None else -1))

    nodes = list(by_node)
    last_loc = {n: next(((p["lat"], p["lon"]) for _, p in reversed(by_node[n]) if p["lat"] is not None),
                        (None, None)) for n in nodes}
    labels = {n: next((p["label"] for _, p in by_node[n] if p["label"]), None) for n in nodes}
    sim = {n: any(p["simulated"] for _, p in by_node[n]) for n in nodes}

    readings: list[tuple] = []
    node_updates: list[tuple] = []
    touches: list[tuple] = []
    escalate: list[tuple[str, dict, float, float, Any]] = []
    dups = 0
    async with db.transaction() as conn:
        rows = await conn.fetch(
            """
            insert into sensor_nodes (id, city_id, label, lat, lon, gateway_id, simulated)
            select k.id, $1, coalesce(k.label, k.id), k.lat, k.lon, $2, k.sim
              from unnest($3::text[], $4::text[], $5::float8[], $6::float8[], $7::bool[])
                   as k(id, label, lat, lon, sim)
            on conflict (id) do update
               set lat = coalesce(excluded.lat, sensor_nodes.lat),
                   lon = coalesce(excluded.lon, sensor_nodes.lon),
                   gateway_id = excluded.gateway_id,
                   simulated = excluded.simulated,
                   label = coalesce(nullif(excluded.label, excluded.id), sensor_nodes.label)
            returning id, lat, lon, ward_id, last_seq, baseline, state, escalated, (xmax = 0) as created
            """,
            city_id, gateway_id, nodes, [labels[n] for n in nodes],
            [last_loc[n][0] for n in nodes], [last_loc[n][1] for n in nodes], [sim[n] for n in nodes],
        )
        node_row = {r["id"]: r for r in rows}

        keyed = [(p["node"], p["seq"], p["up"]) for _, p in valid if p["seq"] is not None and p["up"] is not None]
        seen_db: set[tuple] = set()
        if keyed:
            found = await conn.fetch(
                """
                select r.node_id, r.seq, r.uptime_s from sensor_readings r
                  join unnest($1::text[], $2::bigint[], $3::bigint[]) as k(n, s, u)
                    on r.node_id = k.n and r.seq = k.s and r.uptime_s = k.u
                 where r.observed_at > now() - interval '1 hour'
                """,
                [k[0] for k in keyed], [k[1] for k in keyed], [k[2] for k in keyed],
            )
            seen_db = {(f["node_id"], f["seq"], f["uptime_s"]) for f in found}

        for n in nodes:
            row = node_row.get(n)
            if row is None:
                continue
            lat, lon, ward_id = row["lat"], row["lon"], row["ward_id"]
            moved = any(p["obs"].get("lat") is not None for _, p in by_node[n])
            if lat is not None and lon is not None and (ward_id is None or moved):
                ward_id = await mesh.ward_at(lon, lat, city_id)
            baseline, state = _json(row["baseline"]), _json(row["state"])
            last_esc = _json(row["escalated"]) or {}
            last_seq, lost_total, count, last_obs, newest = row["last_seq"], 0, 0, None, None
            for i, p in by_node[n]:
                key = (n, p["seq"], p["up"])
                if p["seq"] is not None and p["up"] is not None and key in seen_db:
                    results[i] = {"node": n, "duplicate": True}
                    dups += 1
                    continue
                seen_db.add(key)
                obs = p["obs"]
                s = fusion.score(obs, baseline, state)
                baseline = s.baseline
                lost = 0
                try:
                    if last_seq is not None and p["seq"] is not None and p["seq"] > last_seq + 1:
                        lost = p["seq"] - last_seq - 1
                except TypeError:
                    pass
                found_esc = fusion.escalations(s, s.state, p["observed"].timestamp(), last_esc)
                flags = list(s.flags) + (["packet_loss"] if lost else [])
                flags += [f"escalated:{e['kind']}" for e in found_esc]
                readings.append((
                    n, city_id, p["observed"], p["seq"], p["up"], lat, lon,
                    *[_f(obs.get(c)) if c not in ("knocks", "tilt_sw", "pir") else _int(obs.get(c))
                      for c in _COLS],
                    _int(obs.get("rssi")), _f(obs.get("snr")),
                    s.human, s.structural, s.environmental, s.overall, s.evidence, flags,
                    (str(obs.get("raw")) if obs.get("raw") else None),
                    {"sim_mask": p["sim_mask"]} if p["sim_mask"] else {},
                ))
                state = s.state | {"latest": s.as_dict() | {"at": p["observed"].isoformat()}}
                for e in found_esc:
                    last_esc[e["kind"]] = p["observed"].timestamp()
                    if lat is not None and lon is not None:
                        escalate.append((n, e, lat, lon, s))
                if p["seq"] is not None:
                    last_seq = max(last_seq or 0, p["seq"])
                lost_total += lost
                count += 1
                last_obs, newest = obs, p["observed"] if newest is None else max(newest, p["observed"])
                results[i] = {"node": n, **s.as_dict(), "lost": lost,
                              "escalated": [e["kind"] for e in found_esc] or None}
            if count:
                node_updates.append((n, ward_id, newest, last_seq, lost_total, count,
                                     _int(last_obs.get("rssi")), _f(last_obs.get("snr")),
                                     baseline, state, last_esc))
                touches.append((n, lat, lon, ward_id, s, last_obs))
            if row["created"]:
                log.info("iot_node_joined", node=n, kind=kinds.kind_of(n), simulated=sim[n])

        if readings:
            await conn.executemany(
                """
                insert into sensor_readings
                  (node_id, city_id, observed_at, seq, uptime_s, lat, lon,
                   mq2, mq135, temp_c, tilt_deg, gyro_dps, vib_g, mic, piezo, knocks, tilt_sw, pir,
                   rssi, snr, human, structural, environmental, overall, evidence, flags, raw, extra)
                values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,
                        $19,$20,$21,$22,$23,$24,$25,$26,$27,$28)
                """,
                readings,
            )
        if node_updates:
            await conn.executemany(
                """
                update sensor_nodes
                   set ward_id = $2, last_seen = greatest(last_seen, $3), readings = readings + $6,
                       last_seq = coalesce($4, last_seq), lost = lost + $5,
                       rssi = coalesce($7, rssi), snr = coalesce($8, snr),
                       baseline = $9, state = $10, escalated = $11
                 where id = $1
                """,
                node_updates,
            )

    for n, lat, lon, ward_id, s, obs in touches:
        await _touch_mesh_node(n, lat, lon, ward_id, s, obs)
    for n, e, lat, lon, s in escalate:
        await _escalate(n, e, lat, lon, s, city_id)
    await _touch_gateway(gateway_id)
    accepted = sum(1 for r in results if r and "error" not in r and not r.get("duplicate"))
    return {"accepted": accepted, "errors": sum(1 for r in results if "error" in r),
            "duplicates": dups, "results": results}
