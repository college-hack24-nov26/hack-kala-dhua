"""Route-level evaluation: does predicting accessibility change what happens to crews?

    python -m ml.route_eval                       # test storms -> data/eval/routes_*.{json,parquet}
    python -m ml.route_eval --splits train tune test --trips 12   # also the ETA training trips

For sampled trips (a real PCMC unit base -> a place that needs help, at a
decision time inside a held-out storm) three planners route the same trip on the
same road graph and the same evidence:

    current_status  closed = what the evidence says now (B0); costs from the
                    traffic feed; everything else assumed open
    predicted       current_status's closures stay binding, plus the passability
                    model's P(blocked at arrival) per segment, at the horizon the
                    unit would reach it: cost = time x (1 + 4p) + 900 s x p, and
                    p >= 0.6 is avoided outright
    oracle          the simulator's truth at departure (best possible, for scale)

Each route is then *driven* through the simulated truth, step by step. When the
unit reaches a segment that is blocked at that moment, the route was invalid:
it turns back (60 s), marks that segment closed and replans from where it is.
After 6 replans, or with no road left, the last stretch is done on foot
(rescue-team pace and limits). Reported: invalid-route rate, replans, arrival
time, delay against the oracle, trips that ended on foot.

Every trip also records what was known at departure, which is the ETA model's
training data (ml.train_eta).
"""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache

import numpy as np
import pandas as pd
from scipy import sparse
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree

from app.nav.profiles import PROFILES
from ml.district import PROCESSED, SIM
from ml.features import PROFILE_CODES, Evidence, build
from ml.labels import seconds, static_arrays, step_state
from ml.sim.static import load_static
from ml.sim.storms import STEP_MIN
from ml.sim.world import Truth, seed_of

EVAL = PROCESSED.parent / "eval"
AVOID_P = 0.6
DETOUR_S = 900.0
TURNBACK_S = 60.0
MAX_REPLANS = 6

#: Unit bases from migration 029 (lon, lat, profile).
BASES = [
    (73.77174, 18.65925, "fire_engine"), (73.81805, 18.62276, "fire_engine"), (73.77881, 18.61219, "fire_engine"),
    (73.78172, 18.59624, "fire_engine"), (73.81780, 18.66441, "fire_engine"),
    (73.82116, 18.62203, "ambulance"), (73.77499, 18.62584, "ambulance"), (73.77320, 18.65618, "ambulance"),
    (73.82197, 18.62317, "ambulance"), (73.75716, 18.64646, "ambulance"),
]
#: Mobilisation (call -> wheels rolling), minutes: lognormal (median, sigma). Assumption.
MOBILISE = {"ambulance": (2.0, 0.5), "fire_engine": (1.5, 0.4), "rescue_team": (5.0, 0.5)}


@lru_cache(maxsize=1)
def graph():
    seg = pd.read_parquet(PROCESSED / "graph" / "segments.parquet", columns=["u", "v", "oneway"])
    nodes, inv = np.unique(np.r_[seg["u"].to_numpy(), seg["v"].to_numpy()], return_inverse=True)
    n = len(seg)
    u, v = inv[:n], inv[n:]
    ow = seg["oneway"].to_numpy()
    nod = pd.read_parquet(PROCESSED / "graph" / "nodes.parquet").set_index("node_id").loc[nodes]
    return u, v, ow, len(nodes), nod[["lon", "lat"]].to_numpy()


def edges(mode: str):
    """Directed edges (a, b, segment) for road or foot movement."""
    u, v, ow, _, _ = graph()
    g = static_arrays()
    ok = g["drivable"] if mode == "road" else g["walkable"]
    i = np.flatnonzero(ok)
    fwd = i if mode != "road" else i[ow[i] >= 0]
    bwd = i if mode != "road" else i[ow[i] <= 0]
    a = np.r_[u[fwd], v[bwd]]
    b = np.r_[v[fwd], u[bwd]]
    s = np.r_[fwd, bwd]
    return a, b, s


def shortest(cost: np.ndarray, mode: str, src: int):
    """Dijkstra over segment costs (inf = unusable). Returns dist, pred node, pred segment per node."""
    _, _, _, N, _ = graph()
    a, b, s = edges(mode)
    w = cost[s]
    ok = np.isfinite(w)
    a, b, s, w = a[ok], b[ok], s[ok], np.maximum(w[ok], 0.01)
    order = np.lexsort((w, b, a))
    a, b, s, w = a[order], b[order], s[order], w[order]
    first = np.r_[True, (a[1:] != a[:-1]) | (b[1:] != b[:-1])]
    a, b, s, w = a[first], b[first], s[first], w[first]
    m = sparse.csr_matrix((w, (a, b)), shape=(N, N))
    dist, pred = dijkstra(m, indices=src, return_predecessors=True)
    key = pd.Series(s, index=a.astype(np.int64) * N + b)
    return dist, pred, key


