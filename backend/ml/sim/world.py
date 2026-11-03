"""The simulated world for one real storm: flood depth and live hazards on every
road segment, every 30 minutes, for 72 hours.

    from ml.sim.world import simulate
    truth = simulate(storm_id, realization=0)

This is a physically-motivated proxy, not a hydraulic model. It is built to be
honest about what drives road closures in this district and to vary the way
real storms vary, so the models trained on it have something hard to learn.
Every coefficient below is an assumption, named, and in `PARAMS`.

What it simulates per segment and step
---------------------------------------
* River (fluvial) water. GloFAS discharge Q at the segment's river cell gives a
  stage above the dry-season water surface: below bankfull (the median annual
  peak, Q_bf) the river rises inside its banks as bank*sqrt(Q/Q_bf); above it,
  the overbank depth grows to `h_top` at the record peak. Depth on a road is
  stage minus its height above the river (HAND), faded out between 150 m and
  900 m from the river (water has to get there). Bridges carry no water; low
  bridges (causeways) go under at 60% of bank height.
* Rain (pluvial) ponding. Each segment is a bucket: rain on it and its
  catchment flows in, drains take out a fixed capacity, the rest stands. Low
  spots (`pond_m`) collect more and stand deeper; underpasses collect most and
  rely on pumps. ERA5 at 25 km smooths downpours, so hourly rain is split
  unevenly inside the hour and intense hours are boosted; the district gets a
  smooth random rain field so one ward can get far more than the next.
* Live hazards, as events with a start, an end and a place: fallen trees
  (gusts and rain), live wires (a share of tree falls, plus feeders),
  wall/building collapses (rain over 48 h, older dense areas near nallahs),
  landslides (steep segments, rain over 72 h), fires (live wires, waterlogged
  industrial units), gas leaks (flooded industrial land), and bridge closures
  (authorities close bridges at the river's danger level).
* Everyday disruptions, rain or not (`PARAMS["everyday"]`): old trees falling,
  water-main bursts that flood a road and the roads joined to it, blocked
  drains overflowing at low spots, old walls giving way, and road crashes and
  breakdowns (busier hours have more; a quarter block the road, the rest slow
  it). These run in every window, storm or not, so the models also learn the
  roads that close on a dry day.
* Traffic and crowds: time-of-day congestion by road class, slower in rain,
  and spill-over onto the roads next to every blocked one; crowds at hospitals,
  at shelters while nearby areas flood, and onlookers on bridges in high flow.
* Air for drones (kept for when the swarm is switched on): wind, gusts and
  rain per step, no-fly zones over military land, smoke and gas plumes.

Randomness is seeded by (storm, realization), so every run reproduces.
"""
from __future__ import annotations

import json
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

from app.nav.hazards import FIRE_RADIUS_M, GAS_RADIUS_M, AirState, SegmentState
from ml.district import SIM
from ml.sim.static import CELLS, load_adj, load_static
from ml.sim.storms import STEP_MIN

DT_H = STEP_MIN / 60.0

