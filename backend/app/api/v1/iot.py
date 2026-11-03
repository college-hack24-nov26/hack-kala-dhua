"""LoRa sensor nodes and the sensor analytics page.

    POST /iot/observations       the LoRa bridge's readings (gateway key, like the mesh)
    GET  /analytics/overview     nodes + latest scores + flagged readings (staff)
    GET  /analytics/spatial      one metric per node, for a heatmap (staff)
    GET  /analytics/timeseries   one node's history (staff)
    GET  /iot/virtual            the simulated fleet: on/off, size, running episodes (staff)
    POST /iot/virtual            switch the simulated fleet on or off (staff)
    POST /iot/virtual/deploy     drop a new simulated node into the field (staff)
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import Field

from app.api.v1.mesh import _gateway
from app.core.security import StaffPrincipal
from app.iot import service, virtual
from app.schemas.domain import Camel

router = APIRouter(tags=["iot"])


class Observation(Camel):
    """One gateway line, already split into fields by the bridge. Every sensor
    field is optional: a node with a dead sensor sends an empty field."""

    node: str = Field(min_length=1, max_length=40)
    seq: int | None = None
    up: int | None = None
    mq2: float | None = None
    mq135: float | None = None
    temp_c: float | None = None
    tilt_deg: float | None = None
    gyro_dps: float | None = None
    vib_g: float | None = None
    mic: float | None = None
    piezo: float | None = None
    knocks: int | None = Field(default=None, ge=0, le=1000)
    tilt_sw: int | None = None
    pir: int | None = None
    #: node.ino bitmask of MIMICKED channels (1 mq2, 2 mq135, 4 temp, 8 imu, 0x10 mic, 0x20 piezo, 0x40 tilt)
    sim: int | None = Field(default=None, ge=0, le=0xFF)
    rssi: int | None = None
    snr: float | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    received_at: str | None = None
    raw: str | None = Field(default=None, max_length=200)


class ObservationsIn(Camel):
    gateway_id: str = Field(default="lora-gw", min_length=1, max_length=80)
    city_id: str = "pune"
    observations: list[Observation] = Field(default_factory=list, max_length=200)


@router.post("/iot/observations")
async def post_observations(body: ObservationsIn,
                            x_mesh_gateway_key: str | None = Header(default=None)) -> dict:
    _gateway(x_mesh_gateway_key)
    return await service.ingest([o.model_dump() for o in body.observations],
                                gateway_id=body.gateway_id, city_id=body.city_id)


@router.get("/iot/commands")
async def iot_commands(gateway_id: str = "lora-gw",
                       x_mesh_gateway_key: str | None = Header(default=None)) -> dict:
    """Event cues for the LoRa nodes, pulled by the command-centre link."""
    _gateway(x_mesh_gateway_key)
    return {"commands": service.take_cues()}


@router.get("/analytics/overview")
async def analytics_overview(_: StaffPrincipal, city_id: str = "pune", minutes: int = 30) -> dict:
    return await service.overview(city_id, minutes)


@router.get("/analytics/spatial")
async def analytics_spatial(_: StaffPrincipal, metric: str = "overall", city_id: str = "pune",
                            minutes: int = 10) -> dict:
    try:
        return await service.spatial(metric, city_id, minutes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/analytics/timeseries")
async def analytics_timeseries(_: StaffPrincipal, node: str, minutes: int = 30,
                               points: int = 300) -> dict:
    return await service.timeseries(node, minutes, points)


class VirtualToggle(Camel):
    enabled: bool


class DeployIn(Camel):
    kind: str = Field(default="rescue", pattern="^(field|gas|struct|rescue)$")
    region: str = "pune"
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    #: start an episode on it shortly after it lands: f gas/fire, c structural, t trapped
    episode: str | None = Field(default=None, pattern="^[fct]$")


@router.get("/iot/virtual")
async def virtual_status(_: StaffPrincipal) -> dict:
    return virtual.status()


@router.post("/iot/virtual")
async def virtual_toggle(body: VirtualToggle, _: StaffPrincipal) -> dict:
    return virtual.set_enabled(body.enabled)


@router.post("/iot/virtual/deploy")
async def virtual_deploy(body: DeployIn, _: StaffPrincipal) -> dict:
    try:
        return await virtual.deploy(body.kind, body.region, lat=body.lat, lon=body.lon,
                                    episode=body.episode)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
