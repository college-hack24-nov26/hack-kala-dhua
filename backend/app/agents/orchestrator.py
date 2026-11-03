"""The run.

This is the module that was missing. Everything around it existed already:
hazard adapters that score wards, a CP-SAT solver that assigns units, a policy
gate that decides what may issue without a human, and a schema to hold all of
it. Nothing joined them up, which is why every operational table in the
database was empty. This joins them up.

One run does the whole loop:

    signal -> score -> impact -> proposed actions -> policy gate -> decisions
           -> demands -> travel matrix -> allocation -> assignments
           -> field tasks -> alerts

and appends an event for every step, with the id of the event that caused it,
so the console can answer "why did that boat get sent there?" by walking a
chain rather than guessing.

Two rules hold throughout, and they are the reason this is defensible:

  * The model never produces a number. Risk comes from adapters, allocation
    from CP-SAT, authority from the policy corpus. The LLM writes prose. Pull
    the model out entirely and the run still completes; it just explains itself
    in plainer language and says `engine: fallback`.

  * Nothing reads the wall clock. Time comes from the injected `Clock`, so this
    same function is what a simulation and a replay run. There is no separate
    demo path to drift out of sync with production.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.agents import policy
from app.core.logging import get_logger
from app.db import session as db
from app.db.repositories import queries as q
from app.hazards import registry
from app.hazards.base import HazardAdapter, HazardImpact, ProposedAction, ScoredWard, WardContext
from app.schemas.domain import DecisionStatus, RunResult
from app.solver import routing
from app.solver.allocation import Demand, Unit, allocate
from app.taxonomy import cache as taxonomy
from app.world import events as ev
from app.world.clock import WALL, Clock

log = get_logger(__name__)

#: Wards below this severity are scored and stored, but propose no actions and
#: raise no alerts. Recording them matters: "we looked and it was fine" is an
#: auditable fact, and it is what the projection charts are drawn from.
ACTIONABLE_SEVERITY = 4


@dataclass(slots=True)
class StepRecorder:
    """Collects the agent trace as the run proceeds.

    Written in one batch at the end rather than row by row, so a slow database
    never sits in the middle of a dispatch decision.
    """

    agent_run_id: str
    steps: list[dict[str, Any]] = field(default_factory=list)

    def add(
        self,
        agent: str,
        thought: str,
        *,
        duration_ms: int = 0,
        tool: str | None = None,
        tool_input: str | None = None,
        tool_output: str | None = None,
        cited_clause: str | None = None,
        status: str = "ok",
    ) -> None:
        self.steps.append(
            {
                "seq": len(self.steps) + 1,
                "agent": agent,
                "thought": thought,
                "duration_ms": duration_ms,
                "tool": tool,
                "tool_input": tool_input,
                "tool_output": tool_output,
                "cited_clause": cited_clause,
                "status": status,
            }
        )

    async def flush(self, conn: Any) -> None:
        if not self.steps:
            return
        await conn.executemany(
            """
            insert into agent_steps
              (agent_run_id, seq, agent, thought, duration_ms, tool, tool_input,
               tool_output, cited_clause, status)
            values ($1::uuid,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            """,
            [
                (
                    self.agent_run_id,
                    s["seq"],
                    s["agent"],
                    s["thought"],
                    s["duration_ms"],
                    s["tool"],
                    s["tool_input"],
                    s["tool_output"],
                    s["cited_clause"],
                    s["status"],
                )
                for s in self.steps
            ],
        )


# ---------------------------------------------------------------- the run ---
async def run_hazard(
    hazard_id: str,
    *,
    city_id: str = "pune",
    clock: Clock = WALL,
    actor: str = "system",
) -> RunResult:
    """Score a hazard across a city and produce a dispatchable plan.

    Never raises on upstream failure. A dead feed degrades the run and says so;
    a missing solver degrades to greedy and says so; a missing model degrades to
    deterministic prose and says so. A disaster response system that stops
    working when a dependency is down is not a disaster response system.
    """
    started_wall = time.perf_counter()
    adapter = registry.get(hazard_id)
    hazard_ref = taxonomy.hazard(hazard_id)

    wards = await q.ward_contexts(hazard_id, city_id)
    if not wards:
        raise LookupError(f"No wards configured for city {city_id!r}.")

    # ---------------------------------------------------------- 1. signal --
    t0 = time.perf_counter()
    signals = await adapter.fetch_signal(wards)
    live = all(s.live for s in signals.values()) if signals else False
    mode = "live" if live else "fallback"
    signal_ms = int((time.perf_counter() - t0) * 1000)

    async with db.transaction() as conn:
        run_row = await conn.fetchrow(
            """
            insert into hazard_runs (hazard, sources, mode, sim_run_id, started_at)
            values ($1, $2, $3, $4::uuid, $5)
            returning id::text
            """,
            hazard_id,
            list(adapter.sources),
            mode,
            clock.sim_run_id,
            clock.now(),
        )
        run_id = run_row["id"]

        agent_row = await conn.fetchrow(
            """
            insert into agent_runs (run_id, hazard, trigger, engine, sim_run_id, started_at)
            values ($1::uuid, $2, $3, $4, $5::uuid, $6)
            returning id::text
            """,
            run_id,
            hazard_id,
            f"{hazard_ref.display_name} run requested by {actor}",
            "fallback",
            clock.sim_run_id,
            clock.now(),
        )
        agent_run_id = agent_row["id"]

        root = await ev.append(
            clock=clock,
            kind=ev.Kind.RUN_STARTED,
            actor=actor,
            subject_type="run",
            subject_id=run_id,
            city_id=city_id,
            payload={"hazard": hazard_id, "mode": mode, "wards": len(wards)},
            conn=conn,
        )

        trace = StepRecorder(agent_run_id)
        trace.add(
            "hazard_analyst",
            (
                f"Pulled {len(adapter.sources)} upstream sources for "
                f"{hazard_ref.display_name.lower()} across {len(wards)} wards. "
                + (
                    "All feeds answered live."
                    if live
                    else "At least one feed did not answer; scoring from cache and "
                    "marking the run degraded rather than cancelling it."
                )
            ),
            duration_ms=signal_ms,
            tool="ingest.fetch_signal",
            tool_input=json.dumps({"wards": len(wards), "hazard": hazard_id}),
            tool_output=json.dumps({"live": live, "signals": len(signals)}),
            status="ok" if live else "fallback",
        )
        if not live:
            await ev.append(
                clock=clock, kind=ev.Kind.FEED_DEGRADED, actor=ev.feed(hazard_id),
                subject_type="run", subject_id=run_id, city_id=city_id,
                payload={"hazard": hazard_id}, caused_by=root.id, conn=conn,
            )

        # ------------------------------------------- 2. score and 3. impact --
        t0 = time.perf_counter()
        scored: list[tuple[ScoredWard, HazardImpact, WardContext]] = []
        for ward in wards:
            signal = signals.get(ward.ward_id)
            if signal is None:
                continue
            s = adapter.score(signal, ward)
            scored.append((s, adapter.impact(s, ward), ward))
        scored, fused = await _fuse_field_sensors(hazard_id, city_id, adapter, scored)
        scored.sort(key=lambda t: t[0].score, reverse=True)
        score_ms = int((time.perf_counter() - t0) * 1000)
        if fused:
            trace.add(
                "hazard_analyst",
                f"Field sensors raised {len(fused)} ward score(s): "
                + "; ".join(f"{w} +{d:.0%}" for w, d, _ in fused[:4]),
                tool="iot.ward_field",
                tool_output=json.dumps([{"ward": w, "lift": round(d, 3), "why": why}
                                        for w, d, why in fused[:10]]),
            )

        await conn.executemany(
            """
            insert into ward_risks
              (run_id, ward_id, hazard, score, severity, lead_time_hours,
               confidence, population_at_risk, drivers, projection)
            values ($1::uuid,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            """,
            [
                (
                    run_id,
                    s.ward_id,
                    hazard_id,
                    round(s.score, 4),
                    s.severity,
                    s.lead_time_hours,
                    round(s.confidence, 4),
                    impact.population_at_risk,
                    [d.model_dump() for d in s.drivers],
                    s.projection,
                )
                for s, impact, _ward in scored
            ],
        )

        at_risk = [t for t in scored if t[0].severity >= ACTIONABLE_SEVERITY]
        top = scored[0] if scored else None
        trace.add(
            "impact_exposure",
            (
                f"Scored {len(scored)} wards. {len(at_risk)} at severity "
                f"{ACTIONABLE_SEVERITY} or above."
                + (
                    f" Worst is {top[2].name} at {top[0].score:.2f}, "
                    f"{top[1].population_at_risk:,} residents exposed, "
                    f"{top[0].lead_time_hours:g} h lead time."
                    if top
                    else ""
                )
            ),
            duration_ms=score_ms,
            tool="hazards.score",
            tool_output=json.dumps(
                {"scored": len(scored), "actionable": len(at_risk)}
            ),
        )
        await ev.append(
            clock=clock, kind=ev.Kind.RISK_UPDATED, actor=ev.agent("hazard_analyst"),
            subject_type="run", subject_id=run_id, city_id=city_id,
            payload={
                "hazard": hazard_id,
                "scored": len(scored),
                "actionable": len(at_risk),
                "top_ward": top[2].ward_id if top else None,
                "top_score": round(top[0].score, 3) if top else None,
            },
            caused_by=root.id, conn=conn,
        )

        # ----------------------------------------- 4. actions + policy gate --
        proposals: list[tuple[ProposedAction, WardContext, HazardImpact]] = []
        for s, impact, ward in at_risk:
            for action in adapter.action_policy(s, impact, ward):
                proposals.append((action, ward, impact))

        decisions: list[dict[str, Any]] = []
        auto = awaiting = 0
        for action, ward, impact in proposals:
            authority = await policy.authority_for(action.action_key, action.severity)
            status = policy.gate(authority, action.confidence)
            reason = policy.gate_reason(authority, action.confidence)
            auto += status is DecisionStatus.AUTO_ISSUED
            awaiting += status is DecisionStatus.AWAITING_APPROVAL

            row = await conn.fetchrow(
                """
                insert into decisions
                  (agent_run_id, hazard, action_key, action, target, ward_id,
                   rationale, confidence, authority, status, sim_run_id, created_at)
                values ($1::uuid,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::uuid,$12)
                returning id::text
                """,
                agent_run_id, hazard_id, action.action_key, action.action,
                action.target, action.ward_id,
                f"{action.rationale} {reason}",
                round(action.confidence, 4),
                authority.model_dump(),
                status.value, clock.sim_run_id, clock.now(),
            )
            decisions.append(
                {
                    "id": row["id"],
                    "action": action,
                    "ward": ward,
                    "impact": impact,
                    "status": status,
                    "authority": authority,
                }
            )
            trace.add(
                "policy_retriever",
                reason,
                tool="policy.authority_for",
                tool_input=json.dumps(
                    {"action_key": action.action_key, "severity": action.severity}
                ),
                tool_output=json.dumps(
                    {"within_delegation": authority.within_delegation,
                     "delegated_to": authority.delegated_to}
                ),
                cited_clause=authority.clause,
                status="ok" if authority.within_delegation else "fallback",
            )
            await ev.append(
                clock=clock, kind=ev.Kind.DECISION_GATED, actor=ev.agent("policy_retriever"),
                subject_type="decision", subject_id=row["id"], city_id=city_id,
                ward_id=action.ward_id,
                payload={
                    "action_key": action.action_key,
                    "status": status.value,
                    "clause": authority.clause,
                    "within_delegation": authority.within_delegation,
                    "reason": reason,
                },
                caused_by=root.id, conn=conn,
            )

        # ------------------------------------------------- 5. allocation ----
        dispatchable = [
            d for d in decisions
            if d["status"] is DecisionStatus.AUTO_ISSUED and d["action"].resource_need
        ]
        demands = _demands_for(dispatchable)
        units = await _available_units(city_id)

        plan_id: str | None = None
        n_assign = n_uncovered = n_tasks = 0
        alloc = None
        if demands and units:
            t0 = time.perf_counter()
            matrix = await routing.travel_matrix(
                [u.location for u in units], [d.location for d in demands]
            )
            alloc = allocate(demands, units, matrix)
            alloc_ms = int((time.perf_counter() - t0) * 1000)

            plan_row = await conn.fetchrow(
                """
                insert into allocation_plans
                  (run_id, hazard, objective, solver, uncovered, sim_run_id, generated_at)
                values ($1::uuid,$2,$3,$4,$5,$6::uuid,$7)
                returning id::text
                """,
                run_id, hazard_id, alloc.objective_text,
                {
                    "engine": alloc.engine,
                    "runtime_ms": alloc.runtime_ms,
                    "variables": alloc.variables,
                    "constraints": alloc.constraints,
                    "coverage": round(alloc.coverage, 4),
                },
                [
                    {
                        "ward_id": u.demand.ward_id,
                        "incident_id": u.demand.incident_id,
                        "need": u.demand.capability,
                        "reason": u.reason,
                        "shortfall": u.shortfall,
                    }
                    for u in alloc.unmet
                ],
                clock.sim_run_id, clock.now(),
            )
            plan_id = plan_row["id"]
            n_uncovered = len(alloc.unmet)

            plan_event = await ev.append(
                clock=clock, kind=ev.Kind.PLAN_GENERATED, actor=ev.agent("allocation_planner"),
                subject_type="plan", subject_id=plan_id, city_id=city_id,
                payload={
                    "engine": alloc.engine,
                    "assignments": len(alloc.allocations),
                    "uncovered": n_uncovered,
                    "coverage": round(alloc.coverage, 3),
                    "runtime_ms": alloc.runtime_ms,
                },
                caused_by=root.id, conn=conn,
            )

            for a in alloc.allocations:
                arow = await conn.fetchrow(
                    """
                    insert into assignments
                      (plan_id, resource_id, incident_id, ward_id, purpose,
                       eta_minutes, distance_km, status, sim_run_id, created_at)
                    values ($1::uuid,$2,$3::uuid,$4,$5,$6,$7,'proposed',$8::uuid,$9)
                    returning id::text
                    """,
                    plan_id, a.unit.id, a.demand.incident_id, a.demand.ward_id,
                    a.demand.purpose, a.eta_minutes, round(a.distance_km, 2),
                    clock.sim_run_id, clock.now(),
                )
                await conn.execute(
                    "update resources set status = 'assigned', updated_at = $2 where id = $1",
                    a.unit.id, clock.now(),
                )
                task = await conn.fetchrow(
                    """
                    insert into field_tasks
                      (assignment_id, resource_id, operator, title, instruction,
                       location, ward_id, priority, sim_run_id, created_at)
                    values ($1::uuid,$2,$3,$4,$5,
                            extensions.ST_SetSRID(extensions.ST_MakePoint($6,$7),4326)::extensions.geography,
                            $8,$9,$10::uuid,$11)
                    returning id::text
                    """,
                    arow["id"], a.unit.id, a.unit.operator,
                    a.demand.purpose,
                    _instruction(a.demand.purpose, a.unit.label, a.eta_minutes),
                    a.demand.location[0], a.demand.location[1],
                    a.demand.ward_id, a.demand.severity,
                    clock.sim_run_id, clock.now(),
                )
                n_assign += 1
                n_tasks += 1
                await ev.append(
                    clock=clock, kind=ev.Kind.ASSIGNMENT_CREATED,
                    actor=ev.agent("allocation_planner"),
                    subject_type="assignment", subject_id=arow["id"], city_id=city_id,
                    ward_id=a.demand.ward_id,
                    payload={
                        "resource_id": a.unit.id,
                        "resource_kind": a.unit.kind,
                        "capability": a.demand.capability,
                        "eta_minutes": a.eta_minutes,
                        "distance_km": round(a.distance_km, 2),
                        "task_id": task["id"],
                    },
                    caused_by=plan_event.id, conn=conn,
                )

            for u in alloc.unmet:
                await ev.append(
                    clock=clock, kind=ev.Kind.DEMAND_UNCOVERED,
                    actor=ev.agent("allocation_planner"),
                    subject_type="ward", subject_id=u.demand.ward_id, city_id=city_id,
                    ward_id=u.demand.ward_id,
                    payload={"capability": u.demand.capability, "reason": u.reason},
                    caused_by=plan_event.id, conn=conn,
                )

            trace.add(
                "allocation_planner",
                (
                    f"{len(demands)} demands against {len(units)} available units. "
                    f"{alloc.engine} covered {alloc.coverage:.0%} in {alloc.runtime_ms} ms"
                    + (
                        f"; {n_uncovered} demand(s) could not be met and the reason is "
                        "recorded against the ward."
                        if n_uncovered
                        else "."
                    )
                ),
                duration_ms=alloc_ms,
                tool="solver.allocate",
                tool_input=json.dumps(
                    {"demands": len(demands), "units": len(units), "engine": matrix.engine}
                ),
                tool_output=json.dumps(
                    {"assigned": n_assign, "uncovered": n_uncovered,
                     "coverage": round(alloc.coverage, 3)}
                ),
                status="ok" if alloc.engine == "cp-sat" else "fallback",
            )

        # ---------------------------------------------------- 6. alerts -----
        n_alerts = await _issue_alerts(
            conn, decisions, hazard_id, city_id, clock, root.id, trace
        )

        # ------------------------------------------------------ 7. close ----
        await trace.flush(conn)
        summary = _summarise(
            hazard_ref.display_name, scored, at_risk, auto, awaiting, n_assign, n_uncovered
        )
        await conn.execute(
            "update agent_runs set finished_at = $2, summary = $3 where id = $1::uuid",
            agent_run_id, clock.now(), summary,
        )
        await conn.execute(
            "update hazard_runs set finished_at = $2 where id = $1::uuid", run_id, clock.now()
        )
        await ev.append(
            clock=clock, kind=ev.Kind.RUN_FINISHED, actor=actor,
            subject_type="run", subject_id=run_id, city_id=city_id,
            payload={
                "decisions": len(decisions), "auto_issued": auto,
                "awaiting_approval": awaiting, "assignments": n_assign,
                "uncovered": n_uncovered, "alerts": n_alerts,
            },
            caused_by=root.id, conn=conn,
        )
        n_events = await conn.fetchval(
            "select count(*) from events where causation_id = $1 or id = $1", root.id
        )

    duration_ms = int((time.perf_counter() - started_wall) * 1000)
    log.info(
        "hazard_run_complete",
        hazard=hazard_id, run_id=run_id, mode=mode, wards=len(scored),
        decisions=len(decisions), assignments=n_assign, duration_ms=duration_ms,
    )
    return RunResult(
        run_id=run_id,
        agent_run_id=agent_run_id,
        hazard=hazard_id,
        engine=(alloc.engine if alloc else "none"),
        mode=mode,
        wards_scored=len(scored),
        decisions=len(decisions),
        auto_issued=auto,
        awaiting_approval=awaiting,
        assignments=n_assign,
        uncovered=n_uncovered,
        alerts=n_alerts,
        field_tasks=n_tasks,
        events=int(n_events or 0),
        duration_ms=duration_ms,
    )


# ----------------------------------------------------------------- helpers --
async def _fuse_field_sensors(
    hazard_id: str, city_id: str, adapter: HazardAdapter,
    scored: list[tuple[ScoredWard, HazardImpact, WardContext]],
) -> tuple[list[tuple[ScoredWard, HazardImpact, WardContext]], list[tuple[str, float, str]]]:
    """Ground truth from the LoRa field (real and simulated nodes) lifts the
    modelled score of the ward the node sits in. Only upward, never past 1, and
    only for hazards a sensor can actually speak to (see iot.HAZARD_SENSOR).
    The lift is written as a risk driver, so it shows wherever drivers do."""
    from dataclasses import replace

    from app.iot import service as iot
    from app.schemas.domain import RiskDriver

    try:
        field_by_ward = await iot.ward_field(city_id)
    except Exception as exc:  # noqa: BLE001 - sensors are evidence, never a dependency
        log.warning("sensor_fusion_skipped", error=str(exc)[:160])
        return scored, []
    if not field_by_ward:
        return scored, []
    out, fused = [], []
    for s, impact, ward in scored:
        lift = iot.hazard_lift(hazard_id, field_by_ward.get(ward.ward_id))
        if lift:
            add, why = lift
            old = s.score
            new = min(1.0, old + (1.0 - old) * add)
            if new > old + 0.005:
                s = replace(
                    s, score=new,
                    projection=[max(p, new * (0.92 ** i)) for i, p in enumerate(s.projection)],
                    drivers=[*s.drivers, RiskDriver(label="Field sensors",
                                                    contribution=min(1.0, add), detail=why[:200])],
                )
                impact = adapter.impact(s, ward)
                fused.append((ward.ward_id, new - old, why))
        out.append((s, impact, ward))
    return out, fused


def _demands_for(dispatchable: Sequence[dict[str, Any]]) -> list[Demand]:
    """Expand each auto-issued action's capability needs into unit demands."""
    demands: list[Demand] = []
    for d in dispatchable:
        action: ProposedAction = d["action"]
        ward: WardContext = d["ward"]
        impact: HazardImpact = d["impact"]
        for capability, count in action.resource_need.items():
            for i in range(int(count)):
                demands.append(
                    Demand(
                        id=f"{d['id']}:{capability}:{i}",
                        ward_id=ward.ward_id,
                        incident_id=None,
                        capability=capability,
                        purpose=action.action,
                        location=ward.centroid,
                        severity=action.severity,
                        population_at_risk=impact.population_at_risk,
                    )
                )
    return demands


async def _available_units(city_id: str) -> list[Unit]:
    rows = await db.fetch(
        """
        select id, kind, label, operator, capacity,
               extensions.ST_AsGeoJSON(location)::json -> 'coordinates' as loc
          from resources
         where city_id = $1 and status = 'available'
         order by kind, label
        """,
        city_id,
    )
    units: list[Unit] = []
    for r in rows:
        loc = r["loc"]
        if isinstance(loc, str):
            loc = json.loads(loc)
        units.append(
            Unit(
                id=r["id"], kind=r["kind"], label=r["label"], operator=r["operator"],
                location=(float(loc[0]), float(loc[1])), capacity=r["capacity"],
            )
        )
    return units


def _instruction(purpose: str, unit_label: str, eta: int) -> str:
    """What the field operator actually reads on their phone.

    Deliberately imperative and short. A task that needs interpreting under
    pressure is a task that gets done wrong.
    """
    return (
        f"{purpose}. {unit_label} is the assigned unit, {eta} minutes out. "
        "Confirm on arrival and record what you found before closing the task."
    )


async def _issue_alerts(
    conn: Any,
    decisions: Sequence[dict[str, Any]],
    hazard_id: str,
    city_id: str,
    clock: Clock,
    caused_by: int,
    trace: StepRecorder,
) -> int:
    """Public alerts, only for decisions that actually issued.

    An alert is a consequence of an authorised decision, never a side effect of
    a high score. If the clause reserved the action to the Commissioner and she
    has not acted, nobody's phone buzzes.
    """
    issued = 0
    for d in decisions:
        if d["status"] is not DecisionStatus.AUTO_ISSUED:
            continue
        if not d["action"].action_key.startswith(("issue_warning", "issue_advisory")):
            continue
        ward: WardContext = d["ward"]
        action: ProposedAction = d["action"]
        impact: HazardImpact = d["impact"]

        shelter = await conn.fetchrow(
            """
            select name,
                   extensions.ST_AsGeoJSON(location)::json -> 'coordinates' as loc,
                   extensions.ST_Distance(
                     location,
                     extensions.ST_SetSRID(extensions.ST_MakePoint($1,$2),4326)::extensions.geography
                   ) / 1000.0 as km
              from lifelines
             where kind = 'shelter' and coalesce(occupancy,0) < coalesce(capacity,0)
             order by km asc limit 1
            """,
            ward.centroid[0], ward.centroid[1],
        )
        safe = None
        if shelter:
            loc = shelter["loc"]
            if isinstance(loc, str):
                loc = json.loads(loc)
            safe = {
                "name": shelter["name"],
                "location": [float(loc[0]), float(loc[1])],
                "distance_km": round(float(shelter["km"]), 2),
            }

        row = await conn.fetchrow(
            """
            insert into alerts
              (decision_id, ward_id, hazard, severity, headline, action, by_time,
               safe_location, channels, language, reach, issued_at)
            values ($1::uuid,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            returning id::text
            """,
            d["id"], ward.ward_id, hazard_id, action.severity,
            f"{action.action} for {ward.name}",
            _citizen_action(action, safe),
            clock.now(),
            safe,
            ["push", "sms"],
            "English",
            impact.population_at_risk,
            clock.now(),
        )
        issued += 1
        await ev.append(
            clock=clock, kind=ev.Kind.ALERT_ISSUED, actor=ev.agent("guidance_agent"),
            subject_type="alert", subject_id=row["id"], city_id=city_id,
            ward_id=ward.ward_id,
            payload={"severity": action.severity, "reach": impact.population_at_risk,
                     "decision_id": d["id"]},
            caused_by=caused_by, conn=conn,
        )
    if issued:
        trace.add(
            "guidance_agent",
            f"Composed {issued} public alert(s), each tied to a decision that was "
            "authorised to issue. Wards where the action is still awaiting an "
            "officer were deliberately left silent.",
            tool="alerts.compose",
            tool_output=json.dumps({"issued": issued}),
        )
    return issued


def _citizen_action(action: ProposedAction, safe: dict | None) -> str:
    """Plain instruction for a resident. Deterministic; the model may rephrase
    it later, but it is never the thing that decides what to say."""
    if safe:
        return (
            f"Move to {safe['name']}, {safe['distance_km']} km away, before water "
            "reaches the road. Take medicines and identity documents."
        )
    return (
        "Stay off low-lying roads and move to the highest floor available. "
        "Keep your phone charged."
    )


def _summarise(
    hazard_name: str,
    scored: Sequence[tuple[ScoredWard, HazardImpact, WardContext]],
    at_risk: Sequence[tuple[ScoredWard, HazardImpact, WardContext]],
    auto: int,
    awaiting: int,
    assignments: int,
    uncovered: int,
) -> str:
    """The one-paragraph summary an officer reads first.

    Deterministic on purpose. This is the fallback the LLM improves on, not the
    other way round: if the model is unavailable this still says something true
    and specific rather than apologising.
    """
    if not scored:
        return f"{hazard_name} run completed with no wards scored."
    worst = at_risk[0] if at_risk else scored[0]
    exposed = sum(i.population_at_risk for _s, i, _w in at_risk)
    parts = [
        f"{hazard_name}: {len(at_risk)} of {len(scored)} wards at severity 4 or above, "
        f"{exposed:,} residents exposed.",
        f"Worst is {worst[2].name} at {worst[0].score:.2f} with "
        f"{worst[0].lead_time_hours:g} h lead time.",
    ]
    if auto or awaiting:
        parts.append(
            f"{auto} action(s) issued under delegation, {awaiting} held for an officer."
        )
    if assignments:
        parts.append(f"{assignments} unit(s) tasked.")
    if uncovered:
        parts.append(
            f"{uncovered} demand(s) unmet; the shortfall is recorded against the ward "
            "rather than hidden."
        )
    return " ".join(parts)
