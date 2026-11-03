"""Training rows for the passability model, from the simulated runs.

    python -m ml.samples            # -> data/train/passability.parquet

One row = (run, decision time t0, segment, unit, horizon):
    features  what the command centre could see at t0 (ml.features.build)
    label     blocked for that unit at t0 + horizon, by the router's own rules (ml.labels)

Decision times every 3 h from hour 3. Segments per decision are drawn from three
disjoint strata so the rare blocked roads are well represented:
    H  truth-hot: wet (>= 5 cm) or hazard-flagged now or within the next 3 h
    O  evidence-hot: within 400 m of a report in the last 3 h (and not in H)
    R  every other drivable segment
with up to 240 / 120 / 240 segments. Each row carries `w = 1/inclusion
probability`, so calibration and "natural" metrics can undo the oversampling.
Picking candidates uses the truth, but no feature does, and every stratum is
present at serving time anyway (we score all segments on a route).

Groups: rows keep storm_id, realization and split; the split is by storm year,
so no storm is on both sides.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

from app.nav.profiles import PROFILES
from ml.district import PROCESSED, SIM
from ml.features import HORIZONS_MIN, PROFILE_CODES, Evidence, build
from ml.labels import blocked, step_state
from ml.sim.static import load_static
from ml.sim.storms import STEP_MIN
from ml.sim.world import F_BRIDGE, F_COLLAPSE, F_DEBRIS, F_LANDSLIDE, F_WIRE, Truth, seed_of

OUT = PROCESSED.parent / "train"
STRATA = {"H": 240, "O": 120, "R": 240}
DECIDE_EVERY_H = 3


def run_rows(args: tuple[str, int, str]) -> pd.DataFrame:
    sid, k, split = args
    rng = np.random.default_rng(seed_of(sid, k) + 2)
    tr = Truth.load(sid, k)
    ev = Evidence.from_run(sid, k)
    st = load_static()
    drivable = st["drivable"].to_numpy()
    xy = st[["x", "y"]].to_numpy()
    T = tr.steps
    hmax = max(HORIZONS_MIN) // STEP_MIN
    hazmask = F_DEBRIS | F_WIRE | F_COLLAPSE | F_LANDSLIDE | F_BRIDGE
    profiles = list(PROFILE_CODES)
    label_cache: dict[tuple[int, str], np.ndarray] = {}
    state_cache: dict[int, dict] = {}

    def label(t: int, prof: str) -> np.ndarray:
        if (t, prof) not in label_cache:
            if t not in state_cache:
                state_cache[t] = step_state(tr, t)
            label_cache[(t, prof)] = blocked(PROFILES[prof], state_cache[t])[0]
        return label_cache[(t, prof)]

    from scipy.spatial import cKDTree
    tree = cKDTree(xy)
    out = []
    for t0 in range(int(DECIDE_EVERY_H * 60 / STEP_MIN), T - hmax, int(DECIDE_EVERY_H * 60 / STEP_MIN)):
        # Labels are only ever asked for t0..t0+hmax, so earlier world states can
        # go: keeping every step of a long storm was what ran workers out of memory.
        for t in [t for t in state_cache if t < t0]:
            del state_cache[t]
        for key in [key for key in label_cache if key[0] < t0]:
            del label_cache[key]
        win = slice(t0, min(T, t0 + 6))
        hot = ((tr.depth[win].astype(np.float32) >= 0.05).any(0) | ((tr.flags[win] & hazmask) > 0).any(0)) & drivable
        R = ev.reports[(ev.reports["t_obs_min"] <= t0 * STEP_MIN) & (ev.reports["t_obs_min"] > (t0 - 6) * STEP_MIN)]
        near = np.zeros(len(st), bool)
        if len(R):
            lists = tree.query_ball_point(R[["x", "y"]].to_numpy(), 400)
            idx = np.unique(np.concatenate([np.asarray(v, int) for v in lists if len(v)] or [np.array([], int)]))
            near[idx] = True
        strata = {"H": np.flatnonzero(hot), "O": np.flatnonzero(near & ~hot & drivable),
                  "R": np.flatnonzero(drivable & ~hot & ~near)}
        cand, w, sname = [], [], []
        for s, cap in STRATA.items():
            pool = strata[s]
            if not len(pool):
                continue
            m = min(cap, len(pool))
            pick = rng.choice(pool, size=m, replace=False)
            cand.append(pick); w.append(np.full(m, len(pool) / m)); sname += [s] * m
        if not cand:
            continue
        cand = np.concatenate(cand); w = np.concatenate(w)
        hz = rng.choice(HORIZONS_MIN, size=len(cand))
        pr = rng.choice(profiles, size=len(cand))
        lim = np.array([PROFILES[p].depth_limit_m for p in pr])
        ev.fc_noise = float(rng.lognormal(0, 0.4))
        F = build(ev, t0 * STEP_MIN, cand, hz, np.array([PROFILE_CODES[p] for p in pr]), lim)
        y = np.array([label(t0 + h // STEP_MIN, p)[c] for c, h, p in zip(cand, hz, pr)])
        y_now = np.array([label(t0, p)[c] for c, p in zip(cand, pr)])
        F["y"] = y.astype(np.int8)
        F["y_now"] = y_now.astype(np.int8)            # truth at t0: diagnostics only, never a feature
        F["w"] = w
        F["stratum"] = sname
        F["seg"] = cand
        F["t0_min"] = t0 * STEP_MIN
        F["storm_id"], F["realization"], F["split"] = sid, k, split
        out.append(F)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--storms", nargs="*")
    a = ap.parse_args()
    summ = pd.read_parquet(SIM / "runs" / "summary.parquet")
    if a.storms:
        summ = summ[summ["storm_id"].isin(a.storms)]
    jobs = list(zip(summ["storm_id"], summ["realization"].astype(int), summ["split"]))
    with ProcessPoolExecutor(a.workers) as ex:
        parts = list(ex.map(run_rows, jobs))
    df = pd.concat(parts, ignore_index=True)
    OUT.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT / "passability.parquet", index=False)
    print(df.groupby("split").agg(rows=("y", "size"), blocked=("y", "mean")).round(3).to_string())
    print(df.groupby(["profile", "horizon_min"])["y"].mean().unstack().round(3).to_string())


if __name__ == "__main__":
    main()
