"""The passability model, live: P(blocked at arrival) for every PCMC road, from
what the control room knows right now.

    risk = await current_risk()          # cached ~2 min
    pts  = await predicted_blocks()      # [(lng, lat, p)] the router should avoid

Evidence is built from the live database the same way training built it from
simulated observations (`ml.features.build`), so the model sees the same
inputs it learned on:

    citizen_reports (6 h)  -> citizen reports: kind from category, depth from the
                              photo's VLM reading, reliability = trust score;
                              reports from crews -> patrol sightings
    field_reports          -> patrol sightings ("route blocked" = blocked)
    road_blocks            -> official closures (binding), cleared -> reopen
    incidents resolved     -> "clear now" reports
    Open-Meteo forecast    -> rain observed so far + rain forecast (district point)
    GloFAS (Open-Meteo)    -> river stage per cell, by the simulator's own rating
    traffic                -> not available server-side: left missing (the model
                              was trained with missing traffic on most roads)

Only the PCMC district (ml.district.BBOX) is scored; elsewhere the router uses
reported blocks only, as before. Everything the router uses is logged to
nav_predictions for the re-learning loop.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

from app.core.logging import get_logger
from app.db import session as db

log = get_logger(__name__)

import os as _os
TTL_S = float(_os.environ.get("LIVE_RISK_TTL_S", "300"))
AVOID_P = 0.6
MAX_POINTS = 30
STEP_MIN = 30
HISTORY_H = 48

CATEGORY_KIND = {
    "flooded_road": "flood", "waterlogging": "flood", "blocked_drain": "flood", "person_stranded": "flood",
    "fallen_tree": "tree", "power_line": "wire", "structural_damage": "collapse", "earthquake_damage": "collapse",
    "fire": "fire", "air_quality": "gas",
}
DEPTH_BAND = {"none": 0, "ankle": 1, "shin": 1, "knee": 2, "thigh": 3, "waist": 3, "chest": 3, "above": 3}


@dataclass
class Risk:
    at: float
    model: str
    seg_index: np.ndarray
    p: np.ndarray
    sd: np.ndarray
    lon: np.ndarray
    lat: np.ndarray
    seg_id: np.ndarray
    why: list[str]
    features: pd.DataFrame
    notes: list[str]


_cache: Risk | None = None
_lock = asyncio.Lock()
# The evidence and candidate roads behind the last score, so other unit classes
# and horizons are scored on exactly the same picture (arrival-time view).
_basis: tuple | None = None
_arrival: dict[tuple, dict] = {}


def _fetch_json(url: str, params: dict, timeout: float = 8.0) -> dict | None:
    try:
        q = url + "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(urllib.request.Request(q, headers={"User-Agent": "DisruptionOps/1.0"}),
                                    timeout=timeout) as r:
            return json.load(r)
    except Exception as exc:  # noqa: BLE001 - weather is an input, not a dependency
        log.warning("live_weather_failed", url=url, error=str(exc)[:160])
        return None


def _weather(start: datetime, steps: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    """Rain (district, upstream) per 30-min step from `start`, plus the future for the forecast."""
    from ml.district import WEATHER_POINTS
    notes = []
    out = []
    for name in ("district", "maval_upstream"):
        lon, lat = WEATHER_POINTS[name]
        d = _fetch_json("https://api.open-meteo.com/v1/forecast",
                        {"latitude": lat, "longitude": lon, "hourly": "precipitation", "past_days": 2,
                         "forecast_days": 1, "timezone": "Asia/Kolkata"})
        series = np.zeros(steps + 8, dtype=np.float32)
        if d and "hourly" in d:
            h = pd.Series(d["hourly"]["precipitation"], index=pd.to_datetime(d["hourly"]["time"])).fillna(0)
            idx = pd.date_range(start.replace(tzinfo=None), periods=steps + 8, freq="30min")
            series = h.reindex(idx.floor("h")).fillna(0).to_numpy(np.float32)
        else:
            notes.append(f"no live rain for {name}; assumed dry")
        out.append(series)
    return out[0][:steps], out[1][:steps], out[0], notes


def _stages() -> tuple[dict[str, float], list[str]]:
    """Today's river stage per GloFAS cell, by the simulator's rating curve."""
    from ml.district import RIVER_POINTS, SIM
    from ml.sim.static import load_static
    notes = []
    try:
        stats = json.loads((SIM / "river_stats.json").read_text())
    except OSError:
        return {}, ["no river stats; river stage missing"]
    st = load_static()
    out = {}
    cells = list(RIVER_POINTS)
    for k, c in enumerate(cells):
        lon, lat = RIVER_POINTS[c]
        d = _fetch_json("https://flood-api.open-meteo.com/v1/flood",
                        {"latitude": lat, "longitude": lon, "daily": "river_discharge", "past_days": 1,
                         "forecast_days": 1, "models": "seamless_v4"})
        if not d or "daily" not in d:
            notes.append(f"no live discharge for {c}")
            continue
        q = [v for v in d["daily"]["river_discharge"] if v is not None]
        if not q:
            continue
        Q = float(q[-1])
        bank = float(st.loc[st["cell"] == k, "bank_m"].median()) if (st["cell"] == k).any() else 7.0
        qbf, qtop = stats[c]["q_bf"], stats[c]["q_top"]
        if Q <= qbf:
            stage = bank * (max(Q, 0) / qbf) ** 0.5
        else:
            stage = bank + 3.0 * ((Q - qbf) / max(qtop - qbf, 1)) ** 0.6
        out[c] = round(stage, 3)
    return out, notes


async def _evidence(now: datetime):
    """Evidence (ml.features) from the live database for the district."""
    from ml.district import BBOX
    from ml.features import Evidence
    from ml.sim.static import TO_UTM, load_static
    from scipy.spatial import cKDTree

    st = load_static()
    kd = cKDTree(st[["x", "y"]].to_numpy())
    start = (now - timedelta(hours=HISTORY_H)).replace(second=0, microsecond=0)
    start = start - timedelta(minutes=start.minute % STEP_MIN)
    steps = HISTORY_H * 60 // STEP_MIN + 1
    w, s, e, n = BBOX
    env = (w, s, e, n)

    def tmin(ts: datetime) -> int:
        return int((ts - start).total_seconds() // 60)

    rows: list[dict] = []

    def put(source, kind, ts, lng, lat, depth_class=-1, blocked=np.nan, depth_est=np.nan, rel=0.6):
        x, y = TO_UTM.transform(lng, lat)
        rows.append({"source": source, "kind": kind, "t_obs_min": tmin(ts), "x": x, "y": y,
                     "seg_rep": int(kd.query([x, y])[1]), "depth_class": depth_class, "depth_est_m": depth_est,
                     "blocked_est": blocked, "reliability": rel})

    reps = await db.fetch(
        """
        select r.created_at, r.category, r.source, r.trust_score, r.photo_evidence,
               extensions.ST_X(r.location::extensions.geometry) lng, extensions.ST_Y(r.location::extensions.geometry) lat
          from citizen_reports r
         where r.created_at > $1 and r.sim_run_id is null
           and extensions.ST_X(r.location::extensions.geometry) between $2 and $4
           and extensions.ST_Y(r.location::extensions.geometry) between $3 and $5
        """, now - timedelta(hours=6), *env)
    for r in reps:
        kind = CATEGORY_KIND.get(r["category"])
        if not kind:
            continue
        pe = r["photo_evidence"]
        pe = json.loads(pe) if isinstance(pe, str) else (pe or {})
        band = ((pe.get("water") or {}).get("depthBand") or "").lower()
        cls = DEPTH_BAND.get(band, 2 if kind == "flood" else -1)
        rel = float(r["trust_score"]) if r["trust_score"] is not None else 0.6
        if (r["source"] or "").startswith(("field", "crew")):
            put("patrol", "status", r["created_at"], r["lng"], r["lat"], cls, 1.0, np.nan, rel)
        else:
            put("citizen", kind, r["created_at"], r["lng"], r["lat"], cls, np.nan, np.nan, rel)

    frs = await db.fetch(
        """
        select created_at, status_kind, extensions.ST_X(location::extensions.geometry) lng,
               extensions.ST_Y(location::extensions.geometry) lat
          from field_reports
         where created_at > $1 and location is not null
           and extensions.ST_X(location::extensions.geometry) between $2 and $4
           and extensions.ST_Y(location::extensions.geometry) between $3 and $5
        """, now - timedelta(hours=6), *env)
    for r in frs:
        put("patrol", "status", r["created_at"], r["lng"], r["lat"], -1,
            1.0 if r["status_kind"] == "route_blocked" else 0.0, np.nan, 0.95)

    blocks = await db.fetch(
        """
        select created_at, active, extensions.ST_X(location::extensions.geometry) lng,
               extensions.ST_Y(location::extensions.geometry) lat
          from road_blocks
         where (active or created_at > $1)
           and extensions.ST_X(location::extensions.geometry) between $2 and $4
           and extensions.ST_Y(location::extensions.geometry) between $3 and $5
        """, now - timedelta(hours=24), *env)
    for r in blocks:
        put("official", "closure", max(r["created_at"], start), r["lng"], r["lat"], rel=1.0)
        if not r["active"]:
            put("official", "reopen", now - timedelta(minutes=1), r["lng"], r["lat"], rel=1.0)

    res = await db.fetch(
        """
        select updated_at, extensions.ST_X(location::extensions.geometry) lng,
               extensions.ST_Y(location::extensions.geometry) lat
          from incidents
         where status = 'resolved' and updated_at > $1 and sim_run_id is null
           and category = any($6::text[])
           and extensions.ST_X(location::extensions.geometry) between $2 and $4
           and extensions.ST_Y(location::extensions.geometry) between $3 and $5
        """, now - timedelta(hours=3), *env, list(CATEGORY_KIND))
    for r in res:
        put("citizen", "clear", r["updated_at"], r["lng"], r["lat"], 0, rel=0.9)

    rain, up, rain_all, notes = await asyncio.to_thread(_weather, start, steps)
    stages, n2 = await asyncio.to_thread(_stages)
    notes += n2
    sens = [{"source": "gauge", "site": c, "seg": -1, "t_min": (steps - 1) * STEP_MIN, "value_m": v}
            for c, v in stages.items()]
    rep_df = pd.DataFrame(rows, columns=["source", "kind", "t_obs_min", "x", "y", "seg_rep", "depth_class",
                                         "depth_est_m", "blocked_est", "reliability"])
    ev = Evidence(rep_df, pd.DataFrame(sens, columns=["source", "site", "seg", "t_min", "value_m"]),
                  np.zeros((0, 0), np.uint8), np.zeros(0, np.int32), rain, up, rain_future=rain_all,
                  start=pd.Timestamp(start.astimezone(timezone(timedelta(hours=5, minutes=30))).replace(tzinfo=None)))
    notes.append(f"{len(rep_df)} reports/sightings/closures in the last 6 h; "
                 f"rain last 3 h {float(rain[-6:].sum() / 2):.1f} mm; gauges {len(stages)}/4")
    return ev, (steps - 1) * STEP_MIN, notes


def _candidates(ev) -> np.ndarray:
    from ml.sim.static import load_static
    st = load_static()
    # Score the roads that can matter, not all 42k: the ones near anything
    # reported, sighted or closed in the last 6 h, plus the ones the terrain makes
    # risky (underpasses, low spots, low ground by the river, causeways and
    # bridges). The rest have no evidence and no exposure, and the model puts
    # them near zero anyway; skipping them keeps a small API instance fast.
    drv = st["drivable"].to_numpy()
    risky = (st["underpass"].to_numpy() | (st["pond_m"].to_numpy() > 1.0) | (st["hand_m"].to_numpy() < 6)
             | st["low_bridge"].to_numpy() | st["river_bridge"].to_numpy() | (st["dist_river_m"].to_numpy() < 150))
    near = np.zeros(len(st), bool)
    if len(ev.reports):
        from scipy.spatial import cKDTree
        kd = cKDTree(st[["x", "y"]].to_numpy())
        hits = kd.query_ball_point(ev.reports[["x", "y"]].to_numpy(), r=600)
        idx = [i for h in hits for i in h]
        if idx:
            near[np.unique(idx)] = True
    return np.flatnonzero(drv & (risky | near))


_scorer = None


def _score_for(ev, t0_min: int, cand: np.ndarray, profile: str = "ambulance", horizon: int = 30):
    """P(blocked at arrival) on `cand` for one unit class and horizon."""
    global _scorer
    from app.nav.passability import Scorer
    from app.nav.profiles import PROFILES
    from ml.features import PROFILE_CODES, build
    ev.traffic = np.full((t0_min // STEP_MIN + 1, 0), 255, np.uint8)     # no live traffic feed server-side
    ev.traffic_segs = np.zeros(0, np.int32)
    ev._trafpos = {}
    F = build(ev, t0_min, cand, np.full(len(cand), horizon), np.full(len(cand), PROFILE_CODES[profile]),
              np.full(len(cand), PROFILES[profile].depth_limit_m))
    F["traffic_level"] = np.nan
    F["traffic_nodata"] = 0
    if _scorer is None:
        _scorer = Scorer.load()
    p, sd, why = _scorer.score(F)
    return _scorer.version, p, sd, why, F


def _score(ev, t0_min: int):
    global _scorer
    _scorer = None                      # pick up a newly registered champion on each refresh
    cand = _candidates(ev)
    version, p, sd, why, F = _score_for(ev, t0_min, cand)
    return version, cand, p, sd, why, F


async def current_risk(force: bool = False) -> Risk | None:
    global _cache
    if not force and _cache and time.time() - _cache.at < TTL_S:
        return _cache
    async with _lock:
        if not force and _cache and time.time() - _cache.at < TTL_S:
            return _cache
        try:
            now = datetime.now(timezone.utc)
            ev, t0, notes = await _evidence(now)
            version, cand, p, sd, why, F = await asyncio.to_thread(_score, ev, t0)
        except Exception as exc:  # noqa: BLE001 - no model, no data: route on reports only
            log.warning("live_risk_failed", error=str(exc)[:300])
            return _cache
        from ml.sim.static import load_static
        st = load_static()
        _cache = Risk(time.time(), version, cand, p, sd, st["lon"].to_numpy()[cand], st["lat"].to_numpy()[cand],
                      st["seg_id"].to_numpy()[cand], why, F, notes)
        global _basis
        _basis = (ev, t0, cand, _cache.at)
        _arrival.clear()
        return _cache


ARRIVAL_PROFILES = ("ambulance", "fire_engine", "resident")
ARRIVAL_HORIZONS = (30, 60, 90)


async def arrival(profile: str = "ambulance", horizon: int = 30) -> dict | None:
    """The same live picture scored for another unit class and arrival horizon:
    per road, P(blocked when this unit gets there), the ensemble's spread, and
    whether the current-status rule calls it blocked right now. Cached until the
    next refresh of the live evidence."""
    r = await current_risk()
    if r is None or _basis is None:
        return None
    ev, t0, cand, at = _basis
    key = (profile, int(horizon), at)
    hit = _arrival.get(key)
    if hit is not None:
        return hit
    if profile == "ambulance" and int(horizon) == 30:
        p, sd, why, F = r.p, r.sd, r.why, r.features
    else:
        async with _lock:
            _, p, sd, why, F = await asyncio.to_thread(_score_for, ev, t0, cand, profile, int(horizon))
    out = {"p": p, "sd": sd, "why": why, "now": F["status_now"].to_numpy().astype(bool), "at": at,
           "model": r.model, "notes": r.notes, "cand": cand}
    _arrival[key] = out
    if len(_arrival) > 12:
        _arrival.pop(next(iter(_arrival)))
    return out


async def predicted_blocks(min_p: float = AVOID_P, limit: int = MAX_POINTS) -> list[tuple[float, float, float]]:
    """Road points the model expects blocked for an ambulance within 30 min, strongest first,
    thinned to one per ~150 m (the router takes at most 50 avoid points in all)."""
    r = await current_risk()
    if r is None:
        return []
    # Uncertainty counts: a road the ensemble disagrees about is avoided when its
    # pessimistic estimate (p + 2 sd of the members) crosses the line and p is at
    # least 2/3 of it - better a short detour than a crew turned back.
    upper = np.minimum(1.0, r.p + 2 * r.sd)
    idx = np.flatnonzero((r.p >= min_p) | ((upper >= min_p) & (r.p >= min_p * 2 / 3)))
    idx = idx[np.argsort(-upper[idx])]
    out: list[tuple[float, float, float]] = []
    for i in idx:
        lo, la = float(r.lon[i]), float(r.lat[i])
        if all((lo - a) ** 2 * 105.5e3 ** 2 + (la - b) ** 2 * 110.9e3 ** 2 > 150 ** 2 for a, b, _ in out):
            out.append((lo, la, float(r.p[i])))
        if len(out) >= limit:
            break
    return out


async def log_used(points: list[tuple[float, float, float]], used_for: str = "route") -> None:
    """What the router used goes to nav_predictions (031) for the re-learning loop."""
    r = _cache
    if r is None or not points:
        return
    try:
        from app.nav.passability import log_predictions
        pos = {(round(float(a), 6), round(float(b), 6)): k for k, (a, b) in enumerate(zip(r.lon, r.lat))}
        rows = []
        for lo, la, p in points:
            k = pos.get((round(lo, 6), round(la, 6)))
            if k is None:
                continue
            feats = {c: (None if pd.isna(v) else float(v)) for c, v in r.features.iloc[k].items()}
            rows.append({"seg_id": str(r.seg_id[k]), "lon": lo, "lat": la, "profile": "ambulance", "horizon_min": 30,
                         "p": p, "sd": float(r.sd[k]), "features": feats, "model_version": r.model, "used_for": used_for})
        async with db.transaction() as conn:
            await log_predictions(conn, rows)
    except Exception as exc:  # noqa: BLE001 - logging never blocks routing
        log.warning("log_predictions_failed", error=str(exc)[:200])


def arrival_geojson(a: dict, min_p: float = 0.1) -> dict[str, Any]:
    """Roads with P(blocked at arrival) >= min_p, or blocked now by the current-status
    rule: p, its 2-sd band, the reason, and how the router would treat it."""
    from ml.sim.static import load_static
    st = load_static()
    coords = pd.read_parquet(
        __import__("ml.district", fromlist=["PROCESSED"]).PROCESSED / "graph" / "segments.parquet",
        columns=["coords"])["coords"].to_numpy()
    p, sd, now, cand = a["p"], a["sd"], a["now"], a["cand"]
    upper = np.minimum(1.0, p + 2 * sd)
    avoid = (p >= AVOID_P) | ((upper >= AVOID_P) & (p >= AVOID_P * 2 / 3))
    idx = np.flatnonzero((p >= min_p) | now)
    feats = [{"type": "Feature",
              "properties": {"seg": str(st["seg_id"].iloc[cand[i]]), "p": round(float(p[i]), 3),
                             "sd": round(float(sd[i]), 3), "lo": round(max(0.0, float(p[i] - 2 * sd[i])), 3),
                             "hi": round(float(upper[i]), 3), "why": a["why"][i], "now": bool(now[i]),
                             "avoid": bool(avoid[i]), "underpass": bool(st["underpass"].iloc[cand[i]]),
                             "bridge": bool(st["bridge"].iloc[cand[i]])},
              "geometry": {"type": "LineString", "coordinates": json.loads(coords[cand[i]])}}
             for i in idx]
    return {"type": "FeatureCollection", "features": feats}


def as_geojson(r: Risk, min_p: float = 0.3) -> dict[str, Any]:
    from ml.sim.static import load_static
    st = load_static()
    coords = pd.read_parquet(
        __import__("ml.district", fromlist=["PROCESSED"]).PROCESSED / "graph" / "segments.parquet",
        columns=["coords"])["coords"].to_numpy()
    idx = np.flatnonzero(r.p >= min_p)
    feats = [{"type": "Feature",
              "properties": {"seg": str(r.seg_id[i]), "p": round(float(r.p[i]), 3), "sd": round(float(r.sd[i]), 3),
                             "why": r.why[i], "avoid": bool(r.p[i] >= AVOID_P),
                             "underpass": bool(st["underpass"].iloc[r.seg_index[i]])},
              "geometry": {"type": "LineString", "coordinates": json.loads(coords[r.seg_index[i]])}}
             for i in idx]
    return {"type": "FeatureCollection", "features": feats}