#: Every assumption in one place (recorded in each run's meta.json).
PARAMS = {
    # river
    "h_top_m": 3.0,               # overbank depth at the record discharge
    "overbank_exp": 0.6,
    "reach_full_m": 150.0, "reach_zero_m": 900.0,
    "causeway_frac": 0.6,         # low bridges (causeways) submerge at 60% of bank height
    "causeway_close_frac": 0.5,   # ...and are closed a little before that
    "bridge_close_over": 0.8,     # main river bridges close at 80% of the record overbank
    # rain
    "field_sd": 0.30,             # spatial rain variability (lognormal sd)
    "burst_lo": 1.0, "burst_hi": 1.6,   # boost for hours >= 4 mm/h (ERA5 smooths peaks)
    # Effective drain capacity in mm/h of rain on the segment's own area. Calibrated
    # (ml/sim/README.md) so a 25-50 mm day ponds almost nowhere and the 25 Jul
    # 2024 storm floods a few hundred segments; not measured drain sizes.
    "drain_mmph": {"main": 45.0, "minor": 35.0, "local": 30.0, "path": 25.0},
    "drain_underpass_mmph": 40.0, "pump_fail_p": 0.2, "pump_fail_mmph": 6.0,
    "blocked_drain_p": (0.10, 0.25), "blocked_drain_p_june": 0.10, "blocked_drain_factor": 0.4,
    "catch_per_pond": 1.0, "catch_underpass": 3.0,
    "conc_per_pond": 0.6, "conc_underpass": 3.0,
    "cap_base_m": 0.10, "cap_per_pond": 0.25, "cap_underpass_m": 1.4,
    # live hazards (rates per hour)
    "tree_per_km_h": 3e-4, "gust_floor_kmh": 20.0, "gust_scale_kmh": 40.0,
    "wire_with_tree_p": 0.25,
    "collapse_per_seg_h": 1e-6,
    "landslide_per_seg_h": 1e-5, "landslide_slope_pct": 15.0,
    "fire_per_wire_h": 0.03, "fire_industrial_h": 0.01,
    "gas_per_flooded_ind_h": 2e-4,
    "clear_tree_h": (3.0, 0.6), "clear_wire_h": (4.0, 0.5), "fire_h": (2.0, 0.5), "gas_h": (3.0, 0.5),
    # traffic and crowds
    # everyday disruptions, on any day, rain or not (totals for the district;
    # assumptions sized to PCMC news: a couple of tree falls a day, a water-main
    # burst every few days, ~10 road crashes a day of which a quarter block a road)
    "everyday": {"tree_h": 0.08, "water_main_h": 0.0125, "drain_overflow_h": 0.01, "collapse_h": 0.002,
                 "crash_h": 0.42, "crash_block_p": 0.25, "main_depth_m": (0.15, 0.5), "main_h": (4.0, 0.5),
                 "drain_depth_m": (0.1, 0.3), "drain_h": (3.0, 0.5), "crash_h_dur": (0.75, 0.5),
                 "breakdown_jam": 0.5},
    "jam_rain": 0.25, "jam_spill_1": 0.30, "jam_spill_2": 0.15,
    "crowd_hospital": 0.15, "crowd_shelter": 0.4, "crowd_onlookers": 0.6,
}

F_FLOWING, F_DEBRIS, F_WIRE, F_COLLAPSE, F_LANDSLIDE, F_BRIDGE = 1, 2, 4, 8, 16, 32
EVENT_KINDS = ("tree", "wire", "collapse", "landslide", "fire", "gas", "bridge_closure",
               "water_main", "drain_overflow", "crash", "breakdown")
CLASS_JAM = {"main": 1.0, "minor": 0.8, "local": 0.4, "path": 0.0}


def seed_of(storm_id: str, realization: int) -> int:
    return (zlib.crc32(storm_id.encode()) ^ (realization * 7919)) & 0x7FFFFFFF


