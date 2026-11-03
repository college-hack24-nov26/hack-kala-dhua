"""Mesh gateway API.

Three endpoints for a gateway (a phone with signal running the bitchat fork's
forwarder, or `scripts/mesh_bridge.py` on a laptop next to one), authenticated
by `X-Mesh-Gateway-Key` rather than a staff login because a gateway is a
device, not a person:

    POST /mesh/inbound       packets heard on the mesh
    GET  /mesh/outbox        what to broadcast next
    POST /mesh/outbox/ack    what was broadcast
    POST /mesh/civ-sync      a bitchat phone's whole "back online" bundle
    POST /mesh/gateway/heartbeat   "this phone is linked", every ~10 s

Plus `POST /ingest/sensor` for a camera node that has internet and can skip the
mesh, and `GET /mesh/status` for the console.

With `MESH_GATEWAY_KEY` unset every gateway endpoint answers 403. An open
endpoint that files reports into the trust pipeline is a side door, and 014
closed the last one of those.
"""

from __future__ import annotations

import contextlib
import hmac
import time

from fastapi import APIRouter, Header
from pydantic import Field

from app.core.config import settings
from app.core.errors import Forbidden
from app.core.security import StaffPrincipal
from app.mesh import envelope, service
from app.schemas.domain import Camel

router = APIRouter(tags=["mesh"])


def _gateway(key: str | None) -> None:
    if not settings.mesh_gateway_key:
        raise Forbidden("Mesh gateway is disabled: MESH_GATEWAY_KEY is not set.")
    if not key or not hmac.compare_digest(key, settings.mesh_gateway_key):
        raise Forbidden("Wrong or missing X-Mesh-Gateway-Key.")


class InboundIn(Camel):
    gateway_id: str = Field(min_length=1, max_length=80)
    packets: list[str] = Field(default_factory=list, max_length=100)
    city_id: str = "pune"


class AckIn(Camel):
    gateway_id: str = Field(min_length=1, max_length=80)
    ids: list[str] = Field(default_factory=list, max_length=200)


class SensorIn(Camel):
    """A camera node's detection, sent straight over HTTPS when it can."""

    node_id: str = Field(min_length=1, max_length=60)
    event_id: str = Field(min_length=1, max_length=60)
    kind: str
    confidence: float = Field(ge=0, le=1)
    lat: float
    lon: float
    frames: str | None = None
    vlm_agreed: bool = False
    caption: str | None = Field(default=None, max_length=300)
    occurred_at: int | None = None
    city_id: str = "pune"
    #: Optional thumbnail of the frame the detection came from (base64 or data URL,
    #: <= 400 KB). Only over HTTPS; LoRa never carries images.
    image_base64: str | None = Field(default=None, max_length=600_000)


@router.post("/mesh/inbound")
async def inbound(body: InboundIn,
                  x_mesh_gateway_key: str | None = Header(default=None)) -> dict:
    _gateway(x_mesh_gateway_key)
    results = await service.receive(body.packets, gateway_id=body.gateway_id,
                                    city_id=body.city_id)
    return {"results": results}


@router.get("/mesh/outbox")
async def outbox(gateway_id: str, limit: int = 20, city_id: str = "pune",
                 x_mesh_gateway_key: str | None = Header(default=None)) -> dict:
    _gateway(x_mesh_gateway_key)
    return {"messages": await service.pull(gateway_id=gateway_id,
                                           limit=max(1, min(limit, 50)),
                                           city_id=city_id)}


@router.post("/mesh/outbox/ack")
async def outbox_ack(body: AckIn,
                     x_mesh_gateway_key: str | None = Header(default=None)) -> dict:
    _gateway(x_mesh_gateway_key)
    return {"acked": await service.ack(body.ids)}


class CivBundleIn(Camel):
    """`bitchat.civ.sync/v1`, as the bitchat fork's SyncBundleBuilder writes it."""

    bundle_schema: str = "bitchat.civ.sync/v1"
    produced_at: int | None = None
    gateway_id: str = Field(default="bitchat", min_length=1, max_length=80)
    city_id: str = "pune"
    incidents: list[dict] = Field(default_factory=list, max_length=500)
    shelters: list[dict] = Field(default_factory=list, max_length=500)


