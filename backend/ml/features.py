"""Decision-time features for "will this road still be usable when we get there?".

One function, `build`, used for training (from simulated evidence) and for
serving (from live evidence), so the model sees the same thing in both.

The rule that makes it honest: **only evidence observed at or before the
decision time is used** (reports by `t_obs_min`, sensors and gauges by
`t_min`, traffic by its step). Truth columns in simulated reports
(`truth_seg`, `is_false`, `is_stale`, `stale_of`, `t_true_min`) are dropped
when evidence is loaded and never reach this module. The rain forecast is the
real future (coarse, 25 km) rain with forecast error added, never the
simulator's fine rain field.

Evidence = what the command centre has:
    reports   source, kind, t_obs_min, x, y, seg_rep, depth_class, depth_est_m, blocked_est, reliability
    sensors   source (sensor|gauge), site, seg, t_min, value_m
    traffic   level [step, k] (0-3, 255 = no data) for segments `traffic_segs`
    weather   rain_mmph, upstream_mmph per 30-min step (district, coarse), and a
              forecast function rain over the next h minutes
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

from ml.sim.static import CELLS, load_static
from ml.sim.storms import STEP_MIN

#: Unit classes the model predicts for (label = blocked for that unit's rules).
PROFILE_CODES = {"ambulance": 0, "fire_engine": 1, "resident": 2}
HORIZONS_MIN = (30, 60, 90)
CLS_CODE = {"main": 0, "minor": 1, "local": 2, "path": 3}
OBS_COLUMNS = ["source", "kind", "t_obs_min", "x", "y", "seg_rep", "depth_class", "depth_est_m",
               "blocked_est", "reliability"]
HAZARD_KINDS = ("tree", "wire", "collapse", "landslide", "crash")

STATIC_FEATURES = ["hand_m", "dist_river_m", "dist_water_m", "sink_m", "pond_m", "slope_pct", "elev_m",
                   "underpass", "bridge", "river_bridge", "low_bridge", "cls_code", "length_m", "density",
                   "bank_m", "industrial", "free_kmh"]
DYNAMIC_FEATURES = [
    "horizon_min", "profile", "hour",
    "rain_1h", "rain_3h", "rain_6h", "rain_24h", "rain_48h", "upstream_24h", "rain_fc",
    "stage_now", "stage_frac", "stage_rise_2h", "freeboard_m",
    "flood_150_1h", "flood_150_3h", "flood_400_3h", "flood_maxcls_150_3h", "flood_age_150",
    "clear_150_3h", "haz_100_6h", "firegas_300_6h", "cleared_100_6h",
    "patrol_blocked", "patrol_depth", "patrol_age",
    "closure_active", "sensor_val", "sensor_age", "sensor_rise_1h",
    "traffic_level", "traffic_nodata",
    "status_now",
]
FEATURES = STATIC_FEATURES + DYNAMIC_FEATURES


@dataclass
class Evidence:
    reports: pd.DataFrame
    sensors: pd.DataFrame
    traffic: np.ndarray
    traffic_segs: np.ndarray
    rain: np.ndarray                     # mm/h per step, district (coarse)
    upstream: np.ndarray                 # mm/h per step, Maval
    rain_future: np.ndarray | None = None  # for the forecast proxy (sim only)
    fc_noise: float = 1.0                # multiplicative forecast error for this decision
    start: pd.Timestamp | None = None
    _trafpos: dict = field(default_factory=dict)

    @classmethod
    def from_run(cls, storm_id: str, realization: int, rng: np.random.Generator | None = None) -> "Evidence":
        from ml.district import SIM
        from ml.sim.observe import Observations
        d = SIM / "runs" / storm_id / f"r{realization}"
        ob = Observations.load(d)
        drv = pd.read_parquet(SIM / "drivers" / f"{storm_id}.parquet")
        rep = ob.reports[OBS_COLUMNS].copy()          # truth columns dropped here
        sen = ob.sensors[["source", "site", "seg", "t_min", "value_m"]].copy()
        rain = drv["rain_mmph"].to_numpy(np.float32)
        return cls(rep, sen, ob.traffic, ob.traffic_segs, rain, drv["upstream_mmph"].to_numpy(np.float32),
                   rain_future=rain, start=pd.Timestamp(drv["time"].iloc[0]))


def _forecast(true_future: np.ndarray, fc_noise: float) -> np.ndarray:
    """The rain forecast a decision would have had.

    Live (fc_noise == 1.0) the future is a real forecast already and passes
    through. In training it is the simulated storm's true future, degraded with
    the error a real forecast has been measured to have (ml.forecast_error,
    Open-Meteo historical forecasts against ERA5). The drawn noise is turned
    into a quantile of that measured error, so the random stream is unchanged
    and the old invented multiplier remains the fallback when the measurement
    has not been fitted.
    """
    if fc_noise == 1.0:
        return true_future
    from ml.forecast_error import model

    m = model()
    if m is None:
        return true_future * fc_noise
    from scipy.special import ndtr

    u = float(ndtr(np.log(fc_noise) / 0.4))
    return m.perturb(true_future, u)


def _window_sum(series: np.ndarray, t_step: int, hours: float) -> float:
    k = int(hours * 60 / STEP_MIN)
    lo = max(0, t_step + 1 - k)
    return float(series[lo:t_step + 1].sum() * STEP_MIN / 60)


def _pairs(tree: cKDTree, xy: np.ndarray, r: float) -> tuple[np.ndarray, np.ndarray]:
    """(evidence row, candidate) index pairs within r metres."""
    if len(xy) == 0:
        return np.array([], int), np.array([], int)
    lists = tree.query_ball_point(xy, r)
    lens = np.fromiter((len(v) for v in lists), int, len(lists))
    if lens.sum() == 0:
        return np.array([], int), np.array([], int)
    return np.repeat(np.arange(len(lists)), lens), np.concatenate([np.asarray(v, int) for v in lists if len(v)])


def build(ev: Evidence, t0_min: int, cand: np.ndarray, horizon_min: np.ndarray, profile: np.ndarray,
          limit_m: np.ndarray) -> pd.DataFrame:
    """Features for candidate segments `cand` at decision time t0 (minutes from the
    evidence start). horizon_min, profile and limit_m are per-candidate arrays."""
    st = load_static()
    n = len(cand)
    t_step = t0_min // STEP_MIN
    F = pd.DataFrame({c: st[c].to_numpy()[cand] for c in STATIC_FEATURES if c != "cls_code"})
    F["cls_code"] = st["cls"].map(CLS_CODE).to_numpy()[cand]
    for b in ("underpass", "bridge", "river_bridge", "low_bridge", "industrial"):
        F[b] = F[b].astype(np.int8)
    F["horizon_min"] = horizon_min
    F["profile"] = profile
    hour = ((ev.start + pd.Timedelta(minutes=int(t0_min))).hour if ev.start is not None else 12)
    F["hour"] = hour

    # weather, observed up to t0
    for h in (1, 3, 6, 24, 48):
        F[f"rain_{h}h"] = _window_sum(ev.rain, t_step, h)
    F["upstream_24h"] = _window_sum(ev.upstream, t_step, 24)
    if ev.rain_future is not None:
        fut = np.array([ev.rain_future[t_step + 1: t_step + 1 + int(h // STEP_MIN)].sum() * STEP_MIN / 60
                        for h in horizon_min])
        F["rain_fc"] = _forecast(fut, ev.fc_noise)
    else:
        F["rain_fc"] = np.nan

    # river gauges (per cell) up to t0
    g = ev.sensors[(ev.sensors["source"] == "gauge") & (ev.sensors["t_min"] <= t0_min)]
    cell = st["cell"].to_numpy()[cand]
    now, rise = np.full(len(CELLS), np.nan), np.full(len(CELLS), np.nan)
    for k, c in enumerate(CELLS):
        gc = g[g["site"] == c]
        if len(gc):
            now[k] = gc["value_m"].iloc[-1]
            old = gc[gc["t_min"] <= t0_min - 120]
            rise[k] = (now[k] - old["value_m"].iloc[-1]) / 2 if len(old) else 0.0
    F["stage_now"] = now[cell]
    F["stage_frac"] = F["stage_now"] / F["bank_m"]
    F["stage_rise_2h"] = rise[cell]
    F["freeboard_m"] = F["hand_m"] - F["stage_now"]

    # reports, observed up to t0
    xy = np.c_[st["x"].to_numpy()[cand], st["y"].to_numpy()[cand]]
    tree = cKDTree(xy)
    R = ev.reports[(ev.reports["t_obs_min"] <= t0_min) & (ev.reports["t_obs_min"] > t0_min - 6 * 60)]
    age = (t0_min - R["t_obs_min"]).to_numpy()

    def agg(mask: np.ndarray, r: float, max_age: float, weight: np.ndarray | None = None) -> np.ndarray:
        sel = mask & (age <= max_age)
        rows, cands = _pairs(tree, R.loc[sel, ["x", "y"]].to_numpy(), r)
        w = np.ones(len(rows)) if weight is None else weight[sel][rows]
        return np.bincount(cands, weights=w, minlength=n)

    cit = (R["source"] == "citizen").to_numpy()
    flood = cit & (R["kind"] == "flood").to_numpy()
    rel = R["reliability"].fillna(0.5).to_numpy()
    F["flood_150_1h"] = agg(flood, 150, 60, rel)
    F["flood_150_3h"] = agg(flood, 150, 180, rel)
    F["flood_400_3h"] = agg(flood, 400, 180, rel)
    sel = flood & (age <= 180)
    rows, cands = _pairs(tree, R.loc[sel, ["x", "y"]].to_numpy(), 150)
    mx = np.zeros(n)
    agemin = np.full(n, 999.0)
    if len(rows):
        np.maximum.at(mx, cands, R["depth_class"].to_numpy()[sel][rows].astype(float))
        np.minimum.at(agemin, cands, age[sel][rows].astype(float))
    F["flood_maxcls_150_3h"] = mx
    F["flood_age_150"] = agemin
    F["clear_150_3h"] = agg(cit & (R["kind"] == "clear").to_numpy(), 150, 180)
    F["haz_100_6h"] = agg(cit & R["kind"].isin(HAZARD_KINDS).to_numpy(), 100, 360)
    F["firegas_300_6h"] = agg(cit & R["kind"].isin(["fire", "gas"]).to_numpy(), 300, 360)
    off = (R["source"] == "official").to_numpy()
    F["cleared_100_6h"] = agg(off & (R["kind"] == "cleared").to_numpy(), 100, 360)

    # patrol: latest status within 60 m, last 3 h
    pat = (R["source"] == "patrol").to_numpy() & (age <= 180)
    rows, cands = _pairs(tree, R.loc[pat, ["x", "y"]].to_numpy(), 60)
    pb, pdp, pa = np.full(n, np.nan), np.full(n, np.nan), np.full(n, 999.0)
    if len(rows):
        a = age[pat][rows]
        order = np.argsort(-a)                         # oldest first, newest overwrite
        c, rr = cands[order], rows[order]
        pb[c] = R["blocked_est"].to_numpy()[pat][rr]
        pdp[c] = R["depth_est_m"].to_numpy()[pat][rr]
        pa[c] = a[order]
    F["patrol_blocked"], F["patrol_depth"], F["patrol_age"] = pb, pdp, pa

    # official closures on the segment itself (binding until a reopen notice)
    allR = ev.reports[ev.reports["t_obs_min"] <= t0_min]
    ofc = allR[(allR["source"] == "official") & allR["kind"].isin(["closure", "reopen"])]
    active = set()
    for s_, k_ in zip(ofc["seg_rep"], ofc["kind"]):
        (active.add if k_ == "closure" else active.discard)(int(s_))
    F["closure_active"] = np.isin(cand, list(active)).astype(np.int8)

    # water-level sensors within 300 m
    S = ev.sensors[(ev.sensors["source"] == "sensor") & (ev.sensors["t_min"] <= t0_min)]
    sv, sa, sr = np.full(n, np.nan), np.full(n, 999.0), np.full(n, np.nan)
    if len(S):
        last = S.groupby("seg").tail(1)
        prev = S[S["t_min"] <= t0_min - 60].groupby("seg").tail(1).set_index("seg")["value_m"]
        sxy = np.c_[st["x"].to_numpy()[last["seg"]], st["y"].to_numpy()[last["seg"]]]
        rows, cands = _pairs(tree, sxy, 300)
        for r_, c_ in zip(rows, cands):
            s_ = int(last["seg"].iloc[r_])
            sv[c_] = last["value_m"].iloc[r_]
            sa[c_] = t0_min - last["t_min"].iloc[r_]
            sr[c_] = sv[c_] - prev.get(s_, sv[c_])
    F["sensor_val"], F["sensor_age"], F["sensor_rise_1h"] = sv, sa, sr

    # traffic feed at t0
    if not ev._trafpos:
        ev._trafpos.update({int(s): k for k, s in enumerate(ev.traffic_segs)})
    pos = np.array([ev._trafpos.get(int(c), -1) for c in cand])
    lvl = np.full(n, np.nan)
    has = pos >= 0
    if has.any():
        raw = ev.traffic[min(t_step, ev.traffic.shape[0] - 1), pos[has]].astype(float)
        lvl[has] = np.where(raw == 255, np.nan, raw)
        F["traffic_nodata"] = 0
        F.loc[np.flatnonzero(has)[raw == 255], "traffic_nodata"] = 1
    else:
        F["traffic_nodata"] = 0
    F["traffic_level"] = lvl

    F["status_now"] = current_status(F, limit_m).astype(np.int8)
    return F


def current_status(F: pd.DataFrame, limit_m: np.ndarray) -> np.ndarray:
    """The current-status-only baseline: blocked iff the evidence *now* says so.

    Closed if: an official closure is active; a patrol within 60 m in the last
    hour said blocked; a water sensor within 300 m in the last 30 min reads at
    or above the unit's depth limit; a hazard (tree, wire, collapse, landslide)
    was reported within 100 m in the last 6 h and no "cleared" notice followed;
    or at least two reliability-weighted flood reports within 150 m in the last
    hour with the deepest at least knee-deep (ankle-deep for people on foot).
    It never predicts: nothing is closed until someone has seen it closed.
    """
    knee = np.where(limit_m <= 0.2, 1, 2)
    return ((F["closure_active"] > 0)
            | ((F["patrol_blocked"] == 1) & (F["patrol_age"] <= 60))
            | ((F["sensor_val"] >= limit_m) & (F["sensor_age"] <= 30))
            | ((F["haz_100_6h"] > 0) & (F["cleared_100_6h"] == 0))
            | ((F["flood_150_1h"] >= 2) & (F["flood_maxcls_150_3h"] >= knee))).to_numpy()