@dataclass
class Truth:
    """What actually happened. Arrays are [step, segment]."""
    storm_id: str
    realization: int
    start: pd.Timestamp
    depth: np.ndarray          # float16 metres
    fluvial: np.ndarray        # float16 metres (part of depth from the river)
    flags: np.ndarray          # uint8 bitmask (F_*)
    jam: np.ndarray            # uint8, /250 -> 0..1
    crowd: np.ndarray          # uint8, /250
    rain: np.ndarray           # float32 [step] district rain after downscaling, mm/h
    field: np.ndarray          # float32 [segment] rain multiplier
    stage: np.ndarray          # float32 [step, cell] metres above dry-season level
    events: pd.DataFrame
    air: pd.DataFrame
    meta: dict = field(default_factory=dict)

    @property
    def steps(self) -> int:
        return self.depth.shape[0]

    def minute(self, t: int) -> int:
        return t * STEP_MIN

    def active(self, t: int, kinds: tuple[str, ...] | None = None) -> pd.DataFrame:
        e = self.events
        m = (e["t0"] <= t) & (e["t1"] > t)
        if kinds:
            m &= e["kind"].isin(kinds)
        return e[m]

    def point_dist(self, t: int, kind: str, radius: float) -> np.ndarray:
        """Distance from every segment to the nearest active `kind` event (inf if none within 2x radius)."""
        st = load_static()
        a = self.active(t, (kind,))
        out = np.full(len(st), np.inf, dtype=np.float32)
        if a.empty:
            return out
        kd = cKDTree(st[["x", "y"]].to_numpy())
        for x, y in a[["x", "y"]].to_numpy():
            idx = kd.query_ball_point([x, y], r=2 * radius)
            if idx:
                d = np.hypot(st["x"].to_numpy()[idx] - x, st["y"].to_numpy()[idx] - y)
                out[idx] = np.minimum(out[idx], d)
        return out

    def state(self, t: int, i: int, fire_d: np.ndarray | None = None,
              gas_d: np.ndarray | None = None) -> SegmentState:
        f = int(self.flags[t, i])
        fd = (fire_d if fire_d is not None else self.point_dist(t, "fire", FIRE_RADIUS_M))[i]
        gd = (gas_d if gas_d is not None else self.point_dist(t, "gas", GAS_RADIUS_M))[i]
        return SegmentState(
            depth_m=float(self.depth[t, i]), flowing=bool(f & F_FLOWING), debris=bool(f & F_DEBRIS),
            wire=bool(f & F_WIRE), collapse=bool(f & F_COLLAPSE), landslide=bool(f & F_LANDSLIDE),
            fire_dist_m=None if not np.isfinite(fd) else float(fd),
            gas_dist_m=None if not np.isfinite(gd) else float(gd),
            crowd=float(self.crowd[t, i]) / 250, jam=float(self.jam[t, i]) / 250,
            rain_mmph=float(self.rain[t] * self.field[i]), bridge_closed=bool(f & F_BRIDGE))

    def air_state(self, t: int) -> AirState:
        a = self.air.iloc[t]
        return AirState(wind_ms=float(a.wind_ms), gust_ms=float(a.gust_ms), rain_mmph=float(a.rain_max_mmph))

    # ---- storage -------------------------------------------------------
    def save(self, root: Path | None = None) -> Path:
        d = (root or SIM / "runs") / self.storm_id / f"r{self.realization}"
        d.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(d / "truth.npz", depth=self.depth, fluvial=self.fluvial, flags=self.flags,
                            jam=self.jam, crowd=self.crowd, rain=self.rain, field=self.field, stage=self.stage)
        self.events.to_parquet(d / "events.parquet", index=False)
        self.air.to_parquet(d / "air.parquet", index=False)
        (d / "meta.json").write_text(json.dumps(self.meta, indent=1, default=str))
        return d

    @classmethod
    def load(cls, storm_id: str, realization: int, root: Path | None = None) -> "Truth":
        d = (root or SIM / "runs") / storm_id / f"r{realization}"
        z = np.load(d / "truth.npz")
        meta = json.loads((d / "meta.json").read_text())
        return cls(storm_id, realization, pd.Timestamp(meta["start"]), z["depth"], z["fluvial"], z["flags"],
                   z["jam"], z["crowd"], z["rain"], z["field"], z["stage"],
                   pd.read_parquet(d / "events.parquet"), pd.read_parquet(d / "air.parquet"), meta)


