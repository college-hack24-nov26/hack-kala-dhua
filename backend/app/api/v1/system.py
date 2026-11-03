"""System status: what is live, what is cached, and which model is answering."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from app.agents import llm
from app.db.repositories import queries as q
from app.hazards import registry
from app.schemas.domain import FeedStatus, HazardType, LLMStatus, SystemStatus

router = APIRouter(tags=["system"])


@router.get("/status/llm")
async def llm_status() -> dict:
    """Which model is answering, and what the others are doing.

    Separate from `/status` because it is the question asked in a hurry: when an
    explanation reads oddly mid-demo, the first thing worth knowing is whether
    the preferred provider answered it or whether the chain quietly failed over.
    A silent failover is how a team discovers in March that the primary has been
    dead since January.
    """
    from app.agents import llm_cost

    return {
        "engine": llm.current_engine(),
        "note": llm.engine_note(),
        "providers": llm.provider_status(),
        "usage": llm_cost.usage(),
    }


@router.get("/status/llm/usage")
async def llm_usage() -> dict:
    """Where the tokens go: per task, calls vs model calls vs cache, tokens,
    cost where the price is configured, budget and bulkhead state."""
    from app.agents import llm_cost

    return llm_cost.usage()


@router.get("/status/guardrails")
async def guardrail_status() -> dict:
    """Every guardrail rule, how often it has fired, and the latest firings."""
    from app.agents import guardrails

    return guardrails.summary()


@router.get("/status/loop")
async def loop_status() -> dict:
    """Has the event loop been blocked, and where (file:function:line only)? See app.core.loopwatch."""
    from app.core import loopwatch

    return loopwatch.status()


@router.get("/status/router")
async def router_status() -> dict:
    """Is the event router listening, and what has it done? The first question
    when a report lands and nothing re-plans."""
    from app.agents import event_router

    return event_router.status()


@router.get("/status", response_model=SystemStatus)
async def status() -> SystemStatus:
    run = await q.latest_run(HazardType.FLOOD)
    engine = llm.current_engine()

    feeds: list[FeedStatus] = []
    for adapter in registry.all_adapters():
        for source in adapter.sources:
            feeds.append(
                FeedStatus(
                    id=f"{adapter.hazard.value}:{abs(hash(source)) % 10_000}",
                    label=source,
                    # A run that fell back to cache is reported as cached, not live.
                    state="live" if (run and run.get("mode") == "live") else "cached",
                    last_updated=run["started_at"] if run else None,
                    detail=f"{adapter.display_name} adapter ({adapter.maturity.value})",
                )
            )

    return SystemStatus(
        mode="live" if engine != "fallback" and run and run.get("mode") == "live" else "fallback",
        llm=LLMStatus(engine=engine, note=llm.engine_note()),
        feeds=feeds,
        simulated_time=run.get("started_at") if run and run.get("replay_of") else None,
        scenario_id=f"replay-{run['replay_of']}" if run and run.get("replay_of") else None,
    )
