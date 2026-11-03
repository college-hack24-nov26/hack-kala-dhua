"""The Commissioner Copilot API.

Six endpoints, and the shape of them is the argument: everything that *reads* is
open to any staff principal, everything that *acts* is a proposal that goes
through the policy gate, and the endpoint that can move a unit
(`/copilot/apply`) records who asked and then lets the delegation matrix decide
what they may have.

`/copilot/tools` exists so the limits are visible from the same screen as the
capability. A commissioner who can read the catalogue can see that there is no
tool called `evacuate_ward`, and that the ones which change anything all route
through the gate.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import Field

from app.copilot import agent, execute, memory, simulate, strategies as strat, tools
from app.core.errors import BadRequest, NotFound
from app.core.logging import get_logger
from app.core.security import StaffPrincipal
from app.schemas.domain import Camel

log = get_logger(__name__)
router = APIRouter(tags=["copilot"])

# Who may reach any of this is settled by `StaffPrincipal`: ward officers, the
# Commissioner and admins, the same set the rest of the console is behind. There
# is deliberately no second role check for `/copilot/apply`, because a role
# check here would be a worse copy of the delegation matrix — which already
# knows that a ward officer may reallocate a pump and may not requisition NDRF,
# knows it per action rather than per endpoint, and cites the clause.


class AskIn(Camel):
    question: str = Field(min_length=1, max_length=800)
    city_id: str = "pune"
    #: One per browser tab. Lets "cancel it" and "apply the second one" resolve
    #: against what was just discussed. Optional: without it, no memory is used.
    session_id: str | None = Field(default=None, max_length=80)


class RememberIn(Camel):
    content: str = Field(min_length=3, max_length=2000)
    scope: str = "fact"
    ward_id: str | None = None
    city_id: str = "pune"
    importance: int = Field(default=3, ge=1, le=5)


class SimulateIn(Camel):
    """One of the three perturbations, or a combination of them."""

    city_id: str = "pune"
    resource_id: str | None = None
    incident_id: str | None = None
    withdraw: list[str] = Field(default_factory=list)
    ward_id: str | None = None
    capability: str | None = None
    count: int = Field(default=0, ge=0, le=500)


class ApplyIn(Camel):
    city_id: str = "pune"
    strategy_id: str | None = None
    #: Raw actions, for "apply this one thing" rather than a whole strategy.
    actions: list[dict] = Field(default_factory=list)


@router.post("/copilot/ask")
async def copilot_ask(body: AskIn, principal: StaffPrincipal) -> dict:
    """Ask anything. The answer is blocks, not prose with numbers in it."""
    answer = await agent.ask(
        body.question, city_id=body.city_id, session_id=body.session_id,
        actor=principal.full_name or str(principal.role),
    )
    return answer.as_dict()


@router.get("/copilot/commander")
async def commander_recent(_: StaffPrincipal) -> dict:
    """The Incident Commander's recent episodes: trigger, each thought and
    tool call with its result, and how it ended."""
    from app.agents import commander

    return {"episodes": [e.as_dict() for e in commander.RECENT],
            "maxSteps": commander.MAX_STEPS}


class WakeIn(Camel):
    summary: str = Field(min_length=3, max_length=400)
    city_id: str = "pune"


@router.post("/copilot/commander/wake")
async def commander_wake(body: WakeIn, principal: StaffPrincipal) -> dict:
    """Wake the Commander by hand (for a demo, or to ask it to look again)."""
    from app.agents import commander

    ep = await commander.run(
        {"kind": "officer.request", "summary": body.summary,
         "by": principal.full_name or str(principal.role)},
        city_id=body.city_id,
    )
    return ep.as_dict()


@router.get("/copilot/memory")
async def copilot_memory(_: StaffPrincipal, city_id: str = "pune",
                         scope: str | None = None) -> dict:
    """What the Copilot remembers, standing orders first."""
    return {"memory": await memory.list_memory(city_id=city_id, scope=scope),
            "available": memory.available()}


@router.post("/copilot/memory")
async def copilot_remember(body: RememberIn, principal: StaffPrincipal) -> dict:
    if body.scope not in memory.SCOPES:
        raise BadRequest(f"scope must be one of {', '.join(memory.SCOPES)}")
    mid = await memory.remember(
        scope=body.scope, content=body.content, ward_id=body.ward_id,
        created_by=principal.full_name or str(principal.role),
        city_id=body.city_id, importance=body.importance, source="console",
    )
    if mid is None:
        raise BadRequest("Memory is unavailable right now.")
    return {"id": mid}


@router.delete("/copilot/memory/{memory_id}")
async def copilot_forget(memory_id: str, principal: StaffPrincipal) -> dict:
    n = await memory.forget(memory_id=memory_id,
                            actor=principal.full_name or str(principal.role))
    if not n:
        raise NotFound("Nothing active with that id.")
    return {"withdrawn": n}


@router.get("/copilot/tools")
async def copilot_tools(_: StaffPrincipal) -> dict:
    """Everything the Copilot can reach, by tier."""
    return {
        "tools": tools.catalogue(),
        "tiers": {
            "read": "Facts from the database. Changes nothing.",
            "analyse": "Re-solves the real allocation model on a copy, or reads "
                       "the forecaster. Changes nothing.",
            "act": "Proposes. Goes through the policy gate, which decides "
                   "whether a person has to see it first.",
        },
        # Action keys, not tool names: what the system can carry out itself once
        # the gate has authorised it. Everything else needs a person.
        "executable": execute.executable_actions(),
    }


@router.post("/copilot/simulate")
async def copilot_simulate(body: SimulateIn, _: StaffPrincipal) -> dict:
    """What-if, against the live world, writing nothing."""
    if not (body.resource_id or body.withdraw or (body.ward_id and body.count)):
        raise BadRequest(
            "Give me a unit to move, units to hold back, or a ward and a number "
            "of extra calls. Otherwise there is nothing to compare against."
        )
    world = await simulate.inputs(body.city_id)
    pin = (
        {body.resource_id: body.incident_id}
        if body.resource_id and body.incident_id else None
    )
    surge = (
        [(body.ward_id, body.capability or "medical", body.count)]
        if body.ward_id and body.count else []
    )
    now, proposed = await simulate.compare(
        world, label="Proposed", pin=pin, withdraw=body.withdraw, surge=surge
    )
    table = simulate.as_table(now, proposed, world)
    return {
        "comparison": table,
        "baseline": {"assigned": now.assigned, "unmet": now.unmet,
                     "etaMedian": now.eta_median, "coverage": now.coverage},
        "proposed": {"assigned": proposed.assigned, "unmet": proposed.unmet,
                     "etaMedian": proposed.eta_median, "coverage": proposed.coverage,
                     "switches": proposed.switches},
    }


@router.get("/copilot/strategies")
async def copilot_strategies(_: StaffPrincipal, city_id: str = "pune") -> dict:
    """Mitigation options, each priced by the solver, each with its drawbacks."""
    return await strat.generate(city_id)


@router.post("/copilot/apply")
async def copilot_apply(body: ApplyIn, principal: StaffPrincipal) -> dict:
    """Put a strategy, or a single action, to the policy gate.

    Note what this does *not* do: execute. It proposes, the gate rules, and only
    what the gate authorised — and that this system has a mechanism for — is
    carried out. The rest lands on the decision gate with the clause that held
    it, which is the same place every other proposal in the system lands.
    """
    who = principal.full_name or str(principal.role)
    if body.strategy_id:
        result = await agent.apply_strategy(
            body.strategy_id, actor=who, city_id=body.city_id
        )
        if "error" in result:
            raise NotFound(result["error"])
        return result
    if not body.actions:
        raise BadRequest("Nothing to apply.")
    return await agent.apply_actions(
        body.actions, actor=who, city_id=body.city_id
    )


@router.post("/copilot/decisions/{decision_id}/execute")
async def copilot_execute(decision_id: str, principal: StaffPrincipal) -> dict:
    """Carry out a decision a person has approved.

    Separate from approval on purpose: approving records that somebody with the
    delegation agreed, and this is the mechanical consequence. Keeping them
    apart means a decision can be approved by one person and carried out later,
    and that the audit log has both facts rather than one conflated one.
    """
    try:
        return await execute.apply_decision(
            decision_id, actor=principal.full_name or str(principal.role)
        )
    except execute.NotExecutable as exc:
        raise BadRequest(str(exc)) from exc