def path(pred: np.ndarray, key: pd.Series, src: int, dst: int) -> list[tuple[int, int]] | None:
    """[(segment, from_node)] from src to dst."""
    _, _, _, N, _ = graph()
    if pred[dst] < 0 and dst != src:
        return None
    out, x = [], dst
    while x != src:
        p_ = pred[x]
        out.append((int(key[p_ * N + x]), int(p_)))
        x = p_
    return out[::-1]


class Run:
    """Truth and evidence of one simulated run, with cached per-step costs."""

    def __init__(self, sid: str, k: int):
        self.sid, self.k = sid, k
        self.tr = Truth.load(sid, k)
        self.ev = Evidence.from_run(sid, k)
        self._true: dict[tuple[int, str], np.ndarray] = {}

    def true_secs(self, step: int, prof: str) -> np.ndarray:
        step = min(step, self.tr.steps - 1)
        if (step, prof) not in self._true:
            self._true[(step, prof)] = seconds(PROFILES[prof], step_state(self.tr, step))
        return self._true[(step, prof)]


def known_state(run: Run, t_step: int, F: pd.DataFrame, cand: np.ndarray) -> dict[str, np.ndarray]:
    """What the planner believes now: closures from the evidence, traffic from the feed, nothing else."""
    n = len(load_static())
    jam = np.zeros(n, np.float32)
    lvl = F["traffic_level"].to_numpy()
    jam[cand] = np.where(np.isnan(lvl), 0.3, lvl / 3 * 0.9)
    closed = np.zeros(n, bool)
    closed[cand] = F["status_now"].to_numpy() > 0
    z = np.zeros(n, bool)
    return {"depth": np.zeros(n, np.float32), "flowing": z, "debris": z, "wire": z, "collapse": z,
            "landslide": z, "bridge_closed": z, "fire_d": np.full(n, np.inf, np.float32),
            "gas_d": np.full(n, np.inf, np.float32), "jam": jam, "crowd": np.zeros(n, np.float32),
            "rain": np.full(n, float(run.ev.rain[t_step]), np.float32), "confirmed_closed": closed}


def drive(run: Run, prof: str, t_dep_min: int, src: int, dst: int, plan_cost, mode: str = "road") -> dict:
    """Follow plan_cost's route through the truth, replanning on surprises."""
    known_closed: set[int] = set()
    t = 0.0
    node = src
    replans, invalid, length = 0, 0, 0.0
    g = static_arrays()
    while True:
        cost = plan_cost(known_closed)
        dist, pred, key = shortest(cost, mode, node)
        route = path(pred, key, node, dst) if np.isfinite(dist[dst]) else None
        if route is None:
            break
        blocked_at = None
        for s, frm in route:
            step = (t_dep_min * 60 + t) // (STEP_MIN * 60)
            secs = run.true_secs(int(step), prof)[s]
            if not np.isfinite(secs):
                blocked_at = (s, frm)
                break
            t += secs
            length += g["length_m"][s]
        if blocked_at is None:
            return {"arrived": True, "on_foot": False, "travel_s": t, "replans": replans, "invalid": invalid > 0,
                    "length_m": length}
        invalid += 1
        replans += 1
        known_closed.add(blocked_at[0])
        node = blocked_at[1]
        t += TURNBACK_S
        if replans > MAX_REPLANS:
            break
    # last stretch on foot
    step = (t_dep_min * 60 + t) // (STEP_MIN * 60)
    foot = run.true_secs(int(step), "rescue_team")
    dist, _, _ = shortest(foot, "foot", node)
    ok = np.isfinite(dist[dst])
    return {"arrived": bool(ok), "on_foot": True, "travel_s": t + (dist[dst] if ok else np.nan), "replans": replans,
            "invalid": invalid > 0, "length_m": length}


