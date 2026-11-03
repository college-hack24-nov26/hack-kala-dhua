"""Units: live position reports, per-unit logs (viewable and exportable), tracks, and incident teams."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse
from pydantic import Field

from app.core.errors import NotFound
from app.core.security import CurrentPrincipal, StaffPrincipal
from app.schemas.domain import Camel
from app.units import service

router = APIRouter(prefix="/units", tags=["units"])


class PositionIn(Camel):
    lng: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)
    speed_kmh: float | None = Field(default=None, ge=0, le=300)
    heading_deg: float | None = Field(default=None, ge=0, le=360)
    accuracy_m: float | None = Field(default=None, ge=0, le=5000)
    at: datetime | None = None
    source: str = "gps"


@router.post("/{unit_id}/position")
async def post_position(unit_id: str, body: PositionIn, principal: CurrentPrincipal) -> dict:
    """The field app's GPS ping (every ~15 s while on a job). Moves the unit, measures it
    against its road, reroutes it from where it is when it leaves that road, and asks
    for a replan when a free unit has moved far."""
    out = await service.report_position(
        unit_id, body.lng, body.lat, speed_kmh=body.speed_kmh, heading_deg=body.heading_deg,
        accuracy_m=body.accuracy_m, at=body.at, source=body.source if body.source in ("gps", "sim", "manual") else "gps",
        reporter=principal.full_name or principal.operator or "field")
    if not out.get("ok"):
        raise NotFound(out.get("error", "unknown unit"))
    return out


@router.get("")
async def list_units(_: StaffPrincipal, city_id: str = "pune", prefix: str | None = None) -> list[dict]:
    return await service.units_overview(city_id, prefix)


@router.get("/trails")
async def get_trails(_: StaffPrincipal, minutes: int = 30, region: str = "pune") -> dict:
    """All units' trails and reroute points for the wall map (GeoJSON)."""
    return await service.trails(max(5, min(minutes, 240)), "ncr" if region == "ncr" else "pune")


@router.get("/sites")
async def list_sites(_: StaffPrincipal, region: str = "pune") -> list[dict]:
    """Shelters, relief centres, kitchens, water points, camps, hospitals: state now
    and how many changes are logged."""
    return await service.sites("ncr" if region == "ncr" else "pune")


@router.get("/sites/log/export")
async def export_sites(_: StaffPrincipal, since: datetime | None = None, until: datetime | None = None,
                       region: str = "pune", format: str = Query(default="csv", pattern="^(json|csv)$")):
    """Every site's change log in one file."""
    rows = await service.site_log(None, since, until, 50000, "ncr" if region == "ncr" else "pune")
    if format == "csv":
        return PlainTextResponse(service.site_csv(rows), media_type="text/csv",
                                 headers={"Content-Disposition": 'attachment; filename="sites-log.csv"'})
    return {"rows": rows, "count": len(rows)}


@router.get("/sites/{site_id}/log")
async def get_site_log(site_id: str, _: StaffPrincipal, since: datetime | None = None, until: datetime | None = None,
                       format: str = Query(default="json", pattern="^(json|csv)$"),
                       limit: int = Query(default=2000, le=20000)):
    rows = await service.site_log(site_id, since, until, limit)
    if format == "csv":
        return PlainTextResponse(service.site_csv(rows), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="{site_id}-log.csv"'})
    return {"site": site_id, "rows": rows, "count": len(rows)}


@router.get("/teams")
async def list_teams(_: StaffPrincipal, city_id: str = "pune") -> list[dict]:
    """Open incidents with their needs per capability and the units on each."""
    return await service.teams(city_id)


@router.get("/{unit_id}/track")
async def get_track(unit_id: str, _: StaffPrincipal, since: datetime | None = None,
                    limit: int = Query(default=1000, le=5000)) -> dict:
    return await service.track(unit_id, since, limit)


@router.get("/{unit_id}/log")
async def get_log(unit_id: str, _: StaffPrincipal, since: datetime | None = None, until: datetime | None = None,
                  positions: bool = True, format: str = Query(default="json", pattern="^(json|csv)$"),
                  limit: int = Query(default=2000, le=20000)):
    rows = await service.unit_log(unit_id, since, until, positions, limit)
    if format == "csv":
        return PlainTextResponse(service.to_csv(rows, unit_id), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="{unit_id}-log.csv"'})
    return {"unit": unit_id, "rows": rows, "count": len(rows)}


@router.get("/log/export")
async def export_all(_: StaffPrincipal, since: datetime | None = None, until: datetime | None = None,
                     city_id: str = "pune", positions: bool = False,
                     format: str = Query(default="csv", pattern="^(json|csv)$")):
    """Every unit's log in one file (events and field reports; positions optional)."""
    units = await service.units_overview(city_id)
    rows: list[dict] = []
    for u in units:
        for r in await service.unit_log(u["id"], since, until, positions, 5000):
            rows.append({"unit": u["id"], **r})
    rows.sort(key=lambda r: r["at"], reverse=True)
    if format == "csv":
        return PlainTextResponse(service.to_csv(rows), media_type="text/csv",
                                 headers={"Content-Disposition": 'attachment; filename="units-log.csv"'})
    return {"rows": rows, "count": len(rows)}
