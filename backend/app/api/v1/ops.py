"""Operations API: see every job, stop one, say what to do instead.

Reads are open to staff. The one write, `/ops/cancel`, is a proposal: it goes
through the policy gate as `cancel_assignment`, exactly like the Copilot's
actions, and records who asked. The officer never gets a button that bypasses
the delegation matrix; they get one that asks it.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import Field

from app.core.errors import BadRequest
from app.core.security import StaffPrincipal
from app.ops import operations as ops
from app.schemas.domain import Camel

router = APIRouter(tags=["operations"])


class AutonomyIn(Camel):
    paused: bool
    reason: str | None = Field(default=None, max_length=200)


@router.get("/ops/autonomy")
async def autonomy_status(_: StaffPrincipal) -> dict:
    from app.ops import autonomy

    return autonomy.status()


@router.post("/ops/autonomy")
async def autonomy_set(body: AutonomyIn, who: StaffPrincipal) -> dict:
    """Emergency stop / resume. Pausing is always allowed to staff; it is the
    safe direction. It never recalls crews already out."""
    from app.ops import autonomy

    name = who.full_name or str(getattr(who.role, "value", who.role))
    if body.paused:
        return await autonomy.pause(by=name, reason=body.reason)
    return await autonomy.resume(by=name)


class Instead(Camel):
    kind: Literal["replan", "redirect", "stage", "hold", "return_to_base"] = "replan"
    incident_id: str | None = None
    ward_id: str | None = None
    minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    #: Display names, echoed back in the sentence; never used to act.
    incident_title: str | None = None
    ward_name: str | None = None


class PreviewIn(Camel):
    resource_id: str
    instead: Instead = Field(default_factory=Instead)
    city_id: str = "pune"


class CancelIn(PreviewIn):
    reason: str = Field(min_length=3, max_length=600)


def _instead(i: Instead) -> dict:
    return {k: v for k, v in i.model_dump().items() if v is not None}


@router.get("/ops")
async def list_operations(_: StaffPrincipal, city_id: str = "pune") -> dict:
    return await ops.active(city_id)


@router.get("/ops/alternatives/{resource_id}")
async def alternatives(resource_id: str, _: StaffPrincipal, city_id: str = "pune") -> dict:
    return {"alternatives": await ops.alternatives(resource_id, city_id)}


@router.post("/ops/preview")
async def preview(body: PreviewIn, _: StaffPrincipal) -> dict:
    try:
        return await ops.preview(body.resource_id, _instead(body.instead), body.city_id)
    except ops.CannotCancel as exc:
        raise BadRequest(str(exc)) from exc


@router.post("/ops/cancel")
async def cancel(body: CancelIn, principal: StaffPrincipal) -> dict:
    instead = _instead(body.instead)
    if instead.get("kind") == "redirect" and not instead.get("incident_id"):
        raise BadRequest("Say which incident to send it to instead.")
    if instead.get("kind") == "stage" and not instead.get("ward_id"):
        raise BadRequest("Say which ward to stage it in.")
    try:
        return await ops.propose_cancel(
            resource_id=body.resource_id, reason=body.reason.strip(),
            instead=instead, actor=principal.full_name or str(principal.role),
            city_id=body.city_id,
        )
    except ops.CannotCancel as exc:
        raise BadRequest(str(exc)) from exc


@router.post("/ops/overrides/{override_id}/lift")
async def lift(override_id: str, principal: StaffPrincipal) -> dict:
    try:
        return await ops.lift(override_id, actor=principal.full_name or str(principal.role))
    except ops.CannotCancel as exc:
        raise BadRequest(str(exc)) from exc