def trips_for_run(args: tuple[str, int, str, int, str | None]) -> pd.DataFrame:
    sid, k, split, n_trips, model_dir = args
    from pathlib import Path

    from ml.train_passability import Ensemble
    ens = Ensemble.load(Path(model_dir)) if model_dir else None
    rng = np.random.default_rng(seed_of(sid, k) + 3)
    run = Run(sid, k)
    st = load_static()
    _, _, _, N, nxy = graph()
    u, v, *_ = graph()
    kd = cKDTree(nxy)
    drivable = static_arrays()["drivable"]
    cand = np.flatnonzero(drivable)
    rows = []
    T = run.tr.steps
    for t_step in range(12, T - 6, 12):                      # every 6 h
        t0 = t_step * STEP_MIN
        hot = np.flatnonzero(((run.tr.depth[t_step:t_step + 3].astype(np.float32) >= 0.1).any(0)) & drivable)
        for prof in ("ambulance", "fire_engine"):
            pc = PROFILE_CODES[prof]
            lim = PROFILES[prof].depth_limit_m
            # features once per (decision, unit class): all drivable segments x 3 horizons
            reps = np.tile(cand, 3)
            hz = np.repeat([30, 60, 90], len(cand))
            run.ev.fc_noise = float(rng.lognormal(0, 0.4))
            F = build(run.ev, t0, reps, hz, np.full(len(reps), pc), np.full(len(reps), lim))
            Fn = F.iloc[: len(cand)]
            pmat = None
            if ens is not None:
                p, _ = ens.predict(F)
                pmat = p.reshape(3, len(cand))
            S_known = known_state(run, t_step, Fn, cand)
            base = seconds(PROFILES[prof], S_known)
            bases = [b for b in BASES if b[2] == prof]
            for _ in range(n_trips // 2):
                bx = bases[rng.integers(len(bases))]
                src = int(kd.query([bx[0], bx[1]])[1])
                ds = int(rng.choice(hot)) if (len(hot) and rng.random() < 0.6) else int(rng.choice(cand))
                dst = int(u[ds])
                if src == dst:
                    continue
                # offsets from the origin decide each segment's horizon
                d0, _, _ = shortest(base, "road", src)
                if not np.isfinite(d0[dst]):
                    continue
                off_min = d0[u[cand]] / 60
                hidx = np.clip(np.digitize(off_min, [45, 75]), 0, 2)
                p_seg = np.zeros(len(st))
                if pmat is not None:
                    p_seg[cand] = pmat[hidx, np.arange(len(cand))]

                def cs(known: set[int]) -> np.ndarray:
                    c = base.copy()
                    if known:
                        c[list(known)] = np.inf
                    return c

                def pred_cost(known: set[int]) -> np.ndarray:
                    c = base * (1 + 4 * p_seg) + DETOUR_S * p_seg
                    c[p_seg >= AVOID_P] = np.inf
                    if known:
                        c[list(known)] = np.inf
                    return c

                truth0 = run.true_secs(t_step, prof)

                def oracle(known: set[int]) -> np.ndarray:
                    c = truth0.copy()
                    if known:
                        c[list(known)] = np.inf
                    return c

                mob = float(rng.lognormal(np.log(MOBILISE[prof][0]), MOBILISE[prof][1]))
                rec = {"storm_id": sid, "realization": k, "split": split, "t0_min": t0, "profile": prof,
                       "src": src, "dst": dst, "dst_hot": bool(ds in set(hot)), "mobilise_min": mob,
                       "crow_km": float(np.hypot((nxy[src, 0] - nxy[dst, 0]) * 105.5, (nxy[src, 1] - nxy[dst, 1]) * 110.9)),
                       "free_min": float(d0[dst] / 60)}
                planners = {"current_status": cs, "oracle": oracle}
                if ens is not None:
                    planners["predicted"] = pred_cost
                for name, fn in planners.items():
                    r = drive(run, prof, t0, src, dst, fn)
                    for kk, vv in r.items():
                        rec[f"{name}_{kk}"] = vv
                # what was known at departure along the predicted (or current-status) plan: ETA features
                plan_fn = pred_cost if ens is not None else cs
                dist, pred_, key = shortest(plan_fn(set()), "road", src)
                route = path(pred_, key, src, dst) or []
                segs = np.array([s for s, _ in route], int)
                if len(segs):
                    posF = np.searchsorted(cand, segs)
                    rec.update({
                        "plan_km": float(static_arrays()["length_m"][segs].sum() / 1000),
                        "plan_free_min": float(base[segs].sum() / 60),
                        "plan_signals": int(static_arrays()["signals"][segs].sum()),
                        "plan_underpasses": int(st["underpass"].to_numpy()[segs].sum()),
                        "plan_bridges": int(st["bridge"].to_numpy()[segs].sum()),
                        "plan_p_sum": float(p_seg[segs].sum()), "plan_p_max": float(p_seg[segs].max()),
                        "plan_traffic": float(np.nanmean(Fn["traffic_level"].to_numpy()[posF])) if len(posF) else np.nan,
                        "plan_nodata": float(Fn["traffic_nodata"].to_numpy()[posF].mean()),
                        "plan_flood_reports": float(Fn["flood_150_3h"].to_numpy()[posF].sum()),
                        "rain_1h": float(Fn["rain_1h"].iloc[0]), "rain_fc": float(F["rain_fc"].iloc[0]),
                        "stage_frac_max": float(np.nanmax(Fn["stage_frac"].to_numpy()[posF])),
                        "hour": int(Fn["hour"].iloc[0]),
                    })
                rows.append(rec)
    return pd.DataFrame(rows)


def summarise(df: pd.DataFrame) -> dict:
    out = {"trips": int(len(df)), "to_flooded_places": int(df["dst_hot"].sum())}
    for name in ("current_status", "predicted", "oracle"):
        if f"{name}_arrived" not in df:
            continue
        tot = df["mobilise_min"] + df[f"{name}_travel_s"] / 60
        out[name] = {
            "invalid_route_rate": round(float(df[f"{name}_invalid"].mean()), 4),
            "mean_replans": round(float(df[f"{name}_replans"].mean()), 3),
            "ended_on_foot": round(float(df[f"{name}_on_foot"].mean()), 4),
            "not_reached": round(float((~df[f"{name}_arrived"]).mean()), 4),
            "arrival_p50_min": round(float(tot.median()), 2),
            "arrival_p90_min": round(float(tot.quantile(0.9)), 2),
        }
        if name != "oracle":
            dl = (df[f"{name}_travel_s"] - df["oracle_travel_s"]) / 60
            out[name]["delay_vs_oracle_mean_min"] = round(float(dl.mean()), 2)
            out[name]["delay_vs_oracle_p90_min"] = round(float(dl.quantile(0.9)), 2)
    if "predicted_travel_s" in df:
        d = (df["current_status_travel_s"] - df["predicted_travel_s"]) / 60
        out["minutes_saved_by_prediction"] = {"mean": round(float(d.mean()), 2), "p90": round(float(d.quantile(0.9)), 2),
                                              "share_faster": round(float((d > 0.5).mean()), 3),
                                              "share_slower": round(float((d < -0.5).mean()), 3)}
        hot = df[df["dst_hot"]]
        out["to_flooded_places_only"] = {n: {"invalid_route_rate": round(float(hot[f"{n}_invalid"].mean()), 4),
                                             "arrival_p90_min": round(float((hot["mobilise_min"] + hot[f"{n}_travel_s"] / 60).quantile(0.9)), 2)}
                                         for n in ("current_status", "predicted", "oracle")}
    return out


def latest_model() -> str | None:
    d = PROCESSED.parent.parent / "models" / "passability"
    vs = sorted(p for p in d.glob("pass-*") if (p / "model.json").exists()) if d.exists() else []
    return str(vs[-1]) if vs else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--splits", nargs="*", default=["test"])
    ap.add_argument("--trips", type=int, default=10, help="trips per decision time per unit class x2")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--model", default=None)
    ap.add_argument("--realizations", type=int, nargs="*", default=None, help="limit to these realizations")
    a = ap.parse_args()
    model = a.model or latest_model()
    summ = pd.read_parquet(SIM / "runs" / "summary.parquet")
    summ = summ[summ["split"].isin(a.splits)]
    if a.realizations is not None:
        summ = summ[summ["realization"].isin(a.realizations)]
    jobs = [(s, int(k), sp, a.trips, model) for s, k, sp in zip(summ["storm_id"], summ["realization"], summ["split"])]
    with ProcessPoolExecutor(a.workers) as ex:
        df = pd.concat(list(ex.map(trips_for_run, jobs)), ignore_index=True)
    EVAL.mkdir(parents=True, exist_ok=True)
    tag = "_".join(a.splits)
    df.to_parquet(EVAL / f"routes_{tag}.parquet", index=False)
    res = {"model": model, "splits": a.splits, **summarise(df[df["split"] == "test"] if "test" in a.splits else df)}
    json.dump(res, open(EVAL / f"routes_{tag}.json", "w"), indent=1)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
