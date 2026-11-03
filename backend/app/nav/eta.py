"""Live response ETA from the trained quantile model (ml/train_eta.py).

The router says how long the road takes on a normal day. The ETA model says how
long the *response* takes during this flood: it was trained on trips driven
through simulated floods (turn-backs, replans, on-foot last stretches), with the
passability model's risk along the route as a feature - so it is how Addition 2
feeds the ETA. It returns a P50 (for planning) and a P90 (for promises: "18 min,
90% within 24").

    eta.for_route(coords, kind, router_minutes)  -> {"p50": .., "p90": .., ...} | None

Features are rebuilt from what the server has: the route geometry snapped to
the road graph (length, free-flow time, signals, underpasses, bridges), the live
risk cache (P(blocked) along the route, flood reports, rain, river stage) and
the hour. The live server has no traffic feed, so traffic is "no data" - the
model was trained with most roads in that state. Cached per route for a minute.
Never raises: no model or no data means no ETA, and callers keep the router's.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from app.core.logging import get_logger
from app.nav.passability import MODELS

log = get_logger(__name__)

_model = None
_meta: dict = {}
_tree = None
_cache: dict[str, tuple[float, dict | None]] = {}
TTL_S = 60.0
SNAP_M = 35.0


def _load():
    global _model, _meta
    if _model is not None:
        return _model
    try:
        import xgboost as xgb
        reg = json.loads((MODELS / "registry.json").read_text())
        ver = (reg.get("eta") or {}).get("champion")
        d = MODELS / "eta" / ver
        _meta = json.loads((d / "meta.json").read_text())
        b = xgb.Booster()
        b.load_model(str(d / "model.json"))
        _model = b
        log.info("eta_model_loaded", version=ver)
    except Exception as exc:  # noqa: BLE001
        log.warning("eta_model_unavailable", error=str(exc)[:200])
        _model = False
    return _model


def _graph():
    global _tree
    if _tree is None:
        from scipy.spatial import cKDTree
        from ml.sim.static import load_static
        st = load_static()
        xy = np.c_[(st["lon"].to_numpy() - 73.77) * 105_500, (st["lat"].to_numpy() - 18.63) * 110_574]
        _tree = (cKDTree(xy), st)
    return _tree


def _segments(coords: list[list[float]]) -> np.ndarray:
    """Route polyline -> graph segments it runs along (in order, unique)."""
    tree, _ = _graph()
    pts = []
    for (x0, y0), (x1, y1) in zip(coords[:-1], coords[1:]):
        a = np.array([(x0 - 73.77) * 105_500, (y0 - 18.63) * 110_574])
        b = np.array([(x1 - 73.77) * 105_500, (y1 - 18.63) * 110_574])
        n = max(1, int(np.hypot(*(b - a)) // 25))
        for k in range(n):
            pts.append(a + (b - a) * k / n)
    if not pts:
        return np.zeros(0, int)
    d, i = tree.query(np.asarray(pts), distance_upper_bound=SNAP_M)
    i = i[np.isfinite(d)]
    _, first = np.unique(i, return_index=True)
    return i[np.sort(first)]


def _features(coords, kind: str, router_minutes: float | None) -> dict | None:
    from app.nav import live
    from app.nav.profiles import PROFILES
    _, st = _graph()
    segs = _segments(coords)
    if len(segs) < 2:
        return None
    risk = live._cache
    p_map: dict[int, float] = {}
    rep, rain1, rainfc, stage = 0.0, 0.0, 0.0, 0.0
    if risk is not None and len(risk.seg_index):
        pos = {int(s): k for k, s in enumerate(risk.seg_index)}
        F = risk.features
        hit = [pos[s] for s in segs if s in pos]
        for s in segs:
            k = pos.get(int(s))
            if k is not None:
                p_map[int(s)] = float(risk.p[k])
        if hit:
            rep = float(np.nansum(F["flood_150_3h"].to_numpy()[hit]))
            stage = float(np.nanmax(F["stage_frac"].to_numpy()[hit])) if np.isfinite(F["stage_frac"].to_numpy()[hit]).any() else 0.0
        if len(F):
            rain1 = float(F["rain_1h"].iloc[0])
            rainfc = float(F["rain_fc"].iloc[0]) if "rain_fc" in F else 0.0
    p = np.array([p_map.get(int(s), 0.0) for s in segs])
    length = st["length_m"].to_numpy()[segs]
    prof = PROFILES.get(kind) or PROFILES["police"]
    free_s = length / (np.maximum(st["free_kmh"].to_numpy()[segs], 5) * max(prof.speed_factor, 0.3) / 3.6)
    sig = st["signals"].to_numpy()[segs] if "signals" in st else np.zeros(len(segs))
    free_min = float(free_s.sum() + np.nansum(sig) * prof.signal_delay_s) / 60
    a, b = coords[0], coords[-1]
    crow = math.hypot((a[0] - b[0]) * 105.5, (a[1] - b[1]) * 110.9)
    hour = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).hour
    return {
        "plan_km": float(length.sum() / 1000), "plan_free_min": free_min,
        "plan_signals": float(np.nansum(sig)), "plan_underpasses": float(st["underpass"].to_numpy()[segs].sum()),
        "plan_bridges": float(st["bridge"].to_numpy()[segs].sum()), "plan_p_sum": float(p.sum()),
        "plan_p_max": float(p.max()) if len(p) else 0.0, "plan_traffic": np.nan, "plan_nodata": 1.0,
        "plan_flood_reports": rep, "rain_1h": rain1, "rain_fc": rainfc, "stage_frac_max": stage,
        "hour": hour, "unit": 1 if prof.depth_limit_m > 0.3 else 0, "crow_km": crow,
    }


def for_route(coords: list[list[float]] | None, kind: str, router_minutes: float | None = None,
              key: str | None = None) -> dict | None:
    """P50/P90 minutes for a unit of `kind` driving `coords` now (mobilisation included)."""
    if not coords or len(coords) < 2:
        return None
    if kind in ("helicopter", "drone", "boat"):
        return None                               # not road vehicles; the model is for road units
    ck = key or f"{kind}:{len(coords)}:{coords[0]}:{coords[-1]}"
    hit = _cache.get(ck)
    if hit and time.time() - hit[0] < TTL_S:
        return hit[1]
    out = None
    try:
        m = _load()
        if m:
            import pandas as pd
            import xgboost as xgb
            f = _features(coords, kind, router_minutes)
            if f is not None:
                X = pd.DataFrame([f])[_meta["features"]].astype(np.float32)
                q = m.predict(xgb.DMatrix(X), iteration_range=(0, int(_meta.get("best_iteration", 0)) + 1))
                q = np.asarray(q).reshape(-1)
                p50, p90 = float(q[0]), float(q[1]) + float(_meta.get("p90_offset_min", 0.0))
                out = {"p50": round(max(p50, 1.0), 1), "p90": round(max(p90, p50, 1.0), 1),
                       "model": _meta.get("version"), "riskMax": round(f["plan_p_max"], 3),
                       "routerMinutes": router_minutes}
    except Exception as exc:  # noqa: BLE001
        log.warning("eta_predict_failed", error=str(exc)[:200])
    _cache[ck] = (time.time(), out)
    if len(_cache) > 2000:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:1000]:
            _cache.pop(k, None)
    return out


def remaining(path, kind: str, router_min, progress, uid: str) -> dict | None:
    """The model ETA for what is left of an assignment's route (scaled by progress)."""
    try:
        coords = path if isinstance(path, list) else (json.loads(path) if path else None)
        e = for_route(coords, kind, router_min, key=f"{uid}:{len(coords or [])}:{(coords or [[0]])[-1]}")
        if not e:
            return None
        left = max(0.0, 1.0 - float(progress or 0))
        return {**e, "p50": round(e["p50"] * left, 1), "p90": round(e["p90"] * left, 1),
                "remainingShare": round(left, 2)}
    except Exception:  # noqa: BLE001
        return None
