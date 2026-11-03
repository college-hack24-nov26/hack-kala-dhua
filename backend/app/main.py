"""Indradhanu API.

Application wiring only: configuration, lifespan, middleware, routers. Every
piece of behaviour lives in its own module so this file stays readable.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import ORJSONResponse

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import RequestContextMiddleware
from app.core.ratelimit import LIMITS as RATE_LIMITS, RateLimitMiddleware
from app import taxonomy
from app.db import session as db
from app.demo import runner as demo_runner
from app.agents import event_router

configure_logging(settings.log_level, json_logs=settings.is_production)
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.connect()
    # Which hazards, categories, resource kinds and capabilities exist is data.
    # Read it once here so nothing downstream has to ask the database what a
    # boat can do in the middle of a dispatch decision.
    await taxonomy.load()
    log.info(
        "startup",
        env=settings.indradhanu_env,
        llm_provider=settings.llm_provider,
        hazards=len(taxonomy.cache.hazards),
        resource_kinds=len(taxonomy.cache.resource_kinds),
    )
    # An emergency stop survives a restart: read it before anything can act.
    from app.ops import autonomy

    await autonomy.load()
    # Listen to the event log from now on, demo or no demo.
    from app.core import loopwatch
    loopwatch.start()
    await event_router.start()
    # A sensor field that exists with or without the LoRa hardware on the bench.
    from app.iot import virtual as iot_virtual

    await iot_virtual.start()
    # The rescue swarm's live feed: survivors become incidents, obstructions
    # become road blocks the router avoids. Read-only; retries quietly.
    from app.drone import swarm as drone_swarm

    await drone_swarm.start()
    # Two-way link with the agentic drone swarm (drone_swarm repo): filtered drone
    # log, tasking from the panel, recheck-and-replan reasoner. SWARM_AGENCY=1.
    from app.drone import swarm_link

    if swarm_link.enabled():
        await swarm_link.start()
    # The ingest bus's drainers. Zero here when a separate worker deployment
    # (python -m app.worker) does the draining.
    from app.core import ingest

    await ingest.start()
    try:
        yield
    finally:
        await ingest.stop()
        await swarm_link.stop()
        await drone_swarm.stop()
        await iot_virtual.stop()
        await event_router.stop()
        # Stop the demo loop before the pool closes. Otherwise a reload leaves a
        # task writing into a disconnected pool and the log fills with noise
        # that looks like a real failure.
        await demo_runner.stop()
        await db.disconnect()
        log.info("shutdown")


app = FastAPI(
    title="Indradhanu API",
    description=(
        "Hazard-agnostic climate disaster early warning and response. "
        "Forecast to ward-level decision to household instruction."
    ),
    version="0.1.0",
    default_response_class=ORJSONResponse,
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None,
    openapi_url=None if settings.is_production else "/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(RequestContextMiddleware, sample=settings.access_log_sample)
# Outside the request-context middleware, so a refused request is still logged
# with its correlation id and still leaves through CORS. Starlette applies these
# in reverse, so the limiter sees the request before the handler and the 429
# travels back out the same way a 200 would.
app.add_middleware(RateLimitMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=1024)
# A CORS misconfiguration is the one failure this service cannot see: the API
# answers 200, the browser discards the response, and the logs show a healthy
# deployment while the frontend shows nothing. Say out loud, once, which origins
# this process will actually accept, so the answer is in the startup log rather
# than in a dashboard someone has to remember to open.
# The public write endpoints are metered. Said at startup for the same reason
# CORS is: a limit nobody can see is a limit nobody checks until it bites.
log.info("rate_limits", limits={k: f"{v[0]}/{v[1]}s" for k, v in RATE_LIMITS.items()})
log.info(
    "cors",
    origins=settings.cors_origin_list,
    origin_regex=settings.cors_origin_regex or None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_origin_regex=settings.cors_origin_regex or None,
    allow_credentials=True,
    # DELETE was missing, and three endpoints use it: removing a ward, a
    # resource or a lifeline on the configuration screen. Same origin in
    # development, so it worked on a laptop; cross-origin in production, where
    # the preflight refused the method and every delete button failed with an
    # opaque network error and nothing in the API log, because the request never
    # arrived.
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    # `X-Indradhanu-Device` labels an anonymous caller for the rate limiter and
    # the trust scorer. A header the preflight does not allow is not dropped —
    # the whole request is refused — so it has to be named here.
    allow_headers=[
        "Authorization", "Content-Type", "X-Request-ID", "X-Indradhanu-Device",
    ],
    expose_headers=["X-Request-ID"],
)

register_error_handlers(app)
# Health first: load balancers ask it every few seconds per replica, and route
# matching is a linear scan.
@app.get("/health/live", tags=["health"], include_in_schema=False)
async def live() -> dict[str, str]:
    """Liveness: the process is up. Deliberately does not touch the database."""
    return {"status": "ok"}


@app.get("/health", tags=["health"])
async def health() -> dict[str, object]:
    """Readiness: the process is up *and* its dependencies answer."""
    database = await db.healthcheck()
    return {
        "status": "ok" if database else "degraded",
        "env": settings.indradhanu_env,
        "database": database,
        "llm_provider": settings.llm_provider,
    }


app.include_router(api_router, prefix="/api/v1")