@router.post("/mesh/civ-sync")
async def civ_sync(body: CivBundleIn,
                   x_mesh_gateway_key: str | None = Header(default=None)) -> dict:
    """A phone that carried mesh traffic into signal posts it all here. Each
    incident is turned into an R packet (or an S packet for a VLM camera brief
    with a known hazard class) and handled exactly like one heard on the mesh,
    so a report that arrives both ways is still one report."""
    _gateway(x_mesh_gateway_key)
    return await service.receive_civ_bundle(body.model_dump(), gateway_id=body.gateway_id,
                                            city_id=body.city_id)


class HeartbeatIn(Camel):
    """A gateway phone saying it is alive and linked, with what it can see."""

    gateway_id: str = Field(min_length=1, max_length=80)
    city_id: str = "pune"
    label: str | None = Field(default=None, max_length=80)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    peers: int | None = Field(default=None, ge=0, le=10_000)
    queued: int | None = Field(default=None, ge=0, le=100_000)
    battery: int | None = Field(default=None, ge=0, le=100)
    app_version: str | None = Field(default=None, max_length=40)
    listen: bool | None = None


@router.post("/mesh/gateway/heartbeat")
async def gateway_heartbeat(body: HeartbeatIn,
                            x_mesh_gateway_key: str | None = Header(default=None)) -> dict:
    """Marks the phone as a live gateway so the console's Mesh screen shows it
    even before it has carried a single packet. Also the phone's "Sync now"
    check: a 200 here proves the URL and the key are right."""
    _gateway(x_mesh_gateway_key)
    return await service.gateway_heartbeat(body.model_dump())


@router.post("/ingest/sensor")
async def ingest_sensor(body: SensorIn,
                        x_mesh_gateway_key: str | None = Header(default=None)) -> dict:
    """Same handling as an S packet off the mesh, so the two paths cannot
    disagree, and deduplicated with it by event id: a detection that arrives
    both over HTTPS and over the mesh is one report."""
    _gateway(x_mesh_gateway_key)
    text = envelope.encode(
        "S",
        {"id": body.event_id, "n": body.node_id, "k": body.kind,
         "c": round(body.confidence, 3), "la": body.lat, "lo": body.lon,
         "f": body.frames, "v": 1 if body.vlm_agreed else None,
         "x": body.caption, "t": body.occurred_at or int(time.time())},
        key=settings.mesh_hmac_key,
    )
    results = await service.receive([text], gateway_id=f"https:{body.node_id}",
                                    city_id=body.city_id)
    if body.image_base64:
        from app import media
        await media.store(body.image_base64, source="camera", caption=body.caption,
                          event_ref=body.event_id, city_id=body.city_id)
    return results[0]


class CameraIn(Camel):
    """One edge-AI camera frame: what the VLM said, the picture, and where the camera is.

    Every frame is logged (`camera.frame`) and its picture kept, so the console
    shows what the camera sees even when nothing is wrong. Only a frame whose
    description names a hazard becomes a report, through the same path as an
    S packet (trust, dedup, needs), and that report carries the picture.
    """

    node_id: str = Field(min_length=1, max_length=60)
    event_id: str = Field(min_length=1, max_length=60)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    caption: str = Field(default="", max_length=1200)
    alert_level: str | None = Field(default=None, max_length=20)
    hazards: list[str] = Field(default_factory=list, max_length=12)
    kind: str | None = Field(default=None, max_length=30)
    confidence: float = Field(default=0.7, ge=0, le=1)
    #: True when lat/lon is a stand-in point chosen for the demo, not where the camera really is.
    demo_position: bool = False
    label: str | None = Field(default=None, max_length=80)
    occurred_at: int | None = None
    city_id: str = "pune"
    image_base64: str | None = Field(default=None, max_length=600_000)
    #: Optional short clip of the seconds before this frame (mp4 / webm / gif, <= ~3.5 MB), kept for the audit
    #: trail. Stored only once migration 041 allows video in feed_media.
    clip_base64: str | None = Field(default=None, max_length=4_800_000)
    clip_mime: str | None = Field(default=None, max_length=40)


