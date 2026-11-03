"""Live road risk from the passability model (PCMC district)."""
from __future__ import annotations

from fastapi import APIRouter

from app.core.security import StaffPrincipal
from app.nav import live

router = APIRouter(prefix="/nav", tags=["nav"])


@router.get("/risk")
async def risk(_: StaffPrincipal, min_p: float = 0.3, refresh: bool = False) -> dict:
    """Roads likely blocked for an ambulance within 30 min: GeoJSON lines with p,
    the model's own spread (sd), why (model / binding closure / crew), and whether
    the router avoids it (p >= 0.6)."""
    r = await live.current_risk(force=refresh)
    if r is None:
        return {"available": False, "reason": "model or data not available; routing uses reported blocks only"}
    gj = live.as_geojson(r, min_p)
    return {"available": True, "model": r.model, "computedAt": r.at, "notes": r.notes,
            "scored": int(len(r.p)), "avoided": int((r.p >= live.AVOID_P).sum()),
            "atRisk": len(gj["features"]), "segments": gj}


@router.get("/arrival")
async def arrival(_: StaffPrincipal, profile: str = "ambulance", horizon: int = 30, min_p: float = 0.1,
                  segments: bool = True) -> dict:
    """Road access when the unit gets there: the live evidence scored for one unit
    class (ambulance 0.3 m, fire tender 0.6 m, resident on foot 0.2 m) and one
    horizon (30/60/90 min). Each road carries p, its 2-sd band (lo/hi), whether
    the current-status rule calls it blocked now, and whether the router avoids it."""
    if profile not in live.ARRIVAL_PROFILES or horizon not in live.ARRIVAL_HORIZONS:
        return {"available": False, "reason": f"profile one of {live.ARRIVAL_PROFILES}, horizon one of {live.ARRIVAL_HORIZONS}"}
    a = await live.arrival(profile, horizon)
    if a is None:
        return {"available": False, "reason": "model or data not available; routing uses reported blocks only"}
    gj = live.arrival_geojson(a, max(0.0, min(min_p, 1.0)))
    props = [f["properties"] for f in gj["features"]]
    return {
        "available": True, "model": a["model"], "computedAt": a["at"], "notes": a["notes"],
        "profile": profile, "horizon": horizon, "scored": int(len(a["p"])),
        "blockedNow": sum(1 for x in props if x["now"]),
        "predictedBlocked": sum(1 for x in props if x["avoid"]),
        "closingBeforeArrival": sum(1 for x in props if x["avoid"] and not x["now"]),
        "uncertain": sum(1 for x in props if x["hi"] >= live.AVOID_P > x["p"]),
        "avoidAt": live.AVOID_P,
        **({"segments": gj} if segments else {}),
    }


@router.get("/models")
async def models(_: StaffPrincipal) -> dict:
    """What the models are, how they did on the frozen test set against the
    baselines, what they changed on routes and in the scenario replays, and what
    they are doing live right now (predictions logged, outcomes collected)."""
    import json
    from pathlib import Path

    from app.db import session as db
    from app.nav.passability import MODELS

    res = MODELS.parent / "results"
    if not res.exists():                     # running from the repo, not the image
        res = Path(__file__).resolve().parents[3] / "serving" / "results"

    def load(name: str):
        f = res / name
        return json.loads(f.read_text()) if f.exists() else None

    reg = json.loads((MODELS / "registry.json").read_text()) if (MODELS / "registry.json").exists() else {}
    meta = {}
    for task in ("passability", "eta"):
        v = (reg.get(task) or {}).get("champion")
        d = MODELS / task / (v or "")
        for n in ("model.json", "meta.json"):
            if v and (d / n).exists():
                m = json.loads((d / n).read_text())
                meta[task] = {k: m[k] for k in m if k not in ("params", "monotone", "best_iterations")}
                break
    live_stats: dict = {}
    try:
        row = await db.fetchrow(
            "select (select count(*) from nav_predictions) predictions, "
            "(select count(*) from nav_predictions where made_at > now() - interval '24 hours') predictions_24h, "
            "(select count(*) from nav_outcomes) outcomes, "
            "(select count(*) from nav_outcomes where observed_at > now() - interval '24 hours') outcomes_24h, "
            "(select count(*) from nav_examples) examples")
        live_stats = dict(row) if row else {}
        reg_rows = await db.fetch("select version, task, status, promoted_at, metrics from model_registry "
                                  "order by trained_at desc limit 20")
        live_stats["registry"] = [{**dict(r), "promoted_at": r["promoted_at"].isoformat() if r["promoted_at"] else None,
                                   "metrics": r["metrics"] if isinstance(r["metrics"], dict) else json.loads(r["metrics"] or "{}")}
                                  for r in reg_rows]
    except Exception as exc:  # noqa: BLE001
        live_stats = {"error": str(exc)[:160]}
    r = live._cache
    risk = ({"model": r.model, "scored": int(len(r.p)), "avoided": int((r.p >= live.AVOID_P).sum()),
             "atRisk": int((r.p >= 0.3).sum()), "computedAt": r.at, "notes": r.notes} if r else None)
    return {"champions": {k: (reg.get(k) or {}).get("champion") for k in ("passability", "eta")},
            "meta": meta, "passability": load("passability_metrics.json"), "routes": load("routes_test.json"),
            "eta": load("eta_metrics.json"), "scenarios": load("scenarios.json"), "live": live_stats,
            "risk": risk}
