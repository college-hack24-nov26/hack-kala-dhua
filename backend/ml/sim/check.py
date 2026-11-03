"""Sanity checks of the simulator against what is on record.

    python -m ml.sim.check

This is a plausibility check, not a validation: there is no public, road-level
record of which PCMC roads were under water when. What there is:

1. The 25 July 2024 situation report (Sphere India, from district/PCMC inputs)
   names waterlogging at the Vallabhnagar underpass in Pimpri and a traffic
   detour at Pimple Nilakh (Mula riverside). We check whether, in the 2024-07-24
   window, the simulator puts water on those roads on the 25th, in how many of
   its realizations.
2. Storms the press reported as the worst of the decade (Sept 2016, Sept 2017,
   Aug 2019, Aug 2020, Jul 2026 per ERA5/GloFAS) should rank at the top by
   simulated flooding, and the moderate days at the bottom.

Writes data/sim/check.json.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from ml.district import SIM
from ml.sim.static import load_static
from ml.sim.world import Truth

VALLABHNAGAR = (73.8163, 18.6137)       # Yeshwantrao Chavan Rd underpass by the Vallabhnagar ST stand
PIMPLE_NILAKH = (73.788, 18.588)        # Mula riverside roads at the district's southern edge


def _near(st: pd.DataFrame, ll: tuple[float, float], r_m: float, mask: np.ndarray | None = None) -> np.ndarray:
    d = np.hypot((st["lon"] - ll[0]) * 105_500, (st["lat"] - ll[1]) * 110_900)
    m = (d < r_m).to_numpy()
    return np.flatnonzero(m if mask is None else m & mask)


def main() -> dict:
    st = load_static()
    summ = pd.read_parquet(SIM / "runs" / "summary.parquet")
    sid = "20240724-08"
    reals = sorted(summ.loc[summ["storm_id"] == sid, "realization"])
    vb = _near(st, VALLABHNAGAR, 150, (st["underpass"] & st["drivable"]).to_numpy())
    pn = _near(st, PIMPLE_NILAKH, 800, ((st["river"] == "Mula") & st["drivable"]).to_numpy())
    out = {"vallabhnagar_segments": len(vb), "pimple_nilakh_segments": len(pn), "realizations": []}
    for k in reals:
        tr = Truth.load(sid, k)
        times = pd.date_range(tr.start, periods=tr.steps, freq="30min")
        day = np.flatnonzero(times.normalize() == pd.Timestamp("2024-07-25"))
        d = tr.depth[day].astype(np.float32)
        out["realizations"].append({
            "r": k,
            "vallabhnagar_max_depth_m": round(float(d[:, vb].max()), 2) if len(vb) else None,
            "vallabhnagar_hours_ge_30cm": float((d[:, vb].max(axis=1) >= 0.3).sum() / 2) if len(vb) else None,
            "pimple_nilakh_segments_ge_30cm": int((d[:, pn].max(axis=0) >= 0.3).sum()) if len(pn) else None,
        })
    rv = out["realizations"]
    out["vallabhnagar_flooded_in"] = f"{sum(1 for r in rv if (r['vallabhnagar_max_depth_m'] or 0) >= 0.3)}/{len(rv)}"
    out["pimple_nilakh_flooded_in"] = f"{sum(1 for r in rv if (r['pimple_nilakh_segments_ge_30cm'] or 0) > 0)}/{len(rv)}"

    rank = (summ.groupby(["storm_id", "tier"])["segments_depth_ge_0_3"].median()
            .reset_index().sort_values("segments_depth_ge_0_3", ascending=False))
    rank["rank"] = np.arange(1, len(rank) + 1)
    out["top_storms"] = rank.head(8).to_dict("records")
    out["moderate_median_rank"] = float(rank.loc[rank["tier"] == "moderate", "rank"].median())
    out["major_median_rank"] = float(rank.loc[rank["tier"] == "major", "rank"].median())
    out["storms"] = int(len(rank))
    (SIM / "check.json").write_text(json.dumps(out, indent=1, default=str))
    return out


if __name__ == "__main__":
    print(json.dumps(main(), indent=1, default=str))
