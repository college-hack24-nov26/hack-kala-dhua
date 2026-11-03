"""Hazard runs and ward risk. Public — this is the warning itself."""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.core.errors import NotFound
from app.db.repositories import queries as q
from app.hazards import registry
from app.schemas.domain import HazardRun, HazardType, WardRisk

router = APIRouter(tags=["risk"])


@router.get("/hazards")
async def list_hazards() -> list[dict]:
    """Every registered adapter, with its maturity stated plainly.

    A registered-but-untrained adapter is reported as such rather than being
    presented alongside the trained ones.
    """
    return [
        {
            "hazard": a.hazard.value,
            "displayName": a.display_name,
            "maturity": a.maturity.value,
            "sources": list(a.sources),
        }
        for a in registry.all_adapters()
    ]


@router.get("/runs/latest", response_model=HazardRun | None)
async def latest_run(
    hazard: HazardType = Query(default=HazardType.FLOOD),
) -> HazardRun | None:
    run = await q.latest_run(hazard)
    if run is None:
        return None
    return HazardRun(
        id=str(run["id"]),
        hazard=run["hazard"],
        started_at=run["started_at"],
        sources=list(run["sources"] or []),
        ward_risks=await q.ward_risks(run["id"]),
    )


@router.get("/risk", response_model=list[WardRisk])
async def ward_risk(
    hazard: HazardType = Query(default=HazardType.FLOOD),
) -> list[WardRisk]:
    run = await q.latest_run(hazard)
    if run is None:
        return []
    return await q.ward_risks(run["id"])


@router.get("/risk/{ward_id}", response_model=WardRisk)
async def ward_risk_one(
    ward_id: str, hazard: HazardType = Query(default=HazardType.FLOOD)
) -> WardRisk:
    run = await q.latest_run(hazard)
    if run is None:
        raise NotFound("No hazard run has been made for this hazard yet.")
    for risk in await q.ward_risks(run["id"]):
        if risk.ward_id == ward_id:
            return risk
    raise NotFound(f"Ward {ward_id} was not scored in the latest run.")
