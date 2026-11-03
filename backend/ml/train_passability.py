"""Train and evaluate the road-accessibility-at-arrival model (MISC-04 Addition 2).

    python -m ml.train_passability                  # after ml.samples
    python -m ml.train_passability --members 5

Question answered: "if this unit leaves now, will this segment be blocked for
it when it gets there in 30 / 60 / 90 minutes?"

* Model: 5 XGBoost classifiers (different seeds and row subsamples), averaged;
  their spread is the model's own uncertainty. Monotone constraints where the
  physics is one-directional (more reported flooding, higher river, more rain
  forecast never make a road *safer*; more height above the river never makes
  it *worse*).
* Fit on train storms (2015-2021), early-stopped on tune storms (2022), then
  calibrated on tune (isotonic, weighted back to the natural mix of roads).
  Test storms (2023-2026) are touched once, at the end.
* Baselines on the same test rows:
    B0 current-status-only: blocked iff the evidence at decision time says so
       (official closure, patrol, sensor, hazard report, repeated flood reports).
    B1 logistic regression on the same features.
* Writes models/passability/<version>/ and data/eval/passability_*.{json,parquet}.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, log_loss, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler

from ml.district import PROCESSED
from ml.features import FEATURES

DATA = PROCESSED.parent
MODELS = DATA.parent / "models" / "passability"
EVAL = DATA / "eval"

MONOTONE = {
    "flood_150_1h": 1, "flood_150_3h": 1, "flood_400_3h": 1, "flood_maxcls_150_3h": 1,
    "rain_1h": 1, "rain_3h": 1, "rain_fc": 1, "stage_now": 1, "stage_frac": 1,
    "freeboard_m": -1, "hand_m": -1, "closure_active": 1, "sensor_val": 1, "haz_100_6h": 1,
    "status_now": 1, "pond_m": 1, "underpass": 1,
}
PARAMS = dict(objective="binary:logistic", eval_metric="logloss", tree_method="hist", max_depth=7,
              learning_rate=0.06, min_child_weight=5, subsample=0.8, colsample_bytree=0.8,
              reg_lambda=2.0, max_bin=256, nthread=2)


def ece(y: np.ndarray, p: np.ndarray, w: np.ndarray | None = None, bins: int = 15) -> float:
    w = np.ones_like(p) if w is None else w
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges) - 1, 0, bins - 1)
    tot = w.sum()
    e = 0.0
    for b in range(bins):
        m = idx == b
        if m.any():
            e += w[m].sum() / tot * abs(np.average(y[m], weights=w[m]) - np.average(p[m], weights=w[m]))
    return float(e)


def metrics(y: np.ndarray, p: np.ndarray, w: np.ndarray, thr: float) -> dict:
    pred = p >= thr
    tp = float((w * (pred & (y == 1))).sum()); fp = float((w * (pred & (y == 0))).sum())
    fn = float((w * (~pred & (y == 1))).sum()); tn = float((w * (~pred & (y == 0))).sum())
    out = {
        "n": int(len(y)), "positives": int(y.sum()),
        "brier_natural": float(brier_score_loss(y, p, sample_weight=w)),
        "ece_natural": ece(y, p, w),
        "precision": tp / max(tp + fp, 1e-9), "recall": tp / max(tp + fn, 1e-9),
        "missed_closure_rate": fn / max(tp + fn, 1e-9),          # predicted open, was blocked
        "false_closure_rate": fp / max(fp + tn, 1e-9),           # predicted blocked, was open
    }
    if len(np.unique(y)) > 1 and len(np.unique(p)) > 1:
        out["roc_auc"] = float(roc_auc_score(y, p))
        out["pr_auc"] = float(average_precision_score(y, p))
        out["log_loss"] = float(log_loss(y, np.clip(p, 1e-6, 1 - 1e-6)))
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in out.items()}


def best_threshold(y: np.ndarray, p: np.ndarray, w: np.ndarray, min_recall: float = 0.9) -> float:
    """Highest threshold that still catches `min_recall` of real closures (on tune).
    A missed closure sends a crew into water; a false one costs a detour."""
    order = np.argsort(-p)
    yw = (y[order] == 1) * w[order]
    rec = np.cumsum(yw) / max(yw.sum(), 1e-9)
    k = int(np.searchsorted(rec, min_recall))
    return float(p[order][min(k, len(p) - 1)])


class Ensemble:
    def __init__(self, members: list[xgb.Booster], iso: IsotonicRegression, features: list[str]):
        self.members, self.iso, self.features = members, iso, features

    def raw(self, X: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        d = xgb.DMatrix(X[self.features].astype(np.float32))
        ps = np.stack([m.predict(d, iteration_range=(0, m.best_iteration + 1)) for m in self.members])
        return ps.mean(0), ps.std(0)

    def predict(self, X: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        mean, sd = self.raw(X)
        return self.iso.predict(mean), sd

    def save(self, d: Path, meta: dict) -> None:
        d.mkdir(parents=True, exist_ok=True)
        for i, m in enumerate(self.members):
            m.save_model(d / f"member_{i}.json")
        json.dump({"x": self.iso.X_thresholds_.tolist(), "y": self.iso.y_thresholds_.tolist()},
                  open(d / "calibration.json", "w"))
        json.dump({"features": self.features, **meta}, open(d / "model.json", "w"), indent=1)

    @classmethod
    def load(cls, d: Path) -> "Ensemble":
        meta = json.load(open(d / "model.json"))
        members = []
        for f in sorted(d.glob("member_*.json")):
            b = xgb.Booster()
            b.load_model(f)
            members.append(b)
        c = json.load(open(d / "calibration.json"))
        iso = IsotonicRegression(out_of_bounds="clip")
        iso.fit(c["x"], c["y"])
        return cls(members, iso, meta["features"])


def train(df: pd.DataFrame, n_members: int = 5, seed: int = 11) -> tuple[Ensemble, dict]:
    tr, tu = df[df["split"] == "train"], df[df["split"] == "tune"]
    mono = tuple(MONOTONE.get(f, 0) for f in FEATURES)
    # optional per-row training weight (the relearn loop up-weights real outcomes)
    wt = tr["train_w"].to_numpy() if "train_w" in tr else None
    dtr = xgb.DMatrix(tr[FEATURES].astype(np.float32), label=tr["y"], weight=wt)
    dtu = xgb.DMatrix(tu[FEATURES].astype(np.float32), label=tu["y"])
    members = []
    for i in range(n_members):
        p = {**PARAMS, "seed": seed + i, "monotone_constraints": str(mono)}
        b = xgb.train(p, dtr, num_boost_round=1500, evals=[(dtu, "tune")],
                      early_stopping_rounds=60, verbose_eval=False)
        members.append(b)
    ens = Ensemble(members, IsotonicRegression(out_of_bounds="clip"), FEATURES)
    raw_tu, _ = ens.raw(tu)
    ens.iso.fit(raw_tu, tu["y"], sample_weight=tu["w"])
    info = {"best_iterations": [int(m.best_iteration) for m in members], "train_rows": int(len(tr)),
            "tune_rows": int(len(tu))}
    return ens, info


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--members", type=int, default=5)
    ap.add_argument("--eval-only", default=None, help="re-evaluate a saved model version, no training")
    a = ap.parse_args()
    t0 = time.time()
    df = pd.read_parquet(DATA / "train" / "passability.parquet")
    if a.eval_only:
        ens = Ensemble.load(MODELS / a.eval_only)
        info = {k: v for k, v in json.load(open(MODELS / a.eval_only / "model.json")).items()
                if k in ("best_iterations", "train_rows", "tune_rows")}
    else:
        ens, info = train(df, a.members)
    tu, te = df[df["split"] == "tune"], df[df["split"] == "test"]

    # operating threshold, chosen on tune only
    p_tu, _ = ens.predict(tu)
    thr = best_threshold(tu["y"].to_numpy(), p_tu, tu["w"].to_numpy(), 0.9)

    # B1: logistic regression on the same features
    trn = df[df["split"] == "train"]
    lr = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), LogisticRegression(max_iter=400, C=0.5))
    lr.fit(trn[FEATURES].astype(np.float32), trn["y"])
    p_lr_tu = lr.predict_proba(tu[FEATURES].astype(np.float32))[:, 1]
    thr_lr = best_threshold(tu["y"].to_numpy(), p_lr_tu, tu["w"].to_numpy(), 0.9)

    # final, once, on test
    p, sd = ens.predict(te)
    p_lr = lr.predict_proba(te[FEATURES].astype(np.float32))[:, 1]
    y, w = te["y"].to_numpy(), te["w"].to_numpy()
    b0 = te["status_now"].to_numpy().astype(float)
    res = {"model": metrics(y, p, w, thr), "b1_logistic": metrics(y, p_lr, w, thr_lr),
           "b0_current_status": metrics(y, b0, w, 0.5)}

    # Where prediction matters: the road's state changes between now and arrival.
    # Among roads open now: can it tell the ones that will close (0->1) from the
    # ones that stay open? Among roads blocked now: the ones that will reopen?
    yn = te["y_now"].to_numpy()
    op, bl = yn == 0, yn == 1
    res["anticipation"] = {
        "open_now_rows": int(op.sum()), "will_close": int((op & (y == 1)).sum()),
        "will_close_model": metrics(y[op], p[op], w[op], thr),
        "will_close_b0": metrics(y[op], b0[op], w[op], 0.5),
        "blocked_now_rows": int(bl.sum()), "will_reopen": int((bl & (y == 0)).sum()),
        "will_reopen_model_auc": round(float(roc_auc_score(1 - y[bl], 1 - p[bl])), 4) if len(np.unique(y[bl])) > 1 else None,
        "mean_p": {"stay_open": round(float(p[op & (y == 0)].mean()), 4), "will_close": round(float(p[op & (y == 1)].mean()), 4),
                   "will_reopen": round(float(p[bl & (y == 0)].mean()), 4), "stay_blocked": round(float(p[bl & (y == 1)].mean()), 4)},
    }
    by = []
    for (pr, hz), g in te.assign(p=p, b0=b0).groupby(["profile", "horizon_min"]):
        by.append({"profile": int(pr), "horizon_min": int(hz),
                   **{f"model_{k}": v for k, v in metrics(g["y"].to_numpy(), g["p"].to_numpy(), g["w"].to_numpy(), thr).items()
                      if k in ("recall", "precision", "missed_closure_rate", "roc_auc")},
                   **{f"b0_{k}": v for k, v in metrics(g["y"].to_numpy(), g["b0"].to_numpy(), g["w"].to_numpy(), 0.5).items()
                      if k in ("recall", "precision", "missed_closure_rate")}})
    res["by_profile_horizon"] = by
    # uncertainty is informative: are the members' disagreements where the errors are?
    err = np.abs(y - p)
    res["uncertainty_error_corr"] = round(float(np.corrcoef(sd, err)[0, 1]), 3)

    booster = ens.members[0]
    gain = booster.get_score(importance_type="gain")
    top = sorted(gain.items(), key=lambda kv: -kv[1])[:15]

    version = a.eval_only or time.strftime("pass-%Y%m%d-%H%M")
    meta = {"version": version, "threshold": thr, "threshold_rule": "recall >= 0.9 on tune (weighted)",
            "trained_on": "simulated storms 2015-2021 (ml.sim), tuned/calibrated on 2022",
            "params": PARAMS, "monotone": MONOTONE, **info, "test": res["model"],
            "labels": "blocked for the unit at t0+horizon, by app.nav.hazards rules (ml.labels)",
            "profiles": {"0": "ambulance (0.3 m)", "1": "fire_engine (0.6 m)", "2": "resident on foot (0.2 m)"}}
    ens.save(MODELS / version, meta)
    EVAL.mkdir(parents=True, exist_ok=True)
    out = {"version": version, "threshold": thr, "threshold_b1": thr_lr, "seconds": round(time.time() - t0),
           "test_rows": int(len(te)), "test_positive_share": round(float(y.mean()), 4),
           "results": res, "top_features_gain": [[k, round(v, 1)] for k, v in top]}
    json.dump(out, open(EVAL / "passability_metrics.json", "w"), indent=1)
    te[["storm_id", "realization", "t0_min", "seg", "profile", "horizon_min", "stratum", "w", "y", "status_now"]].assign(
        p=p.round(4), p_sd=sd.round(4), p_logistic=p_lr.round(4), model=version).to_parquet(
        EVAL / "passability_test_predictions.parquet", index=False)
    print(json.dumps({k: out[k] for k in ("version", "threshold", "test_rows", "test_positive_share", "seconds")}))
    for k in ("model", "b1_logistic", "b0_current_status"):
        print(k, res[k])
    print("anticipation", res["anticipation"])
    print("top features", top[:10])


if __name__ == "__main__":
    main()
