"""Simulate every storm in the catalogue, several times, and what was seen of it.

    python -m ml.sim.run                       # all storms, 3 realizations each
    python -m ml.sim.run --storms 20240724-08 --realizations 5

Each (storm, realization) is a different but equally plausible version of the
same real storm: a different rain field, different blocked drains and failed
pumps, different trees falling, different people reporting. Same seed, same
run. Writes data/sim/runs/<storm>/r<k>/ (truth, events, air, obs) and
data/sim/runs/summary.parquet.
"""
from __future__ import annotations

import argparse
import time
from concurrent.futures import ProcessPoolExecutor

import pandas as pd

from ml.district import SIM


def one(args: tuple[str, int]) -> dict:
    from ml.sim.observe import observe
    from ml.sim.world import simulate

    sid, k = args
    t0 = time.time()
    tr = simulate(sid, k)
    d = tr.save()
    ob = observe(tr)
    ob.save(d)
    return {"storm_id": sid, "realization": k, "split": tr.meta["split"], "tier": tr.meta["tier"],
            **tr.meta["peak"], **{f"ev_{a}": b for a, b in tr.meta["events"].items()},
            **{f"obs_{a}": b for a, b in ob.meta["counts"].items()},
            "obs_false": ob.meta["false_reports"], "obs_stale": ob.meta["stale_reports"],
            "seconds": round(time.time() - t0, 1)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--storms", nargs="*")
    ap.add_argument("--realizations", type=int, default=3)
    ap.add_argument("--workers", type=int, default=2)
    a = ap.parse_args()
    cat = pd.read_parquet(SIM / "storms.parquet")
    ids = a.storms or list(cat["storm_id"])
    jobs = [(s, k) for s in ids for k in range(a.realizations)]
    with ProcessPoolExecutor(a.workers) as ex:
        rows = list(ex.map(one, jobs))
    df = pd.DataFrame(rows).fillna(0)
    out = SIM / "runs" / "summary.parquet"
    if a.storms and out.exists():
        old = pd.read_parquet(out)
        old = old[~old.set_index(["storm_id", "realization"]).index.isin(df.set_index(["storm_id", "realization"]).index)]
        df = pd.concat([old, df], ignore_index=True).fillna(0)
    df.sort_values(["storm_id", "realization"]).to_parquet(out, index=False)
    print(df.groupby(["split", "tier"]).agg(runs=("storm_id", "size"),
                                             seg_ge_30cm=("segments_depth_ge_0_3", "median"),
                                             reports=("obs_citizen", "median")).to_string())


if __name__ == "__main__":
    main()
