"""Reference geography. Public: a resident reads the map without an account."""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.db.repositories import queries as q
from app.schemas.domain import Lifeline, Shelter, Ward, WardLocation

router = APIRouter(tags=["geography"])


@router.get("/wards", response_model=list[Ward])
async def get_wards() -> list[Ward]:
    return await q.list_wards()


@router.get("/lifelines", response_model=list[Lifeline])
async def get_lifelines() -> list[Lifeline]:
    return await q.list_lifelines()


@router.get("/shelters", response_model=list[Shelter])
async def get_shelters() -> list[Shelter]:
    return await q.list_shelters()


@router.get("/wards/locate", response_model=WardLocation)
async def locate(
    lng: float = Query(..., ge=-180, le=180),
    lat: float = Query(..., ge=-90, le=90),
    city_id: str = Query(default="pune"),
) -> WardLocation:
    """Which ward is this coordinate in?

    Called with the browser's real GPS fix, so it has to handle the honest case
    where the answer is "none of them". A resident standing outside the covered
    area gets `inside: false`, the nearest ward, and how far away it is, rather
    than being silently snapped to whichever polygon happened to be closest and
    then shown that ward's flood risk as if it were their own.

    Deployments grow outward one ward at a time; pretending otherwise is how
    someone gets told they are safe because the system quietly answered a
    question about a place 14 km away.
    """
    return await q.locate_ward(lng, lat, city_id)
