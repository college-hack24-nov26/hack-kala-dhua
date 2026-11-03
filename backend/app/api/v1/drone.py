"""Drone frame localization (mapp-s map matcher) in the command centre.

    POST /drone/localized   the finder's webhook: one result (gateway key)
    POST /drone/localize    console upload, forwarded to the finder (staff)
    GET  /drone/recent      the latest results, newest first (staff)
    GET  /drone/swarm       the rescue swarm listener: connection, run, drones,
                            survivors on the map, Jev decisions, what it changed (staff)
    POST /drone/swarm       point it at a new tunnel URL, switch it, re-anchor it (staff)
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.api.v1.mesh import _gateway
from app.core.security import StaffPrincipal
from app.drone import service, swarm

router = APIRouter(tags=["drone"])


class LocalizedIn(BaseModel):
    result: dict[str, Any]
    source: str = Field(default="finder", max_length=40)
    drone: str | None = Field(default=None, max_length=40)
    thumb: str | None = None
    city_id: str = "pune"


@router.post("/drone/localized")
async def drone_localized(body: LocalizedIn,
                          x_mesh_gateway_key: str | None = Header(default=None)) -> dict:
    _gateway(x_mesh_gateway_key)
    entry = await service.record(body.result, source=body.source, drone=body.drone,
                                 thumb=body.thumb, city_id=body.city_id)
    return {"recorded": entry["id"], "accepted": entry["accepted"]}


@router.post("/drone/localize")
async def drone_localize(_: StaffPrincipal, query: UploadFile = File(...),
                         drone: str | None = Form(default=None)) -> dict:
    data = await query.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty image.")
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image over 15 MB.")
    out = await service.localize(data, query.filename or "frame.jpg", query.content_type or "image/jpeg",
                                 drone=drone)
    if not out["ok"]:
        raise HTTPException(status_code=503 if out.get("status") == 503 else 502,
                            detail=f"Localizer: {out['error']}")
    return out["entry"]


@router.get("/drone/recent")
async def drone_recent(_: StaffPrincipal, limit: int = 20) -> dict:
    return {"items": service.recent(max(1, min(limit, 40))), "finder": service.FINDER_URL}


class SwarmConfig(BaseModel):
    url: str | None = Field(default=None, pattern=r"^wss?://[^\s]+$", max_length=300)
    enabled: bool | None = None
    anchor_lat: float | None = Field(default=None, ge=-90, le=90)
    anchor_lon: float | None = Field(default=None, ge=-180, le=180)
    scale: float | None = Field(default=None, gt=0, le=100)


@router.get("/drone/swarm")
async def drone_swarm_status(_: StaffPrincipal) -> dict[str, Any]:
    return swarm.status()


@router.post("/drone/swarm")
async def drone_swarm_configure(body: SwarmConfig, _: StaffPrincipal) -> dict[str, Any]:
    anchor = ((body.anchor_lat, body.anchor_lon)
              if body.anchor_lat is not None and body.anchor_lon is not None else None)
    return await swarm.configure(url=body.url, enabled=body.enabled, anchor=anchor, scale=body.scale)


# --------------------------------------------------------------------------- swarm link --
# Two-way link with the agentic drone swarm (app/drone/swarm_link.py): filtered drone log,
# tasking from the panel, and the recheck-and-replan reasoner.
from app.drone import swarm_link  # noqa: E402


class LinkTask(BaseModel):
    action: str = Field(pattern=r"^(assign_drone|assign_unit|unassign|recall|recheck|update|close|cancel|new_task|replan|orchestrator|autonomy)$")
    incident_id: str | None = Field(default=None, max_length=40)
    drone_id: str | None = Field(default=None, max_length=20)
    drone_ids: list[str] | None = None
    resource_id: str | None = Field(default=None, max_length=40)
    resource_ids: list[str] | None = None
    kind: str | None = Field(default=None, max_length=30)
    role: str | None = Field(default=None, max_length=20)
    count: int | None = Field(default=None, ge=1, le=6)
    payload: list[str] | None = None
    people: int | None = Field(default=None, ge=0, le=500)
    severity: int | None = Field(default=None, ge=1, le=5)
    priority: int | None = Field(default=None, ge=1, le=5)
    category: str | None = Field(default=None, max_length=40)
    lng: float | None = Field(default=None, ge=-180, le=180)
    lat: float | None = Field(default=None, ge=-90, le=90)
    needs: list[str] | None = None
    note: str | None = Field(default=None, max_length=200)
    reason: str | None = Field(default=None, max_length=160)
    value: bool | None = None
    paused: bool | None = None


class LinkConfig(BaseModel):
    url: str | None = Field(default=None, max_length=300)
    token: str | None = Field(default=None, max_length=200)
    auto: bool | None = None


def _require_link():
    link = swarm_link.link()
    if link is None:
        raise HTTPException(status_code=409, detail="Swarm link is off. Paste the swarm URL and token in the panel first.")
    if not link.connected:
        raise HTTPException(status_code=503, detail=f"Swarm not connected{': ' + link.last_error if link.last_error else ''}.")
    return link


@router.get("/drone/link")
async def drone_link_status(_: StaffPrincipal, log_after: int = 0) -> dict[str, Any]:
    return swarm_link.status(log_after)


@router.post("/drone/link/task")
async def drone_link_task(body: LinkTask, who: StaffPrincipal) -> dict[str, Any]:
    link = _require_link()
    try:
        out = await link.task(body.model_dump(exclude_none=True), by=(who.full_name or "officer")[:40])
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ConnectionError, TimeoutError) as exc:
        raise HTTPException(status_code=503, detail=f"Swarm did not answer: {type(exc).__name__}") from exc
    if not out.get("ok") and "plan" not in out:
        raise HTTPException(status_code=422, detail=f"Swarm refused: {out.get('error')}")
    return out


@router.post("/drone/link/plans/{plan_id}/{decision}")
async def drone_link_plan(plan_id: str, decision: str, who: StaffPrincipal) -> dict[str, Any]:
    link = _require_link() if decision == "approve" else swarm_link.link()
    if link is None:
        raise HTTPException(status_code=409, detail="Swarm link is off.")
    try:
        if decision == "approve":
            return await link.apply_plan(plan_id, by=(who.full_name or "officer")[:40])
        if decision == "reject":
            return link.reject_plan(plan_id, by=(who.full_name or "officer")[:40])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown plan") from exc
    raise HTTPException(status_code=400, detail="decision must be approve or reject")


@router.post("/drone/link/config")
async def drone_link_config(body: LinkConfig, _: StaffPrincipal) -> dict[str, Any]:
    if body.url is not None or body.token is not None:
        # Token-only saves keep the current (or configured) address instead of failing on an empty URL.
        url = (body.url or (swarm_link.link().url if swarm_link.link() else "") or swarm_link.env("SWARM_WS_URL")).strip()
        if not url:
            raise HTTPException(status_code=400, detail="Enter the swarm address first (e.g. https://drone-1-oj8n.onrender.com).")
        if url.startswith("http"):
            url = "ws" + url[4:]
        url = url.rstrip("/")
        if url and not url.endswith("/ops/ws"):
            url += "/ops/ws"
        if not url.startswith(("ws://", "wss://")):
            raise HTTPException(status_code=400, detail="URL must be the swarm's https:// or wss:// address")
        token = body.token if body.token is not None else (swarm_link.link().token if swarm_link.link() else swarm_link.env("SWARM_INTEGRATION_TOKEN"))
        await swarm_link.start(url=url, token=token.strip())
    if body.auto is not None and swarm_link.link():
        swarm_link.link().auto = body.auto
    return swarm_link.status()
