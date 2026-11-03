"""The re-learning loop: keep the passability model honest with what crews find.

    python -m ml.relearn status
    python -m ml.relearn register --task passability --version pass-... --champion
    python -m ml.relearn check     [--source db | --examples file.parquet]
    python -m ml.relearn run       [--source db | --examples file.parquet] [--dry-run]
    python -m ml.relearn demo      # the whole loop on simulated "real" outcomes, labelled as such

How it works (also in docs: "Training & re-learning, step by step"):

1. Log.   Every prediction the router uses is stored with its exact inputs
          (nav_predictions). Every trusted observation of a road is an outcome
          (nav_outcomes): crew/drone pins in road_blocks (trigger), patrol and
          official status, sensors, and roads a unit actually drove (open).
2. Join.  nav_examples pairs a prediction with the outcome nearest the moment it
          was about (made_at + horizon, +-15 min, within the outcome's radius).
3. Check. Drift (PSI of the main inputs vs training data) and calibration on the
          last N real examples. Ranking still good but probabilities off ->
          recalibrate only (minutes). Ranking worse, or enough new data ->
          retrain.
4. Train. Challenger = same code as the first model (ml.train_passability.train)
          on simulated storms + real examples, real ones up-weighted (x5 by
          default) and decayed with age (half-life 180 days).
5. Gate.  Champion and challenger are scored on (a) the frozen simulated test
          storms and (b) the most recent 30% of real examples, which the
          challenger never trained on. Promote only if it is better on recent
          real outcomes (Brier), not worse at catching closures (recall at the
          champion's threshold), not worse on underpasses, and not worse on the
          frozen test beyond a small tolerance. Every decision is written down.
6. Serve. The registry names one champion per task; the API loads it. The old
          champion stays on disk for rollback.

Real labels are biased: we see the roads we drive and patrol. The loop keeps
'shadow' predictions (scored but not used) and patrol spot-checks so negatives
are not only "roads we chose". When real data is scarce, simulation stays the
bulk of training; it is never thrown away.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import time
from pathlib import Path

import numpy as np
import pandas as pd

from ml.district import PROCESSED, SIM
from ml.features import FEATURES, PROFILE_CODES, Evidence, build
from ml.train_passability import MODELS, Ensemble, best_threshold, ece, metrics, train

DATA = PROCESSED.parent
REGISTRY = DATA.parent / "models" / "registry.json"
RELEARN = DATA / "relearn"

GATES = {"min_examples": 300, "min_positives": 30, "real_weight": 5.0, "half_life_days": 180,
         "recent_share": 0.3, "frozen_auc_tolerance": 0.005, "frozen_ece_tolerance": 0.005,
         "min_brier_gain": 0.02, "recall_tolerance": 0.02, "psi_alert": 0.25, "ece_alert": 0.05}
TOP_DRIFT = ["rain_3h", "rain_fc", "stage_frac", "freeboard_m", "flood_150_3h", "traffic_level",
             "sensor_val", "hand_m", "pond_m", "horizon_min"]


# ------------------------------------------------------------------ registry --
def registry() -> dict:
    return json.loads(REGISTRY.read_text()) if REGISTRY.exists() else {"passability": {"champion": None, "history": []},
                                                                    "eta": {"champion": None, "history": []}}


def save_registry(r: dict) -> None:
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY.write_text(json.dumps(r, indent=1))


def record(task: str, version: str, status: str, decision: dict, metrics_: dict | None = None) -> None:
    r = registry()
    t = r.setdefault(task, {"champion": None, "history": []})
    if status == "champion":
        if t["champion"] and t["champion"] != version:
            t["history"].append({"version": t["champion"], "status": "retired", "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        t["champion"] = version
    t["history"].append({"version": version, "status": status, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                         "decision": decision, "metrics": metrics_ or {}})
    save_registry(r)
    _db_record(task, version, status, decision, metrics_ or {})


def _db_record(task, version, status, decision, metrics_) -> None:
    """Mirror to model_registry when a database is configured (migration 031)."""
    try:
        from app.core.config import settings
        if not settings.dsn:
            return
        import asyncpg

        async def go():
            c = await asyncpg.connect(settings.dsn, statement_cache_size=0)
            try:
                async with c.transaction():
                    if status == "champion":
                        await c.execute("update model_registry set status='retired', retired_at=now() "
                                        "where task=$1 and status='champion' and version<>$2", task, version)
                    await c.execute(
                        "insert into model_registry (version, task, status, metrics, decision, artifact_uri, promoted_at) "
                        "values ($1,$2,$3,$4::jsonb,$5::jsonb,$6, case when $3='champion' then now() end) "
                        "on conflict (version) do update set status=excluded.status, metrics=excluded.metrics, "
                        "decision=excluded.decision, promoted_at=coalesce(model_registry.promoted_at, excluded.promoted_at)",
                        version, task, status, json.dumps(metrics_), json.dumps(decision), f"models/{task}/{version}")
            finally:
                await c.close()
        asyncio.run(go())
    except Exception as exc:  # noqa: BLE001 - the local registry is the source of truth offline
        print(f"(model_registry not updated: {exc.__class__.__name__}: {exc})")


# ------------------------------------------------------------------ examples --
def load_examples(source: str | None, path: str | None) -> pd.DataFrame:
    """Labelled real examples: FEATURES + y + made_at + profile + horizon_min."""
    if path:
        return pd.read_parquet(path)
    if source == "db":
        from app.core.config import settings
        import asyncpg

        async def go():
            c = await asyncpg.connect(settings.dsn, statement_cache_size=0)
            try:
                return await c.fetch("select prediction_id, model_version, made_at, profile, horizon_min, p_blocked, "
                                     "features::text as features, label, label_source, confidence from nav_examples "
                                     "where sim_run_id is null")
            finally:
                await c.close()
        rows = asyncio.run(go())
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame([dict(r) for r in rows])
        feats = pd.DataFrame([json.loads(f or "{}") for f in df.pop("features")])
        df = pd.concat([df, feats], axis=1)
        df["y"] = df.pop("label").astype(int)
        return df
    raise SystemExit("give --examples file.parquet or --source db")


def psi(a: np.ndarray, b: np.ndarray, bins: int = 10) -> float:
    a, b = a[np.isfinite(a)], b[np.isfinite(b)]
    if len(a) < 50 or len(b) < 50:
        return float("nan")
    edges = np.unique(np.quantile(a, np.linspace(0, 1, bins + 1)))
    if len(edges) < 3:
        return 0.0
    pa = np.histogram(a, edges)[0] / len(a) + 1e-4
    pb = np.histogram(np.clip(b, edges[0], edges[-1]), edges)[0] / len(b) + 1e-4
    return float(np.sum((pb - pa) * np.log(pb / pa)))


def check(ex: pd.DataFrame, champion: Ensemble, sim_train: pd.DataFrame) -> dict:
    out = {"examples": int(len(ex)), "positives": int(ex["y"].sum()) if len(ex) else 0, "reasons": []}
    if len(ex) < 50:
        out["action"] = "wait"
        out["reasons"].append("fewer than 50 labelled real examples")
        return out
    p, _ = champion.predict(ex)
    y = ex["y"].to_numpy()
    out["champion_on_real"] = {"brier": round(float(np.mean((p - y) ** 2)), 4), "ece": round(ece(y, p), 4)}
    if len(np.unique(y)) > 1:
        from sklearn.metrics import roc_auc_score
        out["champion_on_real"]["roc_auc"] = round(float(roc_auc_score(y, p)), 4)
    out["psi"] = {f: round(psi(sim_train[f].to_numpy(float), ex[f].to_numpy(float)), 3) for f in TOP_DRIFT if f in ex}
    drifted = [f for f, v in out["psi"].items() if np.isfinite(v) and v > GATES["psi_alert"]]
    if drifted:
        out["reasons"].append(f"input drift (PSI > {GATES['psi_alert']}): {', '.join(drifted)}")
    auc_ok = out["champion_on_real"].get("roc_auc", 1.0) >= 0.85
    if out["champion_on_real"]["ece"] > GATES["ece_alert"]:
        out["reasons"].append(f"miscalibrated on real outcomes (ECE {out['champion_on_real']['ece']})")
    if not auc_ok:
        out["reasons"].append(f"ranking degraded (AUC {out['champion_on_real'].get('roc_auc')})")
    enough = len(ex) >= GATES["min_examples"] and out["positives"] >= GATES["min_positives"]
    if (drifted or not auc_ok) and enough:
        out["action"] = "retrain"
    elif out["champion_on_real"]["ece"] > GATES["ece_alert"]:
        out["action"] = "recalibrate"
    elif enough:
        out["action"] = "retrain_scheduled"
    else:
        out["action"] = "ok"
    return out


# ----------------------------------------------------------------------- run --
def run(ex: pd.DataFrame, dry_run: bool = False, members: int = 3, sim_frac: float = 1.0) -> dict:
    reg = registry()
    champ_v = reg["passability"]["champion"]
    if not champ_v:
        raise SystemExit("no passability champion: python -m ml.relearn register --task passability --version ... --champion")
    champ = Ensemble.load(MODELS / champ_v)
    champ_meta = json.load(open(MODELS / champ_v / "model.json"))
    sim = pd.read_parquet(DATA / "train" / "passability.parquet")
    decision: dict = {"champion": champ_v, "gates": GATES}

    chk = check(ex, champ, sim[sim["split"] == "train"].sample(min(200_000, len(sim)), random_state=0))
    decision["check"] = chk
    if len(ex) < GATES["min_examples"] or int(ex["y"].sum()) < GATES["min_positives"]:
        decision["outcome"] = "not enough real outcomes yet"
        return decision

    ex = ex.sort_values("made_at").reset_index(drop=True)
    cut = int(len(ex) * (1 - GATES["recent_share"]))
    old, recent = ex.iloc[:cut].copy(), ex.iloc[cut:].copy()
    age_days = (pd.Timestamp(ex["made_at"].max()) - pd.to_datetime(old["made_at"])).dt.total_seconds() / 86400
    old["train_w"] = GATES["real_weight"] * 0.5 ** (age_days.to_numpy() / GATES["half_life_days"])
    rng = np.random.default_rng(0)
    old["split"] = np.where(rng.random(len(old)) < 0.25, "tune", "train")
    old["w"] = 1.0
    simtr = sim[sim["split"].isin(["train", "tune"])]
    simtr = (simtr.sample(frac=sim_frac, random_state=2) if sim_frac < 1 else simtr).copy()
    simtr["train_w"] = 1.0
    df = pd.concat([simtr, old[FEATURES + ["y", "w", "train_w", "split"]]], ignore_index=True)
    t0 = time.time()
    chal, info = train(df, members)
    tu = df[df["split"] == "tune"]
    p_tu, _ = chal.predict(tu)
    thr = best_threshold(tu["y"].to_numpy(), p_tu, tu["w"].to_numpy(), 0.9)

    frozen = sim[sim["split"] == "test"].sample(min(150_000, (sim["split"] == "test").sum()), random_state=1)
    res = {}
    for name, m, th in (("champion", champ, champ_meta["threshold"]), ("challenger", chal, thr)):
        pf, _ = m.predict(frozen)
        pr, _ = m.predict(recent)
        res[name] = {"frozen_test": metrics(frozen["y"].to_numpy(), pf, frozen["w"].to_numpy(), th),
                     "recent_real": metrics(recent["y"].to_numpy(), pr, np.ones(len(recent)), th)}
        und = recent["underpass"].to_numpy() > 0
        if und.sum() >= 20 and len(np.unique(recent["y"].to_numpy()[und])) > 1:
            res[name]["recent_underpasses"] = metrics(recent["y"].to_numpy()[und], pr[und], np.ones(und.sum()), th)
    c, h = res["champion"], res["challenger"]
    gates = {
        "better_on_recent_real": h["recent_real"]["brier_natural"] <= c["recent_real"]["brier_natural"] * (1 - GATES["min_brier_gain"]),
        "catches_closures": h["recent_real"]["recall"] >= c["recent_real"]["recall"] - GATES["recall_tolerance"],
        "frozen_auc_ok": h["frozen_test"].get("roc_auc", 0) >= c["frozen_test"].get("roc_auc", 0) - GATES["frozen_auc_tolerance"],
        "frozen_ece_ok": h["frozen_test"]["ece_natural"] <= c["frozen_test"]["ece_natural"] + GATES["frozen_ece_tolerance"],
        "underpasses_ok": ("recent_underpasses" not in h or "recent_underpasses" not in c
                           or h["recent_underpasses"]["recall"] >= c["recent_underpasses"]["recall"] - GATES["recall_tolerance"]),
    }
    promote = all(gates.values())
    version = time.strftime("pass-%Y%m%d-%H%M") + "-rl"
    decision.update({"challenger": version, "seconds": round(time.time() - t0), "train_info": info,
                     "real_used": {"train_tune": int(len(old)), "recent_holdout": int(len(recent))},
                     "results": res, "gate_results": gates, "outcome": "promoted" if promote else "rejected"})
    if not dry_run:
        meta = {"version": version, "threshold": thr, "parent": champ_v, "relearn": True,
                "trained_on": "simulated storms + real outcomes", **info, "test": h["frozen_test"]}
        chal.save(MODELS / version, meta)
        record("passability", version, "champion" if promote else "rejected", decision, h)
    RELEARN.mkdir(parents=True, exist_ok=True)
    (RELEARN / f"decision_{version}.json").write_text(json.dumps(decision, indent=1, default=str))
    return decision


def recalibrate(ex: pd.DataFrame) -> dict:
    """Cheap fix when ranking is fine but probabilities drifted: refit isotonic on recent real outcomes."""
    from sklearn.isotonic import IsotonicRegression
    champ_v = registry()["passability"]["champion"]
    champ = Ensemble.load(MODELS / champ_v)
    raw, _ = champ.raw(ex)
    iso = IsotonicRegression(out_of_bounds="clip").fit(raw, ex["y"])
    new = Ensemble(champ.members, iso, champ.features)
    version = f"{champ_v}-recal{time.strftime('%m%d%H%M')}"
    meta = json.load(open(MODELS / champ_v / "model.json")) | {"version": version, "parent": champ_v,
                                                                "recalibrated_on": int(len(ex))}
    new.save(MODELS / version, meta)
    dec = {"outcome": "recalibrated", "parent": champ_v, "examples": int(len(ex))}
    record("passability", version, "champion", dec)
    return dec


# ---------------------------------------------------------------------- demo --
def corroborated_label(sightings: pd.DataFrame, reports: pd.DataFrame) -> np.ndarray:
    """Outcome label from patrol sightings, with the corroboration rule real labels need.

    Closed roads are rare (about 1 in 100 patrolled roads), so even a 97%-right
    patrol produces more false "blocked" sightings than true ones. A "blocked"
    sighting therefore counts only if something backs it: the patrol's own depth
    estimate is at least 25 cm, or another source (citizen hazard report,
    official notice, a second patrol) put a blockage within 100 m in the 3 h
    before. In production the same rule decides nav_outcomes.confidence
    (>= 0.8 corroborated, 0.6 single-source) and nav_examples keeps only >= 0.8.
    """
    from scipy.spatial import cKDTree
    y = sightings["blocked_est"].fillna(0).to_numpy().astype(int)
    deep = sightings["depth_est_m"].fillna(0).to_numpy() >= 0.25
    other = reports[(reports["source"].isin(["citizen", "official", "patrol"]))
                    & (reports["kind"].isin(["tree", "wire", "collapse", "landslide", "closure"])
                       | ((reports["source"] == "patrol") & (reports["blocked_est"] == 1)))]
    ok = deep.copy()
    if len(other):
        tree = cKDTree(other[["x", "y"]].to_numpy())
        for i, (x, yy, t) in enumerate(sightings[["x", "y", "t_obs_min"]].to_numpy()):
            if y[i] and not ok[i]:
                for j in tree.query_ball_point([x, yy], 100):
                    r = other.iloc[j]
                    if t - 180 <= r["t_obs_min"] < t and not (r["x"] == x and r["y"] == yy and r["t_obs_min"] == t):
                        ok[i] = True
                        break
    return np.where(y == 1, ok.astype(int), 0)


def demo_examples(storm: str, realization: int, champion: Ensemble, shift: bool) -> pd.DataFrame:
    """Simulated stand-in for real outcomes (until real ones exist), clearly labelled.

    Re-simulates a held-out storm, optionally in a *shifted* world (drains far
    more blocked than the simulator assumed, as after a bad pre-monsoon desilting),
    lets the champion predict the way the router would, and takes the patrols'
    later sightings of those roads as the outcomes - noisy, like real labels.
    """
    from ml.sim.observe import observe
    from ml.sim.world import simulate
    params = {"blocked_drain_p": (0.45, 0.6), "drain_mmph": {"main": 30.0, "minor": 24.0, "local": 20.0, "path": 18.0}} if shift else None
    tr = simulate(storm, realization, params)
    root = RELEARN / "demo_runs"
    d = tr.save(root)
    ob = observe(tr)
    ob.save(d)
    from ml.sim.observe import Observations
    o = Observations.load(d)
    drv = pd.read_parquet(SIM / "drivers" / f"{storm}.parquet")
    ev = Evidence(o.reports.drop(columns=[c for c in o.reports.columns if c not in
                                         ("source", "kind", "t_obs_min", "x", "y", "seg_rep", "depth_class",
                                          "depth_est_m", "blocked_est", "reliability")]),
                  o.sensors[["source", "site", "seg", "t_min", "value_m"]], o.traffic, o.traffic_segs,
                  drv["rain_mmph"].to_numpy(np.float32), drv["upstream_mmph"].to_numpy(np.float32),
                  rain_future=drv["rain_mmph"].to_numpy(np.float32), start=pd.Timestamp(drv["time"].iloc[0]))
    pat = o.reports[o.reports["source"] == "patrol"]
    rng = np.random.default_rng(5)
    rows = []
    base = pd.Timestamp(drv["time"].iloc[0])
    for t_obs, g in pat.groupby(pat["t_obs_min"] // 30 * 30):
        h = int(rng.choice([30, 60, 90]))
        t_pred = int(t_obs - h)
        if t_pred < 60:
            continue
        segs = g["seg_rep"].to_numpy()
        F = build(ev, t_pred, segs, np.full(len(segs), h), np.full(len(segs), PROFILE_CODES["ambulance"]),
                  np.full(len(segs), 0.3))
        F["y"] = corroborated_label(g, o.reports)
        F["made_at"] = base + pd.Timedelta(minutes=t_pred)
        F["profile_name"] = "ambulance"
        rows.append(F)
    ex = pd.concat(rows, ignore_index=True)
    ex["source"] = "simulated-outcomes" + ("-shifted-world" if shift else "")
    return ex


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["status", "register", "check", "run", "recalibrate", "demo"])
    ap.add_argument("--task", default="passability")
    ap.add_argument("--version")
    ap.add_argument("--champion", action="store_true")
    ap.add_argument("--source", choices=["db"])
    ap.add_argument("--examples")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--members", type=int, default=3)
    ap.add_argument("--sim-frac", type=float, default=1.0, help="share of simulated rows to retrain on (speed)")
    ap.add_argument("--storms", nargs="*", default=["20250927-18", "20260722-14", "20240823-18"])
    a = ap.parse_args()

    if a.cmd == "status":
        print(json.dumps(registry(), indent=1)[:4000])
    elif a.cmd == "register":
        src = (MODELS if a.task == "passability" else MODELS.parent / "eta") / a.version
        if not src.exists():
            raise SystemExit(f"no model at {src}")
        record(a.task, a.version, "champion" if a.champion else "candidate", {"outcome": "registered by hand"})
        print(f"{a.task}: {a.version} -> {'champion' if a.champion else 'candidate'}")
    elif a.cmd == "check":
        ex = load_examples(a.source, a.examples)
        champ = Ensemble.load(MODELS / registry()["passability"]["champion"])
        sim = pd.read_parquet(DATA / "train" / "passability.parquet", columns=FEATURES + ["split"])
        print(json.dumps(check(ex, champ, sim[sim["split"] == "train"].sample(200_000, random_state=0)), indent=1))
    elif a.cmd == "run":
        print(json.dumps(run(load_examples(a.source, a.examples), a.dry_run, a.members, a.sim_frac), indent=1, default=str)[:6000])
    elif a.cmd == "recalibrate":
        print(json.dumps(recalibrate(load_examples(a.source, a.examples)), indent=1))
    elif a.cmd == "demo":
        champ_v = registry()["passability"]["champion"]
        champ = Ensemble.load(MODELS / champ_v)
        parts = [demo_examples(s, 7, champ, shift=True) for s in a.storms]
        ex = pd.concat(parts, ignore_index=True)
        RELEARN.mkdir(parents=True, exist_ok=True)
        ex.to_parquet(RELEARN / "demo_examples.parquet", index=False)
        sim = pd.read_parquet(DATA / "train" / "passability.parquet", columns=FEATURES + ["split"])
        chk = check(ex, champ, sim[sim["split"] == "train"].sample(200_000, random_state=0))
        print("CHECK", json.dumps(chk, indent=1))
        dec = run(ex, a.dry_run, a.members, a.sim_frac)
        print("DECISION", json.dumps({k: dec[k] for k in ("outcome", "gate_results", "real_used") if k in dec}, indent=1))
        for k in ("champion", "challenger"):
            if "results" in dec:
                print(k, "recent_real", dec["results"][k]["recent_real"])


if __name__ == "__main__":
    main()