def _rain_field(rng: np.random.Generator, x: np.ndarray, y: np.ndarray, sd: float) -> np.ndarray:
    """Smooth lognormal multiplier, mean 1: a few Gaussian blobs 2-6 km wide."""
    g = np.zeros_like(x, dtype=np.float64)
    for _ in range(8):
        cx, cy = rng.uniform(x.min(), x.max()), rng.uniform(y.min(), y.max())
        w = rng.uniform(2000, 6000)
        g += rng.normal() * np.exp(-((x - cx) ** 2 + (y - cy) ** 2) / (2 * w * w))
    g = (g - g.mean()) / (g.std() + 1e-9) * sd
    f = np.exp(g - sd * sd / 2)
    return (f / f.mean()).astype(np.float32)


def _downscale(rng: np.random.Generator, hourly_held: np.ndarray, p: dict) -> np.ndarray:
    """Split each hour unevenly across its two half-hours and boost intense hours."""
    r = hourly_held.astype(np.float64).copy()
    n = len(r) // 2 * 2
    a = rng.uniform(0.3, 1.7, size=n // 2)
    r[0:n:2] *= a
    r[1:n:2] *= 2 - a
    boost = rng.uniform(p["burst_lo"], p["burst_hi"])
    r = np.where(hourly_held >= 4, r * boost, r)
    return r.astype(np.float32)


def _lognormal_steps(rng, median_h: float, sigma: float) -> int:
    return max(1, int(round(rng.lognormal(np.log(median_h), sigma) / DT_H)))


def simulate(storm_id: str, realization: int = 0, params: dict | None = None) -> Truth:
    p = {**PARAMS, **(params or {})}
    rng = np.random.default_rng(seed_of(storm_id, realization))
    cat = pd.read_parquet(SIM / "storms.parquet").set_index("storm_id")
    storm = cat.loc[storm_id]
    drv = pd.read_parquet(SIM / "drivers" / f"{storm_id}.parquet")
    stats = json.loads((SIM / "river_stats.json").read_text())
    st = load_static()
    adj = load_adj()
    n, T = len(st), len(drv)

    x, y = st["x"].to_numpy(), st["y"].to_numpy()
    cls = st["cls"].to_numpy()
    drivable = st["drivable"].to_numpy()
    under = st["underpass"].to_numpy()
    pond = st["pond_m"].to_numpy()
    hand = st["hand_m"].to_numpy()
    distr = st["dist_river_m"].to_numpy()
    bank = st["bank_m"].to_numpy()
    cell = st["cell"].to_numpy()
    rbridge = st["river_bridge"].to_numpy()
    lbridge = st["low_bridge"].to_numpy()
    length_km = st["length_m"].to_numpy() / 1000

    # ---- hydraulic jitter per realization --------------------------------
    h_top = p["h_top_m"] * rng.uniform(0.8, 1.2)
    hand_err = rng.normal(0, 0.5, size=n)          # DEM error, fixed for the run

    # ---- river stage per cell, per step ------------------------------------
    Q = drv[[f"q_{c}" for c in CELLS]].to_numpy(dtype=np.float64)
    qbf = np.array([stats[c]["q_bf"] for c in CELLS])
    qtop = np.array([stats[c]["q_top"] for c in CELLS])
    # bank height for each cell = the bank of the river most of its segments sit on
    cell_bank = np.array([float(np.median(bank[cell == k])) if (cell == k).any() else 9.0 for k in range(len(CELLS))])
    ratio = Q / qbf
    over = h_top * np.clip((Q - qbf) / np.maximum(qtop - qbf, 1), 0, None) ** p["overbank_exp"]
    stage_frac = np.where(ratio <= 1, np.sqrt(np.clip(ratio, 0, 1)), 1 + over / cell_bank)   # x bank height
    stage = (stage_frac * cell_bank).astype(np.float32)                                         # [T, cells]

    reach = np.clip((p["reach_zero_m"] - distr) / (p["reach_zero_m"] - p["reach_full_m"]), 0, 1)
    seg_stage_frac = stage_frac[:, cell]                                                        # [T, n]
    fluv = np.clip(seg_stage_frac * bank - (hand + hand_err), 0, None) * reach
    fluv[:, rbridge] = 0.0
    caus = np.clip((seg_stage_frac[:, lbridge] - p["causeway_frac"]) * bank[lbridge], 0, None)
    fluv[:, lbridge] = caus
    # authorities' rule: causeways early, main bridges only near a record flood
    main_close = 1 + p["bridge_close_over"] * h_top / bank
    bridge_closed = (seg_stage_frac >= main_close[None, :]) & rbridge & ~lbridge
    bridge_closed |= (seg_stage_frac >= p["causeway_close_frac"]) & lbridge

    # ---- rain -------------------------------------------------------------
    field_ = _rain_field(rng, x, y, p["field_sd"])
    rain = _downscale(rng, drv["rain_mmph"].to_numpy(), p)                        # [T]
    month = int(pd.Timestamp(storm["start"]).month)
    pblock = rng.uniform(*p["blocked_drain_p"]) + (p["blocked_drain_p_june"] if month == 6 else 0)
    drain = np.array([p["drain_mmph"][c] for c in cls], dtype=np.float64)
    drain = np.where(rng.random(n) < pblock, drain * p["blocked_drain_factor"], drain)
    pump = np.where(rng.random(n) < p["pump_fail_p"], p["pump_fail_mmph"], p["drain_underpass_mmph"])
    drain = np.where(under, pump, drain)
    catch = 1 + p["catch_per_pond"] * pond + p["catch_underpass"] * under
    conc = 1 + p["conc_per_pond"] * pond + p["conc_underpass"] * under
    cap = p["cap_base_m"] + p["cap_per_pond"] * pond + p["cap_underpass_m"] * under

    S = np.zeros(n)
    pluv = np.zeros((T, n), dtype=np.float32)
    rain48 = np.zeros((T, n), dtype=np.float32)
    acc = np.concatenate([[0.0], np.cumsum(rain) * DT_H])     # acc[k] = rain before step k
    win = lambda t, h: (acc[t + 1] - acc[max(0, t + 1 - int(h / DT_H))])  # noqa: E731
    for t in range(T):
        S = np.clip(S + (rain[t] * field_ * catch - drain) * DT_H, 0, None)
        pluv[t] = np.minimum(cap, S / 1000 * conc)
        rain48[t] = win(t, 48) * field_

    depth = np.maximum(fluv, pluv) + 0.25 * np.minimum(fluv, pluv)
    flags = np.zeros((T, n), dtype=np.uint8)
    slope = st["slope_pct"].to_numpy()
    flowing = ((fluv > 0.1) & (distr < 300)[None, :]) | ((pluv > 0.1) & (slope > 5)[None, :])
    flags |= np.where(flowing, F_FLOWING, 0).astype(np.uint8)
    flags |= np.where(bridge_closed, F_BRIDGE, 0).astype(np.uint8)

    # ---- live hazard events ------------------------------------------------
    gust = drv["gust_kmh"].to_numpy()
    events: list[dict] = []
    dens = st["density"].to_numpy()
    resid = (cls == "local") & drivable
    near_nallah = st["dist_water_m"].to_numpy() < 200
    steep = (slope >= p["landslide_slope_pct"]) & (distr > 300)
    ind = st["industrial"].to_numpy()
    lon, lat = st["lon"].to_numpy(), st["lat"].to_numpy()

    def add(kind: str, i: int, t0: int, dur: int, cause: str, radius: float = 0.0, depth_m: float = 0.0) -> None:
        events.append({"kind": kind, "seg": int(i), "x": float(x[i]), "y": float(y[i]),
                       "lon": float(lon[i]), "lat": float(lat[i]), "t0": int(t0), "t1": int(min(T, t0 + dur)),
                       "radius_m": radius, "cause": cause, "depth_m": depth_m})

    def draw(rate: np.ndarray, t: int) -> np.ndarray:
        tot = float(rate.sum()) * DT_H
        k = rng.poisson(tot) if tot > 0 else 0
        if k == 0:
            return np.array([], dtype=int)
        return rng.choice(len(rate), size=k, replace=True, p=rate / rate.sum())

    tree_w = length_km * np.where(cls == "path", 0.2, 1.0) * (1 + 0.5 * resid)
    E = p["everyday"]
    _times = pd.DatetimeIndex(drv["time"])
    _hours = _times.hour.to_numpy() + _times.minute.to_numpy() / 60
    busy = 0.12 + 0.38 * np.exp(-((_hours - 10) / 1.5) ** 2) + 0.45 * np.exp(-((_hours - 19) / 2.0) ** 2)
    busy = np.where((_hours < 6) | (_hours >= 23), 0.04, busy)
    busy = busy / busy.mean()
    road_w = length_km * drivable * np.where(cls == "main", 3.0, np.where(cls == "minor", 1.5, 0.3))
    pipe_w = length_km * drivable * np.where(cls == "local", 0.5, 1.0)
    pond_w = drivable * (pond > 0) * (1 + pond)
    wall_w = resid * (1 + dens)

    def pick(w: np.ndarray) -> int | None:
        tot = float(w.sum())
        return int(rng.choice(len(w), p=w / tot)) if tot > 0 else None

    for t in range(T):
        # ---- everyday disruptions, rain or not ----------------------------
        if rng.random() < E["tree_h"] * DT_H and (i := pick(tree_w * drivable)) is not None:
            add("tree", i, t, _lognormal_steps(rng, *p["clear_tree_h"]), "old tree fell (no storm)")
            if rng.random() < p["wire_with_tree_p"]:
                add("wire", i, t, _lognormal_steps(rng, *p["clear_wire_h"]), "tree on the line")
        if rng.random() < E["water_main_h"] * DT_H and (i := pick(pipe_w)) is not None:
            add("water_main", i, t, _lognormal_steps(rng, *E["main_h"]), "water main burst",
                depth_m=float(rng.uniform(*E["main_depth_m"])))
        if rng.random() < E["drain_overflow_h"] * DT_H and (i := pick(pond_w)) is not None:
            add("drain_overflow", i, t, _lognormal_steps(rng, *E["drain_h"]), "blocked drain overflowing",
                depth_m=float(rng.uniform(*E["drain_depth_m"])))
        if rng.random() < E["collapse_h"] * DT_H and (i := pick(wall_w)) is not None:
            add("collapse", i, t, T, "old wall gave way (no storm)")
        for _ in range(rng.poisson(E["crash_h"] * busy[t] * DT_H)):
            i = pick(road_w)
            if i is None:
                continue
            if rng.random() < E["crash_block_p"]:
                add("crash", i, t, _lognormal_steps(rng, *E["crash_h_dur"]), "crash blocking the road")
            else:
                add("breakdown", i, t, _lognormal_steps(rng, *E["crash_h_dur"]), "crash or breakdown, one lane")

        g = max(0.0, gust[t] - p["gust_floor_kmh"]) / p["gust_scale_kmh"]
        r_t = rain[t] * field_
        if g > 0:
            for i in draw(p["tree_per_km_h"] * g ** 3 * (1 + r_t / 15) * tree_w * drivable, t):
                add("tree", i, t, _lognormal_steps(rng, *p["clear_tree_h"]), f"gust {gust[t]:.0f} km/h")
                if rng.random() < p["wire_with_tree_p"]:
                    add("wire", i, t, _lognormal_steps(rng, *p["clear_wire_h"]), "tree on the line")
        coll = p["collapse_per_seg_h"] * resid * (1 + dens) * (1 + near_nallah) * (rain48[t] / 100) ** 2
        for i in draw(coll, t):
            add("collapse", i, t, T, f"{rain48[t, i]:.0f} mm in 48 h")
        r72 = win(t, 72) * field_
        ls = p["landslide_per_seg_h"] * steep * drivable * (r72 / 150) ** 3
        for i in draw(ls, t):
            add("landslide", i, t, T, f"slope {slope[i]:.0f}%, {r72[i]:.0f} mm")
        wires = [e for e in events if e["kind"] == "wire" and e["t0"] <= t < e["t1"]]
        for e in wires:
            if rng.random() < p["fire_per_wire_h"] * DT_H:
                add("fire", e["seg"], t, _lognormal_steps(rng, *p["fire_h"]), "short circuit from a downed line",
                    FIRE_RADIUS_M)
        if rain[t] > 5 and ind.any() and rng.random() < p["fire_industrial_h"] * DT_H:
            add("fire", int(rng.choice(np.flatnonzero(ind))), t, _lognormal_steps(rng, *p["fire_h"]),
                "electrical fire in a waterlogged unit", FIRE_RADIUS_M)
        flooded_ind = np.flatnonzero(ind & (depth[t] > 0.3))
        if len(flooded_ind) and rng.random() < 1 - np.exp(-p["gas_per_flooded_ind_h"] * len(flooded_ind) * DT_H):
            add("gas", int(rng.choice(flooded_ind)), t, _lognormal_steps(rng, *p["gas_h"]),
                "chemical store flooded", GAS_RADIUS_M)

    for i in np.flatnonzero(rbridge | lbridge):
        on = bridge_closed[:, i]
        if on.any():
            t0 = int(np.argmax(on))
            t1 = int(T - np.argmax(on[::-1]))
            add("bridge_closure", i, t0, t1 - t0, "river at the danger mark")

    ev = pd.DataFrame(events, columns=["kind", "seg", "x", "y", "lon", "lat", "t0", "t1", "radius_m", "cause", "depth_m"])
    for kind, flag in (("tree", F_DEBRIS), ("wire", F_WIRE), ("collapse", F_COLLAPSE), ("landslide", F_LANDSLIDE),
                       ("crash", F_DEBRIS)):
        for r in ev[ev["kind"] == kind].itertuples():
            flags[r.t0:r.t1, r.seg] |= flag
    # water on the road with no rain: a burst main floods its road and spills onto
    # the roads joined to it; an overflowing drain floods its low spot.
    for r in ev[ev["kind"].isin(["water_main", "drain_overflow"])].itertuples():
        depth[r.t0:r.t1, r.seg] = np.maximum(depth[r.t0:r.t1, r.seg], r.depth_m)
        if r.kind == "water_main":
            nb = adj[r.seg].indices
            depth[r.t0:r.t1][:, nb] = np.maximum(depth[r.t0:r.t1][:, nb], 0.5 * r.depth_m)
    breakdown_jam = np.zeros((T, n), dtype=np.float32)
    for r in ev[ev["kind"] == "breakdown"].itertuples():
        breakdown_jam[r.t0:r.t1, r.seg] = E["breakdown_jam"]

    # ---- traffic -----------------------------------------------------------
    times = pd.DatetimeIndex(drv["time"])
    hours = times.hour.to_numpy() + times.minute.to_numpy() / 60
    tod = 0.12 + 0.38 * np.exp(-((hours - 10) / 1.5) ** 2) + 0.45 * np.exp(-((hours - 19) / 2.0) ** 2)
    tod = np.where((hours < 6) | (hours >= 23), 0.04, tod) * np.where(times.dayofweek.to_numpy() == 6, 0.7, 1.0)
    cw = np.array([CLASS_JAM[c] for c in cls])
    jam = np.zeros((T, n), dtype=np.float32)
    blocked_any = (depth >= 0.3) | (flags & (F_DEBRIS | F_WIRE | F_COLLAPSE | F_LANDSLIDE | F_BRIDGE)).astype(bool)
    blocked_any &= drivable[None, :]
    weight_blk = np.where(cls == "main", 1.0, np.where(cls == "minor", 0.6, 0.25))
    for t in range(T):
        j = tod[t] * cw + p["jam_rain"] * np.minimum(1, rain[t] * field_ / 8) * cw
        b = blocked_any[t] * weight_blk
        if b.any():
            s1 = adj @ b
            s2 = adj @ (s1 > 0).astype(np.float32)
            j = j + p["jam_spill_1"] * np.minimum(1, s1) + p["jam_spill_2"] * np.minimum(1, s2) * (s1 == 0)
        j = j + breakdown_jam[t]
        jam[t] = np.clip(j, 0, 1) * drivable
    # stored in steps of 0.02 (5/250): finer than any feed reports, and it compresses
    jam_u8 = (np.round(jam * 50) * 5).astype(np.uint8)

    # ---- crowds ------------------------------------------------------------
    crowd = np.zeros((T, n), dtype=np.float32)
    hosp = st["hospital_m"].to_numpy() < 300
    shel = st["shelter_m"].to_numpy() < 200
    shel_i = np.flatnonzero(shel)
    day = (hours >= 7) & (hours < 20)
    for t in range(T):
        c = p["crowd_hospital"] * hosp * (1.0 if day[t] else 0.3)
        fl = np.flatnonzero(fluv[t] > 0.2)
        if len(fl) and len(shel_i):
            d, _ = cKDTree(np.c_[x[fl], y[fl]]).query(np.c_[x[shel_i], y[shel_i]], distance_upper_bound=1500)
            c[shel_i[np.isfinite(d)]] += p["crowd_shelter"]
        if day[t]:
            look = rbridge & (seg_stage_frac[t] >= 0.7)
            if look.any():
                c = c + p["crowd_onlookers"] * look
                c = c + 0.5 * p["crowd_onlookers"] * (adj @ look.astype(np.float32) > 0)
        crowd[t] = np.clip(c, 0, 1)
    crowd_u8 = (crowd * 250).astype(np.uint8)

    # ---- air (drones) --------------------------------------------------------
    plume = ev[ev["kind"].isin(["fire", "gas"])]
    air = pd.DataFrame({
        "t": np.arange(T), "time": times,
        "wind_ms": (drv["wind_kmh"].to_numpy() / 3.6).round(2),
        "gust_ms": (drv["gust_kmh"].to_numpy() / 3.6).round(2),
        "rain_mean_mmph": rain.round(2), "rain_max_mmph": (rain * float(field_.max())).round(2),
        "cloud_pct": drv["cloud_pct"].to_numpy(),
        "plumes": [int(((plume["t0"] <= t) & (plume["t1"] > t)).sum()) for t in range(T)],
    })

    meta = {
        "storm_id": storm_id, "realization": realization, "seed": seed_of(storm_id, realization),
        "start": str(pd.Timestamp(storm["start"])), "step_min": STEP_MIN, "steps": T,
        "split": storm["split"], "tier": storm["tier"], "glofas_kind": storm["glofas_kind"],
        "params": p, "h_top_m_run": round(h_top, 3), "blocked_drain_p_run": round(pblock, 3),
        "events": ev["kind"].value_counts().to_dict(),
        "peak": {
            "segments_depth_ge_0_15": int((depth.max(0) >= 0.15).sum()),
            "segments_depth_ge_0_3": int((depth.max(0) >= 0.3).sum()),
            "segments_depth_ge_0_6": int((depth.max(0) >= 0.6).sum()),
            "fluvial_ge_0_3": int((fluv.max(0) >= 0.3).sum()),
            "underpasses_ge_0_3": int(((depth.max(0) >= 0.3) & under).sum()),
            "max_stage_frac": float(stage_frac.max()),
        },
        "no_fly": "military land use (OSM)",
    }
    return Truth(storm_id, realization, pd.Timestamp(storm["start"]), depth.astype(np.float16),
                 fluv.astype(np.float16), flags, jam_u8, crowd_u8, rain, field_, stage, ev, air, meta)
