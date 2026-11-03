"""Audit trail: what the agents decided, and everything each decision was based on (app/audit/basis.py).

    GET /audit/decisions            agent decisions, newest first (filter by group, incident, text)
    GET /audit/decisions/{event_id} the full basis of one decision: chain, belief then and now, reports and
                                    photos, camera frames and clips, drone observations, satellite passes and
                                    images, sensor readings, units and routes
    GET /audit/media/{media_id}     the actual image or clip bytes (staff)
    GET /audit/satellite            Copernicus GFM / Sentinel-1 reading at any point (with image URLs)
"""
from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import Response

from app.audit import basis as audit
from app.core.errors import NotFound
from app.core.security import StaffPrincipal

router = APIRouter(tags=["audit"])


@router.get("/audit/decisions")
async def list_decisions(_: StaffPrincipal, group: str | None = None, incident_id: str | None = None,
                         q: str | None = Query(default=None, max_length=80), before: int | None = None,
                         limit: int = Query(default=80, ge=1, le=300), city_id: str = "pune") -> dict:
    items = await audit.decisions(city_id=city_id, group=group, incident_id=incident_id, q=q, before=before, limit=limit)
    return {"decisions": items, "groups": list(audit.GROUPS)}


@router.get("/audit/decisions/{event_id}")
async def decision_basis(event_id: int, _: StaffPrincipal) -> dict:
    out = await audit.basis(event_id)
    if out is None:
        raise NotFound("No such decision.")
    return out


@router.get("/audit/media/{media_id}")
async def media(media_id: int, _: StaffPrincipal) -> Response:
    got = await audit.media_bytes(media_id)
    if got is None:
        raise NotFound("No such media.")
    data, mime = got
    return Response(content=data, media_type=mime, headers={"Cache-Control": "private, max-age=3600"})


@router.get("/audit/satellite")
async def satellite(_: StaffPrincipal, lat: float = Query(ge=-80, le=80), lng: float = Query(ge=-180, le=180)) -> dict:
    from app.eo import gfm
    return await gfm.observe(lng, lat)
