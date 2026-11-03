"""Response ETA: how long until the unit is actually there, with a P50 and a P90.

    python -m ml.train_eta          # after ml.route_eval for train/tune and test

The idea of food-delivery ETA models (Zomato/Swiggy-style: gradient-boosted
trees on route, traffic, weather and time-of-day, explained with SHAP), tuned
for emergency response:

* the target is the whole response: mobilisation (call -> rolling) + driving,
  including turn-backs and replans on roads that turned out blocked, and the
  last stretch on foot when the road is gone (ml.route_eval drives every trip
  through the simulated truth);
* quantiles, not a single number: P50 for planning, P90 for promises. A
  dispatcher is told "18 min, 90% within 27", and the planner uses P90 when a
  late arrival is dangerous;
* flood risk on the route is a feature (sum and max of the passability model's
  P(blocked) along the planned route), which is how Addition 2 feeds the ETA.

Trained on trips in train storms (2015-2021), early-stopped on tune (2022),
reported once on test (2023-2026). Baselines on the same test trips:
    distance rule   crow-fly km x 1.35 at 18 km/h + median mobilisation
    traffic ETA     free-flow route time with the live traffic feed (what a
                    Mapbox-style ETA knows: traffic yes, floods no) + mobilisation
A conformal correction on tune makes the P90 cover 90% on tune; the relearn loop
re-fits it from real arrivals.
"""
from __future__ import annotations

import json
import time

import numpy as np
import pandas as pd
import xgboost as xgb

from ml.district import PROCESSED

DATA = PROCESSED.parent
EVAL = DATA / "eval"
MODELS = DATA.parent / "models" / "eta"
FEATURES = ["plan_km", "plan_free_min", "plan_signals", "plan_underpasses", "plan_bridges", "plan_p_sum",
            "plan_p_max", "plan_traffic", "plan_nodata", "plan_flood_reports", "rain_1h", "rain_fc",
            "stage_frac_max", "hour", "unit", "crow_km"]
PARAMS = dict(objective="reg:quantileerror", quantile_alpha=np.array([0.5, 0.9]), tree_method="hist",
              max_depth=5, learning_rate=0.05, min_child_weight=5, subsample=0.85, colsample_bytree=0.9, nthread=2)


def load() -> pd.DataFrame:
    parts = [pd.read_parquet(p) for p in sorted(EVAL.glob("routes_*.parquet"))]
    df = pd.concat(parts, ignore_index=True).drop_duplicates(["storm_id", "realization", "t0_min", "profile", "src", "dst"])
    df["unit"] = (df["profile"] == "fire_engine").astype(int)
    df["actual_min"] = df["mobilise_min"] + df["predicted_travel_s"] / 60
    return df[np.isfinite(df["actual_min"]) & df["plan_km"].notna()].reset_index(drop=True)


def pinball(y, q, a):
    d = y - q
    return float(np.mean(np.maximum(a * d, (a - 1) * d)))


def report(y, p50, p90=None) -> dict:
    out = {"mae_min": round(float(np.mean(np.abs(y - p50))), 2),
           "within_5_min": round(float(np.mean(np.abs(y - p50) <= 5)), 3),
           "late_over_10_min": round(float(np.mean(y - p50 > 10)), 3)}
    if p90 is not None:
        out["p90_coverage"] = round(float(np.mean(y <= p90)), 3)
        out["pinball_p90"] = round(pinball(y, p90, 0.9), 2)
        out["late_vs_p90_over_10_min"] = round(float(np.mean(y - p90 > 10)), 3)
    return out


def main() -> None:
    t0 = time.time()
    df = load()
    tr, tu, te = (df[df["split"] == s] for s in ("train", "tune", "test"))
    dtr = xgb.DMatrix(tr[FEATURES], label=tr["actual_min"])
    dtu = xgb.DMatrix(tu[FEATURES], label=tu["actual_min"])
    b = xgb.train(PARAMS, dtr, num_boost_round=2000, evals=[(dtu, "tune")], early_stopping_rounds=80,
                  verbose_eval=False)
    it = (0, b.best_iteration + 1)

    def q(d):
        p = b.predict(xgb.DMatrix(d[FEATURES]), iteration_range=it)
        return p[:, 0], np.maximum(p[:, 1], p[:, 0])

    # conformal correction of P90 on tune
    _, q90_tu = q(tu)
    resid = tu["actual_min"].to_numpy() - q90_tu
    n = len(resid)
    off = float(np.quantile(resid, min(1.0, np.ceil((n + 1) * 0.9) / n))) if n else 0.0
    off = max(off, 0.0)

    p50, p90 = q(te)
    p90 = p90 + off
    y = te["actual_min"].to_numpy()
    mob_med = float(tr["mobilise_min"].median())
    dist_rule = te["crow_km"].to_numpy() * 1.35 / 18 * 60 + mob_med
    traffic_eta = te["plan_free_min"].to_numpy() + mob_med
    res = {"model": report(y, p50, p90), "distance_rule": report(y, dist_rule), "traffic_eta": report(y, traffic_eta)}
    hot = te["dst_hot"].to_numpy()
    res["to_flooded_places"] = {"model": report(y[hot], p50[hot], p90[hot]), "traffic_eta": report(y[hot], traffic_eta[hot])}

    contrib = b.predict(xgb.DMatrix(te[FEATURES]), pred_contribs=True, iteration_range=it)
    c50 = contrib[:, 0, :-1] if contrib.ndim == 3 else contrib[:, :-1]
    shap = sorted(zip(FEATURES, np.abs(c50).mean(0)), key=lambda kv: -kv[1])

    version = time.strftime("eta-%Y%m%d-%H%M")
    d = MODELS / version
    d.mkdir(parents=True, exist_ok=True)
    b.save_model(d / "model.json")
    meta = {"version": version, "features": FEATURES, "best_iteration": int(b.best_iteration),
            "p90_offset_min": round(off, 3), "mobilise_median_min": mob_med,
            "trips": {"train": int(len(tr)), "tune": int(len(tu)), "test": int(len(te))},
            "target": "mobilisation + travel (with replans, turn-backs, on-foot last stretch) in minutes",
            "test": res["model"]}
    json.dump(meta, open(d / "meta.json", "w"), indent=1)
    out = {"version": version, "seconds": round(time.time() - t0), **meta["trips"], "results": res,
           "shap_mean_abs_p50": [[k, round(float(v), 3)] for k, v in shap]}
    EVAL.mkdir(parents=True, exist_ok=True)
    json.dump(out, open(EVAL / "eta_metrics.json", "w"), indent=1)
    te.assign(eta_p50=p50.round(2), eta_p90=p90.round(2), dist_rule=dist_rule.round(2),
              traffic_eta=traffic_eta.round(2), model=version)[
        ["storm_id", "realization", "t0_min", "profile", "src", "dst", "dst_hot", "actual_min", "eta_p50", "eta_p90",
         "dist_rule", "traffic_eta", "model"]].to_parquet(EVAL / "eta_test_predictions.parquet", index=False)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
