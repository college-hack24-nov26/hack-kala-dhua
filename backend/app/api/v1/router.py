"""API v1 surface."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    agent_graph,
    auth,
    config,
    citizen,
    copilot,
    demo,
    drone,
    geography,
    iot,
    mesh,
    operations,
    ops,
    personas,
    replay,
    reports,
    risk,
    runs,
    system,
    ingest,
    agent_log,
    audit,
    units,
    nav,
    surge,
)
from app.hazards import registry

# Adapters register once, at import of the router, so `/hazards` is populated
# before the first request rather than on first use.
registry.load_adapters()

api_router = APIRouter()
# First, on purpose: routing is a linear scan over every route (~250 here),
# so the doors that take the volume are matched before the console's long tail.
api_router.include_router(ingest.router)
api_router.include_router(units.router)
api_router.include_router(nav.router)
api_router.include_router(surge.router)
api_router.include_router(system.router)
api_router.include_router(auth.router)
api_router.include_router(runs.router)
api_router.include_router(reports.router)
api_router.include_router(demo.router)
api_router.include_router(personas.router)
api_router.include_router(geography.router)
api_router.include_router(risk.router)
api_router.include_router(operations.router)
api_router.include_router(citizen.router)
api_router.include_router(config.router)
api_router.include_router(copilot.router)
api_router.include_router(replay.router)
api_router.include_router(ops.router)
api_router.include_router(mesh.router)
api_router.include_router(agent_graph.router)
api_router.include_router(iot.router)
api_router.include_router(drone.router)
api_router.include_router(agent_log.router)
api_router.include_router(audit.router)
