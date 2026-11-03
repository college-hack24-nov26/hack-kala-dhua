"""Runs, the taxonomy, and the audit log.

Three endpoints that did not exist before and that the rest of the system needs:

  * `POST /runs` actually executes a hazard run, which is what fills every
    operational table. Until this existed the database was a well-designed
    empty room.
  * `GET /taxonomy` hands the frontend the hazards, categories, resource kinds
    and capabilities this deployment has, so the client stops hard-coding
    unions that a second city would invalidate.
  * `GET /events` is the PS20 activity and audit log, and `GET /events/{id}/chain`
    answers "why did this happen?" by walking the recorded causal chain rather
    than reconstructing it from timestamps.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.agents import orchestrator
from app.agents import preposition
from app.core.errors import BadRequest, NotFound
from app.core.security import CurrentPrincipal, StaffPrincipal
from app.hazards import registry
from app.schemas.domain import Event, RunRequest, RunResult, Taxonomy
from app.taxonomy import UnknownTaxonomyValue
from app.taxonomy import cache as taxonomy
from app.world import clock as clocks
from app.world import events as ev

router = APIRouter(tags=["runs"])


def _clock_for(sim_run_id: str | None):
    """Resolve a simulation scope, turning a bad one into a 400 rather than a 500.

    The docs page sends the literal string "string" for an unset optional field,
    which is how this first showed up as an internal error on an endpoint that
    was otherwise fine.
    """
    try:
        return clocks.get(sim_run_id)
    except clocks.UnknownScope as exc:
        raise BadRequest(str(exc)) from exc


@router.get("/taxonomy", response_model=Taxonomy)
async def get_taxonomy(city_id: str = Query(default="pune")) -> Taxonomy:
    """Everything this deployment knows about, as data.

    A client that reads this instead of hard-coding its own enums keeps working
    when a city adds cyclone or retires a vehicle type.
    """
    try:
        snapshot = taxonomy.snapshot(city_id)
    except UnknownTaxonomyValue as exc:
        raise NotFound(str(exc)) from exc

    # Maturity is a property of the adapter, not the row: a hazard can be
    # registered in the table with no scorer behind it yet, and the API should
    # say so rather than let a stub look like a model.
    adapters = {a.hazard: a for a in registry.all_adapters()}
    for hazard in snapshot.hazards:
        adapter = adapters.get(hazard.id)
        hazard.maturity = str(adapter.maturity) if adapter else None
    return snapshot


@router.post("/runs", response_model=RunResult, status_code=201)
async def start_run(body: RunRequest, principal: StaffPrincipal) -> RunResult:
    """Score a hazard across a city and produce a dispatchable plan.

    Synchronous on purpose for now: a run takes a couple of seconds and an
    officer who pressed the button should see the result, not a job id. It moves
    behind the event loop when the reactive planner lands.
    """
    try:
        taxonomy.hazard(body.hazard)
    except UnknownTaxonomyValue as exc:
        raise NotFound(str(exc)) from exc
    try:
        registry.get(body.hazard)
    except LookupError as exc:
        raise NotFound(
            f"{body.hazard!r} is a known hazard but has no adapter in this build."
        ) from exc

    clock = _clock_for(body.sim_run_id)
    return await orchestrator.run_hazard(
        body.hazard,
        city_id=body.city_id,
        clock=clock,
        actor=ev.officer(principal.full_name or principal.user_id or "unknown"),
    )


@router.post("/forecast/preposition", status_code=201)
async def propose_prepositioning(
    principal: StaffPrincipal,
    city_id: str = Query(default="pune"),
    sim_run_id: str | None = Query(default=None),
) -> dict:
    """Act on the forecast: propose moving equipment before it is needed.

    Separate from `POST /runs` on purpose. A run scores a hazard and dispatches
    against what has already happened; this reads the projection and asks
    whether anything should move *in anticipation*, which is a different
    question with a different answer when it is wrong. Keeping them apart also
    means a forecast an officer distrusts cannot quietly move vehicles as a side
    effect of a routine run.

    Every proposal goes through the same delegation gate as a dispatch.
    `preposition_equipment` is delegated to the Ward Officer under PMC DMP 2023
    cl. 6.1, so most auto-issue under that clause and appear in the decision
    feed with it cited. Nothing here moves a unit: the decision names the units
    and the ward, and the existing approval path carries it out.
    """
    clock = _clock_for(sim_run_id)
    decisions = await preposition.propose_all(city_id=city_id, clock=clock)
    return {
        "proposed": len(decisions),
        "decisions": decisions,
        # Said plainly, because "0 proposals" has two very different meanings and
        # an officer should not have to guess which one they are looking at.
        "note": (
            "Nothing is projected short enough to act on."
            if not decisions
            else f"{len(decisions)} proposal(s) in the decision gate."
        ),
        "actor": principal.full_name or principal.user_id or "unknown",
    }


@router.get("/events", response_model=list[Event])
async def list_events(
    _: CurrentPrincipal,
    sim_run_id: str | None = Query(default=None),
    kind: list[str] | None = Query(default=None),
    subject_type: str | None = Query(default=None),
    subject_id: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[Event]:
    """The activity and audit log. Newest first."""
    rows = await ev.recent(
        sim_run_id=sim_run_id,
        limit=limit,
        kinds=kind,
        subject_type=subject_type,
        subject_id=subject_id,
    )
    return [Event(**r) for r in rows]


@router.get("/events/{event_id}/chain", response_model=list[Event])
async def event_chain(event_id: int, _: StaffPrincipal) -> list[Event]:
    """Why did this happen? The causal chain, oldest cause first.

    Every event records the id of the event that caused it at write time, so
    this is one recursive query rather than an inference over timestamps.
    """
    rows = await ev.chain(event_id)
    if not rows:
        raise NotFound("No such event.")
    return [Event(**r) for r in rows]
