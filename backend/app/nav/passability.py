"""Serving the passability model: P(blocked at arrival) per segment, plus the rules
that sit above it.

    scorer = Scorer.load()                       # champion from models/registry.json
    p, sd, why = scorer.score(F, confirmed)      # F from ml.features.build

Rules (MISC-04: predictions support, never replace them):
  * a confirmed closure is binding: p = 1 until a valid update reopens it;
  * a crew/patrol/drone confirmation that a road is open in the last 30 min
    caps p at 0.05 for that road (fresh eyes beat the model);
  * everything the router uses is logged to nav_predictions with its inputs,
    which is what the re-learning loop learns from (ml.relearn).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

import os

REPO = Path(__file__).resolve().parents[3]
#: DISRUPTIONOPS_MODELS when deployed (backend/serving/models in the image).
MODELS = Path(os.environ.get("DISRUPTIONOPS_MODELS") or (REPO / "models"))


class LightEnsemble:
    """The trained ensemble without scikit-learn: XGBoost boosters averaged, then
    the isotonic calibration applied as the piecewise-linear map it is
    (np.interp over its thresholds, clipped) - identical to
    IsotonicRegression.predict with out_of_bounds='clip', and light enough for a
    small API container."""

    def __init__(self, d: Path):
        import xgboost as xgb
        meta = json.loads((d / "model.json").read_text())
        self.features = meta["features"]
        self.members = []
        for f in sorted(d.glob("member_*.json")):
            b = xgb.Booster()
            b.load_model(str(f))
            self.members.append(b)
        c = json.loads((d / "calibration.json").read_text())
        self.cx, self.cy = np.asarray(c["x"], float), np.asarray(c["y"], float)

    def predict(self, X: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        import xgboost as xgb
        dm = xgb.DMatrix(X[self.features].astype(np.float32))
        ps = np.stack([m.predict(dm, iteration_range=(0, m.best_iteration + 1)) if m.best_iteration is not None
                       else m.predict(dm) for m in self.members])
        mean = ps.mean(0)
        return np.interp(mean, self.cx, self.cy), ps.std(0)


@dataclass
class Confirmation:
    seg_index: int
    blocked: bool
    age_min: float


class Scorer:
    def __init__(self, ensemble, version: str, threshold: float):
        self.ens, self.version, self.threshold = ensemble, version, threshold

    @classmethod
    def load(cls, version: str | None = None) -> "Scorer":
        reg = json.loads((MODELS / "registry.json").read_text()) if (MODELS / "registry.json").exists() else {}
        version = version or (reg.get("passability") or {}).get("champion")
        if not version:
            raise RuntimeError("no passability champion registered (python -m ml.relearn register ...)")
        d = MODELS / "passability" / version
        meta = json.loads((d / "model.json").read_text())
        return cls(LightEnsemble(d), version, float(meta["threshold"]))

    def score(self, F: pd.DataFrame, confirmations: list[Confirmation] | None = None,
              cand: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray, list[str]]:
        p, sd = self.ens.predict(F)
        why = ["model"] * len(p)
        closed = F["closure_active"].to_numpy() > 0
        p = np.where(closed, 1.0, p)
        for i in np.flatnonzero(closed):
            why[i] = "confirmed closure (binding)"
        if confirmations and cand is not None:
            pos = {int(s): k for k, s in enumerate(cand)}
            for c in confirmations:
                k = pos.get(c.seg_index)
                if k is None:
                    continue
                if c.blocked:
                    p[k], why[k] = 1.0, "crew confirmed blocked"
                elif c.age_min <= 30 and not closed[k]:
                    p[k], why[k] = min(p[k], 0.05), "crew confirmed open"
        return p, sd, why


_registered: set[str] = set()


async def ensure_registered(conn, version: str, task: str = "passability") -> None:
    """nav_predictions.model_version references model_registry. A fresh database
    (or one where ml.relearn ran without DATABASE_URL) has no row for the model
    the server ships with, and every prediction log then fails its foreign key.
    The server registers what it serves - as champion, with the metrics saved
    beside the model - once per process."""
    if version in _registered:
        return
    meta, d = {}, MODELS / task / version
    for name in ("model.json", "meta.json"):
        if (d / name).exists():
            try:
                meta = json.loads((d / name).read_text())
                break
            except ValueError:
                pass
    metrics = meta.get("test") or meta.get("metrics") or {}
    window = {k: meta[k] for k in ("trained_on", "train_rows", "tune_rows") if k in meta}
    # one champion per task (unique index): if another is already champion the
    # served model goes in as a candidate, and ml.relearn decides promotion
    await conn.execute(
        "insert into model_registry (version, task, status, data_window, metrics, artifact_uri, promoted_at) "
        "select $1, $2, case when exists (select 1 from model_registry where task = $2 and status = 'champion') "
        "then 'candidate' else 'champion' end, $3::jsonb, $4::jsonb, $5, now() "
        "on conflict (version) do nothing",
        version, task, json.dumps(window, default=str), json.dumps(metrics, default=str),
        f"serving/models/{task}/{version}")
    _registered.add(version)


async def log_predictions(conn, rows: list[dict]) -> int:
    """Insert what the router used into nav_predictions (migration 031).
    rows: seg_id, lon, lat, profile, horizon_min, p, sd, features (dict), model_version,
    optional assignment_id, used_for ('route' | 'scan' | 'shadow'), city_id, sim_run_id."""
    if not rows:
        return 0
    for v in {r["model_version"] for r in rows}:
        await ensure_registered(conn, v)
    await conn.executemany(
        "insert into nav_predictions (city_id, sim_run_id, model_version, seg_id, location, profile, horizon_min, "
        "p_blocked, p_sd, features, assignment_id, used_for) values ($1,$2,$3,$4, "
        "extensions.ST_SetSRID(extensions.ST_MakePoint($5,$6),4326)::extensions.geography, $7,$8,$9,$10,$11::jsonb,$12,$13)",
        [(r.get("city_id", "pune"), r.get("sim_run_id"), r["model_version"], r["seg_id"], r["lon"], r["lat"],
          r["profile"], int(r["horizon_min"]), float(r["p"]), r.get("sd"), json.dumps(r.get("features") or {}),
          r.get("assignment_id"), r.get("used_for", "route")) for r in rows])
    return len(rows)


async def log_driven(conn, segs: list[dict], assignment_id: str | None = None) -> int:
    """Roads a unit actually drove are outcomes too (open at that moment).
    segs: seg_id, lon, lat, observed_at (datetime), profile."""
    if not segs:
        return 0
    await conn.executemany(
        "insert into nav_outcomes (observed_at, location, radius_m, seg_id, profile, blocked, source, confidence, "
        "ref_table, ref_id) values ($1, extensions.ST_SetSRID(extensions.ST_MakePoint($2,$3),4326)::extensions.geography, "
        "25, $4, $5, false, 'trip', 0.95, 'assignments', $6)",
        [(s["observed_at"], s["lon"], s["lat"], s["seg_id"], s.get("profile"), assignment_id) for s in segs])
    return len(segs)
