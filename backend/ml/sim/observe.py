"""What the command centre would have *seen* of a simulated storm.

    from ml.sim.observe import observe
    obs = observe(truth)        # truth from ml.sim.world.simulate / Truth.load

The models never see the truth. They see this: late, noisy, partial and
sometimes wrong. Sources, and how each one lies:

citizen    Reports from residents (app, WhatsApp, helpline). More where more
           people live and in daytime, mostly early in a flood, then fading.
           Late (median 12 min, long tail), placed ~25 m off by GPS or 150 m off
           by a landmark (then snapped to whatever road is nearest, sometimes
           the wrong one), depth said as none/ankle/knee/waist and often a
           class off. Also: rumours (a "flooded" report on a dry road near real
           flooding), hoaxes, stale forwards (an old report re-shared hours
           later as if new) and "it's clear now" reports once water drops.
patrol     Traffic police and crews checking roads, steered towards roads
           with recent reports. Few, quick (about 5 minutes) and nearly right.
official   Bridge closure and reopening notices from the authority, and
           tree-cell "cleared" notices. Right, but 15-60 minutes behind.
sensor     Water-level sensors at the worst underpasses and the lowest
           riverside roads, every step, 2 cm noise, some drop-outs, and in
           half of the runs one sensor stuck on an old value.
gauge      River stage per GloFAS cell, every step, 8 cm noise.
traffic    Congestion level (0 low .. 3 severe, 255 no data) on main and minor
           roads, from probe vehicles: a closed road has no probes, so it often
           shows *no data*, not red.

Drones are not a source while the swarm is paused.

Writes data/sim/runs/<storm>/r<k>/obs/{reports.parquet, sensors.parquet, traffic.npz}.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

from ml.sim.static import CELLS, load_static
from ml.sim.storms import STEP_MIN
from ml.sim.world import F_BRIDGE, F_COLLAPSE, F_DEBRIS, F_LANDSLIDE, F_WIRE, Truth, seed_of

DT_H = STEP_MIN / 60.0
DEPTH_CLASSES = (0.05, 0.15, 0.45)   # none < 5 cm <= ankle < 15 cm <= knee < 45 cm <= waist

OBS_PARAMS = {
    "flood_report_h": 0.06,         # reports/hour from one flooded segment in the densest area, early on
    "event_reports": {"tree": 1.5, "wire": 2.0, "collapse": 3.0, "landslide": 2.0, "fire": 4.0, "gas": 3.0,
                      "crash": 2.5, "breakdown": 0.6, "water_main": 2.0, "drain_overflow": 1.0},
    "delay_median_min": 12.0, "delay_sigma": 0.9, "delay_cap_min": 360,
    "gps_sd_m": 25.0, "landmark_p": 0.15, "landmark_sd_m": 150.0,
    "class_err_p": 0.3, "kind_err_p": 0.08,
    "rumour_h": 0.08, "hoax_h": 0.02, "stale_p": 0.12, "clear_p": 0.3,
    "patrol_per_step": 30, "patrol_err_p": 0.03,
    "sensor_underpasses": 12, "sensor_riverside": 4, "sensor_sd_m": 0.02, "sensor_drop_p": 0.03,
    "gauge_sd_m": 0.08, "traffic_flip_p": 0.15,
}

KIND_FLAG = {"tree": F_DEBRIS, "wire": F_WIRE, "collapse": F_COLLAPSE, "landslide": F_LANDSLIDE}


def depth_class(d: np.ndarray) -> np.ndarray:
    return np.searchsorted(np.array(DEPTH_CLASSES), d, side="right").astype(np.int8)


@dataclass
class Observations:
    reports: pd.DataFrame
    sensors: pd.DataFrame
    traffic: np.ndarray          # uint8 [step, len(traffic_segs)]
    traffic_segs: np.ndarray     # segment indices
    meta: dict

    def save(self, run_dir) -> None:
        d = run_dir / "obs"
        d.mkdir(parents=True, exist_ok=True)
        self.reports.to_parquet(d / "reports.parquet", index=False)
        self.sensors.to_parquet(d / "sensors.parquet", index=False)
        np.savez_compressed(d / "traffic.npz", level=self.traffic, segs=self.traffic_segs)
        (d / "meta.json").write_text(json.dumps(self.meta, indent=1))

    @classmethod
    def load(cls, run_dir) -> "Observations":
        d = run_dir / "obs"
        z = np.load(d / "traffic.npz")
        return cls(pd.read_parquet(d / "reports.parquet"), pd.read_parquet(d / "sensors.parquet"),
                   z["level"], z["segs"], json.loads((d / "meta.json").read_text()))


def observe(tr: Truth, params: dict | None = None) -> Observations:
    p = {**OBS_PARAMS, **(params or {})}
    rng = np.random.default_rng(seed_of(tr.storm_id, tr.realization) + 1)
    st = load_static()
    n, T = len(st), tr.steps
    x, y = st["x"].to_numpy(), st["y"].to_numpy()
    dens = st["density"].to_numpy()
    usable = (st["drivable"] | st["walkable"]).to_numpy()
    kd = cKDTree(np.c_[x, y])
    times = pd.date_range(tr.start, periods=T, freq=f"{STEP_MIN}min")
    hours = times.hour.to_numpy()
    awake = np.where((hours >= 7) & (hours < 22), 1.0, np.where((hours >= 5) & (hours < 7), 0.5, 0.2))
    depth = tr.depth.astype(np.float32)

    rows: list[dict] = []

    def place(i: int) -> tuple[float, float, int]:
        sd = p["landmark_sd_m"] if rng.random() < p["landmark_p"] else p["gps_sd_m"]
        px, py = x[i] + rng.normal(0, sd), y[i] + rng.normal(0, sd)
        return px, py, int(kd.query([px, py])[1])

    def delay() -> int:
        return int(min(p["delay_cap_min"], rng.lognormal(np.log(p["delay_median_min"]), p["delay_sigma"])))

    def report(src: str, kind: str, t_true: int, seg: int | None, *, cls_: int = -1, rel: float | None = None,
               false: bool = False, stale_of: int = -1, depth_est: float = np.nan, blocked: float = np.nan,
               lag: int | None = None, exact: bool = False) -> None:
        if seg is None:
            return
        if exact:
            px, py, rep = x[seg], y[seg], seg
        else:
            px, py, rep = place(seg)
        tt = t_true * STEP_MIN + int(rng.integers(0, STEP_MIN))
        rows.append({"source": src, "kind": kind, "t_true_min": tt, "t_obs_min": tt + (delay() if lag is None else lag),
                     "seg_rep": rep, "x": px, "y": py, "depth_class": cls_,
                     "depth_est_m": depth_est, "blocked_est": blocked,
                     "reliability": rel if rel is not None else float(rng.beta(6, 2)),
                     "truth_seg": -1 if false else seg, "is_false": false, "is_stale": stale_of >= 0,
                     "stale_of": stale_of})

    # ---- citizen: flooding ------------------------------------------------
    wet = (depth >= 0.1) & usable[None, :]
    run_start = np.full(n, -1)
    for t in range(T):
        w = wet[t]
        run_start = np.where(w, np.where(run_start < 0, t, run_start), -1)
        idx = np.flatnonzero(w)
        if not len(idx):
            continue
        age_h = (t - run_start[idx]) * DT_H
        sev = np.clip(depth[t, idx] / 0.3, 0.3, 2.0)
        lam = p["flood_report_h"] * (0.2 + dens[idx]) * sev * awake[t] * (np.exp(-age_h / 3) + 0.2)
        k = rng.poisson(lam * DT_H)
        for i, kk in zip(idx[k > 0], k[k > 0]):
            for _ in range(int(kk)):
                c = int(depth_class(np.array([depth[t, i]]))[0])
                if rng.random() < p["class_err_p"]:
                    c = int(np.clip(c + rng.choice([-1, 1]), 1, 3))
                kind = "flood" if rng.random() > p["kind_err_p"] else "tree"
                report("citizen", kind, t, int(i), cls_=c)
        # "clear now": a run that just ended after real water
        if t > 0:
            ended = np.flatnonzero(wet[t - 1] & ~wet[t] & (depth[max(0, t - 4):t].max(0) >= 0.15))
            for i in ended:
                if rng.random() < p["clear_p"] * (0.3 + dens[i]) * awake[t]:
                    report("citizen", "clear", t, int(i), cls_=0)

    # ---- citizen: live-hazard events ---------------------------------------
    for e in tr.events.itertuples():
        if e.kind == "bridge_closure":
            continue
        lam = p["event_reports"].get(e.kind, 1.0) * (0.3 + dens[e.seg]) * awake[e.t0]
        for _ in range(1 + rng.poisson(lam)):
            t = int(min(e.t1 - 1, e.t0 + rng.exponential(1.0) / DT_H))
            if e.kind in ("water_main", "drain_overflow"):     # people say "the road is flooded"
                tt = max(e.t0, t)
                report("citizen", "flood", tt, int(e.seg), cls_=int(depth_class(np.array([depth[tt, e.seg]]))[0]))
            else:
                report("citizen", e.kind, max(e.t0, t), int(e.seg))

    # ---- rumours and hoaxes --------------------------------------------------
    for t in range(T):
        fl = np.flatnonzero(depth[t] >= 0.3)
        lam = p["rumour_h"] * np.sqrt(len(fl)) * awake[t] + p["hoax_h"]
        for _ in range(rng.poisson(lam * DT_H)):
            if len(fl) and rng.random() > p["hoax_h"] / max(lam, 1e-9):
                a = int(rng.choice(fl))
                ang, r = rng.uniform(0, 2 * np.pi), rng.uniform(500, 2500)
                cand = kd.query_ball_point([x[a] + r * np.cos(ang), y[a] + r * np.sin(ang)], r=300)
            else:
                cand = [int(rng.integers(n))]
            cand = [c for c in cand if depth[t, c] < 0.05 and usable[c]]
            if cand:
                report("citizen", "flood", t, int(rng.choice(cand)), cls_=int(rng.integers(1, 4)),
                       rel=float(rng.beta(3, 3)), false=True)

    # ---- stale forwards --------------------------------------------------------
    base = len(rows)
    for j in range(base):
        r = rows[j]
        if r["source"] == "citizen" and r["kind"] != "clear" and rng.random() < p["stale_p"]:
            fw = dict(r)
            fw["t_obs_min"] = r["t_obs_min"] + int(rng.uniform(60, 360))
            fw["reliability"] = float(rng.beta(4, 3))
            fw["is_stale"], fw["stale_of"] = True, j
            rows.append(fw)

    # ---- patrol --------------------------------------------------------------
    rep_df = pd.DataFrame(rows)
    road = np.flatnonzero(st["drivable"].to_numpy() & st["cls"].isin(["main", "minor"]).to_numpy())
    blockflags = F_DEBRIS | F_WIRE | F_COLLAPSE | F_LANDSLIDE | F_BRIDGE
    for t in range(T):
        wgt = np.ones(len(road))
        if len(rep_df):
            recent = rep_df[(rep_df["t_obs_min"] <= t * STEP_MIN) & (rep_df["t_obs_min"] > (t - 2) * STEP_MIN)]
            if len(recent):
                near = kd.query_ball_point(recent[["x", "y"]].to_numpy(), r=300)
                hot = np.unique(np.concatenate([np.asarray(v, int) for v in near])) if len(near) else []
                wgt[np.isin(road, hot)] += 20
        pick = rng.choice(road, size=min(p["patrol_per_step"], len(road)), replace=False, p=wgt / wgt.sum())
        for i in pick:
            d = float(depth[t, i])
            blk = d >= 0.3 or bool(tr.flags[t, i] & blockflags)
            if rng.random() < p["patrol_err_p"]:
                blk = not blk
            report("patrol", "status", t, int(i), cls_=int(depth_class(np.array([d]))[0]),
                   depth_est=max(0.0, d + rng.normal(0, 0.05)), blocked=float(blk), rel=0.97,
                   lag=int(rng.uniform(2, 10)), exact=True)

    # ---- official notices --------------------------------------------------------
    for e in tr.events.itertuples():
        if e.kind == "bridge_closure":
            report("official", "closure", e.t0, int(e.seg), rel=1.0, lag=int(rng.uniform(15, 60)), exact=True)
            if e.t1 < T:
                report("official", "reopen", e.t1, int(e.seg), rel=1.0, lag=int(rng.uniform(15, 60)), exact=True)
        elif e.kind in ("tree", "wire", "crash", "water_main") and e.t1 < T and rng.random() < 0.6:
            report("official", "cleared", e.t1, int(e.seg), rel=1.0, lag=int(rng.uniform(10, 40)), exact=True)

    reports = pd.DataFrame(rows)
    if len(reports):
        reports["_row"] = np.arange(len(reports))
        reports = reports.sort_values("t_obs_min", kind="stable").reset_index(drop=True)
        new_id = dict(zip(reports["_row"], reports.index))
        reports.insert(0, "obs_id", reports.index.to_numpy())
        reports["stale_of"] = [new_id[s] if s >= 0 else -1 for s in reports["stale_of"]]
        reports = reports.drop(columns="_row")
    else:
        reports = pd.DataFrame(columns=["obs_id"])

    # ---- sensors and gauges --------------------------------------------------------
    sens_rows = []
    und = st[(st["underpass"]) & st["drivable"]].copy()
    und["score"] = und["pond_m"] + 1.0
    und_pick = und.drop_duplicates("seg_id").sort_values(["score", "length_m"], ascending=False)
    und_pick = und_pick.loc[~und_pick.index.duplicated()].head(p["sensor_underpasses"]).index.to_numpy()
    rv = st[st["drivable"] & (st["dist_river_m"] < 150) & ~st["bridge"]].sort_values("hand_m")
    rv_pick = rv.head(p["sensor_riverside"]).index.to_numpy()
    sites = list(und_pick) + list(rv_pick)
    stuck = int(rng.choice(sites)) if rng.random() < 0.5 else -1
    stuck_from = int(rng.integers(T // 4, T // 2))
    for i in sites:
        for t in range(T):
            if rng.random() < p["sensor_drop_p"]:
                continue
            tt = t if not (i == stuck and t >= stuck_from) else stuck_from
            v = max(0.0, float(depth[tt, i]) + rng.normal(0, p["sensor_sd_m"]))
            sens_rows.append({"source": "sensor", "site": f"wl-{i}", "seg": int(i), "t_min": t * STEP_MIN,
                              "value_m": round(v, 3), "faulty": i == stuck and t >= stuck_from})
    for k, c in enumerate(CELLS):
        for t in range(T):
            if rng.random() < 0.02:
                continue
            sens_rows.append({"source": "gauge", "site": c, "seg": -1, "t_min": t * STEP_MIN,
                              "value_m": round(float(tr.stage[t, k]) + rng.normal(0, p["gauge_sd_m"]), 3),
                              "faulty": False})
    sensors = pd.DataFrame(sens_rows)

    # ---- traffic feed -----------------------------------------------------------------
    jam = tr.jam[:, road].astype(np.float32) / 250
    lvl = np.digitize(jam, [0.25, 0.5, 0.75]).astype(np.int16)
    flip = rng.random(lvl.shape) < p["traffic_flip_p"]
    lvl = np.where(flip, np.clip(lvl + rng.choice([-1, 1], size=lvl.shape), 0, 3), lvl)
    blocked = (depth[:, road] >= 0.3) | ((tr.flags[:, road] & blockflags) > 0)
    night = ((hours < 6) | (hours >= 23))[:, None]
    nodata = (blocked & (rng.random(lvl.shape) < 0.7)) | (night & (rng.random(lvl.shape) < 0.5))
    lvl = np.where(blocked & ~nodata, 3, lvl)
    lvl = np.where(nodata, 255, lvl).astype(np.uint8)

    meta = {"params": p, "counts": reports["source"].value_counts().to_dict() if len(reports) else {},
            "false_reports": int(reports["is_false"].sum()) if len(reports) else 0,
            "stale_reports": int(reports["is_stale"].sum()) if len(reports) else 0,
            "sensor_sites": [int(s) for s in sites], "stuck_sensor": stuck,
            "drones": "paused"}
    return Observations(reports, sensors, lvl, road.astype(np.int32), meta)
