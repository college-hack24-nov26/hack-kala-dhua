"""High-throughput doors: accept, queue, answer 202 with a ticket.

    POST /ingest/telemetry      sensor readings in bulk (gateway key)      lane: bulk
    POST /ingest/reports        a citizen report, queued (anyone)          lane: critical
    GET  /ingest/tickets/{id}   what happened to something queued
    GET  /status/ingest         lane depths, throughput, shedding

The existing synchronous doors (`/iot/observations`, `/reports`,
`/citizen/report`) are unchanged: the apps keep their behaviour. These are the
doors for volume: a gateway with a backlog, a city's worth of phones in the
first minute of a flood, a load test.

A `202` here means "accepted and durable for as long as the queue is"; a `503`
with `Retry-After` means the bulk lane is full and the sender should re-send
(gateways already retry). A report is never refused for load: if its lane is
full it is processed inline, slower, and still answered.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Header, Response
from pydantic import Field

from app.api.v1.iot import Observation
from app.api.v1.mesh import _gateway
from app.core import ingest
from app.core.errors import NotFound
from app.core.security import CurrentPrincipal
from app.schemas.domain import Camel, CategoryId, CityId, LngLat

router = APIRouter(tags=["ingest"])


class TelemetryIn(Camel):
    gateway_id: str = Field(default="lora-gw", min_length=1, max_length=80)
    city_id: str = "pune"
    observations: list[Observation] = Field(default_factory=list, max_length=1000)


class QueuedReportIn(Camel):
    ward_id: str
    category: CategoryId
    location: LngLat
    note: str = Field(default="", max_length=2000)
    photo_url: str | None = None
    source: str = "app"
    device_id: str | None = None
    occurred_at: datetime | None = None
    city_id: CityId = "pune"


def _shed(response: Response, retry: int) -> dict:
    response.status_code = 503
    response.headers["Retry-After"] = str(retry)
    return {"accepted": False, "detail": f"Ingest is at capacity; re-send after {retry}s."}


@router.post("/ingest/telemetry", status_code=202)
async def ingest_telemetry(body: TelemetryIn, response: Response,
                           x_mesh_gateway_key: str | None = Header(default=None)) -> dict:
    _gateway(x_mesh_gateway_key)
    tickets: list[str] = []
    shed = 0
    retry = 0
    for o in body.observations:
        got = await ingest.submit("telemetry", {"gatewayId": body.gateway_id, "cityId": body.city_id,
                                                "observation": o.model_dump()},
                                  idempotency_key=f"{o.node}:{o.seq}:{o.up}" if o.seq is not None and o.up is not None else None)
        if got.accepted and got.ticket:
            tickets.append(got.ticket)
        elif got.shed:
            shed += 1
            retry = max(retry, got.retry_after)
    if shed and not tickets:
        return _shed(response, retry)
    if shed:
        response.headers["Retry-After"] = str(retry)
    return {"accepted": len(tickets), "shed": shed, "tickets": tickets[:50]}


@router.post("/ingest/reports", status_code=202)
async def ingest_report(body: QueuedReportIn, principal: CurrentPrincipal, response: Response,
                        idempotency_key: str | None = Header(default=None, max_length=128)) -> dict:
    payload = body.model_dump(mode="json")
    payload["source"] = body.source if principal.is_staff or body.source == "app" else "app"
    payload["reporter_id"] = principal.user_id
    payload["reporter_name"] = principal.full_name or "Anonymous"
    got = await ingest.submit("report", payload, idempotency_key=idempotency_key)
    if got.inline_result is not None:
        response.status_code = 201
    return {"accepted": True, "ticket": got.ticket, "duplicate": got.duplicate,
            "statusUrl": f"/api/v1/ingest/tickets/{got.ticket}", "result": got.inline_result}


@router.get("/ingest/tickets/{ticket_id}")
async def ingest_ticket(ticket_id: str) -> dict:
    t = await ingest.ticket(ticket_id)
    if t is None:
        raise NotFound("No such ticket, or it is older than ten minutes.")
    return t


@router.get("/status/ingest")
async def ingest_status() -> dict:
    return ingest.status()
