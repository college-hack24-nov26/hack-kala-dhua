"""Copy what the API needs to score roads live into backend/serving (the Docker
build context is backend/, so models/ and data/ at the repo root never reach the
image).

    python -m ml.export_serving          # after training + `ml.relearn register ... --champion`

backend/serving/
    models/registry.json                 champions only
    models/passability/<champion>/       boosters + calibration + model.json
    models/eta/<champion>/               (if registered)
    data/processed/graph/static.parquet  per-segment features
    data/processed/graph/segments.parquet  geometry for the risk map layer
    data/sim/river_stats.json            rating curve inputs for live river stage
    results/*.json                       test metrics, route evaluation, scenarios (Models page)

About 25 MB. The image sets DISRUPTIONOPS_DATA and DISRUPTIONOPS_MODELS to it.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from ml.district import PROCESSED, REPO, SIM

OUT = Path(__file__).resolve().parents[1] / "serving"


def main() -> None:
    reg = json.loads((REPO / "models" / "registry.json").read_text())
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "models").mkdir(parents=True)
    slim = {}
    for task in ("passability", "eta"):
        champ = (reg.get(task) or {}).get("champion")
        slim[task] = {"champion": champ, "history": []}
        if champ:
            shutil.copytree(REPO / "models" / task / champ, OUT / "models" / task / champ)
    (OUT / "models" / "registry.json").write_text(json.dumps(slim, indent=1))
    g = OUT / "data" / "processed" / "graph"
    g.mkdir(parents=True)
    for f in ("static.parquet", "segments.parquet"):
        shutil.copy2(PROCESSED / "graph" / f, g / f)
    (OUT / "data" / "sim").mkdir(parents=True)
    shutil.copy2(SIM / "river_stats.json", OUT / "data" / "sim" / "river_stats.json")
    # Results the website's Models page shows (frozen test metrics, route
    # evaluation, ETA metrics, scenario outcomes).
    r = OUT / "results"
    r.mkdir(parents=True)
    ev = PROCESSED.parent / "eval"
    for f in ("passability_metrics.json", "routes_test.json", "eta_metrics.json"):
        if (ev / f).exists():
            shutil.copy2(ev / f, r / f)
    sc = {}
    for f in sorted((PROCESSED.parent / "scenarios").glob("*/summary.json")):
        s = json.loads(f.read_text())
        sc[s["scenario"]] = s
    (r / "scenarios.json").write_text(json.dumps(sc, indent=1))
    size = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    print(f"serving bundle: {OUT} ({size / 1e6:.1f} MB) champions {slim['passability']['champion']}, {slim['eta']['champion']}")


if __name__ == "__main__":
    main()