@router.post("/ingest/camera")
async def ingest_camera(body: CameraIn,
                        x_mesh_gateway_key: str | None = Header(default=None)) -> dict:
    _gateway(x_mesh_gateway_key)
    from app import media
    from app.world import events as ev
    from app.world.clock import WALL

    kind = (body.kind or "").lower() or service.scene_kind(body.caption, body.hazards, body.alert_level)
    media_id = await media.store(body.image_base64, source="camera", caption=body.caption[:500],
                                 event_ref=body.event_id, city_id=body.city_id) if body.image_base64 else None
    await service.camera_seen(body.node_id, body.lat, body.lon, label=body.label,
                              meta={"demoPosition": body.demo_position, "lastCaption": body.caption[:300],
                                    "alertLevel": body.alert_level, "kind": kind})
    ward = await service.ward_at(body.lon, body.lat, body.city_id)
    clip_id = None
    if body.clip_base64:
        import base64 as b64
        from app.incidents.fusion import _store_media
        raw = body.clip_base64.split(",", 1)[-1]
        mime = (body.clip_mime or "video/mp4").lower()
        if mime.startswith(("video/", "image/gif")):
            with contextlib.suppress(Exception):
                clip_id = await _store_media(b64.b64decode(raw), caption=f"Clip before: {body.caption[:200]}",
                                             event_ref=body.event_id, city_id=body.city_id, mime=mime, source="camera")
    result: dict = {"outcome": "logged", "kind": kind, "mediaId": media_id, "clipMediaId": clip_id, "wardId": ward}
    if kind and kind in service.SENSOR_CATEGORY:
        caption = body.caption[:280] + (" (camera at a demo position)" if body.demo_position else "")
        text = envelope.encode(
            "S",
            {"id": body.event_id, "n": body.node_id, "k": kind, "c": round(body.confidence, 3),
             "la": body.lat, "lo": body.lon, "f": "vlm", "v": 1, "x": caption,
             "t": body.occurred_at or int(time.time())},
            key=settings.mesh_hmac_key,
        )
        res = (await service.receive([text], gateway_id=f"https:{body.node_id}", city_id=body.city_id))[0]
        result.update(outcome=res.get("outcome"), ref=res.get("ref"), reason=res.get("reason"))
        if media_id and res.get("outcome") in ("report", "linked", "held"):
            await service.attach_camera_image(body.node_id, media_id, body.image_base64)
    with contextlib.suppress(Exception):
        await ev.append(clock=WALL, kind="camera.frame", actor=f"sensor:{body.node_id}", subject_type="camera",
                        subject_id=body.node_id, city_id=body.city_id, ward_id=ward,
                        payload={"eventId": body.event_id, "caption": body.caption[:600], "alertLevel": body.alert_level,
                                 "hazards": body.hazards, "kind": kind, "mediaId": media_id, "clipMediaId": clip_id,
                                 "lat": body.lat, "lon": body.lon,
                                 "demoPosition": body.demo_position, "outcome": result["outcome"],
                                 "incidentId": result.get("ref") if result.get("outcome") in ("report", "linked") else None})
    return result


@router.get("/mesh/status")
async def mesh_status(_: StaffPrincipal, city_id: str = "pune", region: str = "all") -> dict:
    """region: pune | ncr | all (the console's region picker)."""
    return await service.status(city_id, region if region in ("pune", "ncr", "all") else "all")


@router.get("/mesh/detail")
async def mesh_detail(_: StaffPrincipal, packet: int | None = None, report: str | None = None) -> dict:
    """What actually came in behind one live-feed line: the packet, the camera/VLM
    description and sensor channels, the node, the report (photo or the VLM's
    reading of it) and the incident with the units on it."""
    return await service.detail(packet, report)
