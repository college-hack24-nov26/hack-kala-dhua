"""Re-allocation, when the world changes.

The hazard run produces a plan once. This produces the next one, and the
difference between those two jobs is the whole of PS20's "dynamic re-allocation
on new reports".

Re-planning naively is worse than not re-planning. Solve from scratch on every
new report and the entire city reshuffles every few seconds: units that were
two minutes from a rescue turn around because some arithmetic found a marginally
better global arrangement, and the field stops trusting the screen. So this
does three things a fresh solve does not:

  * it tells the solver what every unit is **already committed to**, and how far
    along it is, so moving one costs something and moving one that is nearly
    there costs a lot;
  * it returns a **diff**, not a plan. Kept, reassigned, newly assigned,
    released, uncovered. The console shows what changed and why, which is the
    only form an officer can actually check;
  * it is **debounced**. Ten reports arriving in twenty seconds produce one
    re-plan, not ten.

Demand comes from incident needs rather than from reports, because four reports
of one flooded road need one pump between them.
"""

from __future__ import annotations

import asyncio

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from app.core.logging import get_logger
from app.db import session as db
from app.solver import routing
from app.solver.allocation import MAX_ETA_MINUTES, Demand, Unit, allocate
from app.taxonomy import cache as taxonomy
from app.world import events as ev
from app.world.clock import WALL, Clock

log = get_logger(__name__)

#: Statuses that mean a unit is committed but has not finished.
ACTIVE = ("proposed", "approved", "en_route", "on_site")
#: An assignment younger than this is treated as at least half-way done when
#: pricing a switch. Without it a unit tasked on one re-plan was nearly free to
#: re-task on the next, six seconds later, and the map showed trucks turning
#: round in the street for no reason anyone could see.
MIN_DWELL_SECONDS = 60
DWELL_PROGRESS_FLOOR = 0.5


def map_commitments(
    units: Sequence[Unit],
    current_raw: dict[str, str],
    demands: Sequence[Demand],
    progress: dict[str, float],
) -> dict[str, str]:
    """Which demand each committed unit is already serving.

    The first version mapped every unit on an incident to that incident's
    *first* demand id. Two faults, both of which made plans thrash:

      * capability-blind: a pump on a flooded road that also needs an ambulance
        was "committed" to the ambulance demand, which it cannot serve, so
        staying put was priced as a switch and the solver happily moved it;
      * shared: two units on one incident were both mapped to the same demand,
        only one could keep it, and the other paid a switching cost for
        staying exactly where it was.

    Now each committed unit gets its own demand of a capability it can provide,
    most-progressed unit first, so the unit nearest to finishing keeps its job.
    """
    by_incident: dict[str, list[Demand]] = {}
    for d in demands:
        if d.incident_id:
            by_incident.setdefault(d.incident_id, []).append(d)
    kinds = {u.id: u.kind for u in units}
    taken: set[str] = set()
    out: dict[str, str] = {}
    for unit_id in sorted(current_raw, key=lambda u: -progress.get(u, 0.0)):
        options = by_incident.get(current_raw[unit_id]) or []
        kind = kinds.get(unit_id, "")
        for d in options:
            if d.id in taken:
                continue
            if taxonomy.kind_can(kind, d.capability):
                out[unit_id] = d.id
                taken.add(d.id)
                break
    return out


async def load_overrides(conn: Any, city_id: str) -> dict[str, Any]:
    """Standing instructions from a person that the planner must respect.

    `pin`    — keep this unit on its current job; the planner may not move it.
    `hold`   — this unit is out of the plan (resting, reserved, broken down).
    `forbid` — this unit must not be sent to this incident (it was cancelled
               there, and the officer said why).

    Read defensively: if migration 021 has not been applied the table does not
    exist and the planner behaves exactly as it did before, rather than failing.
    """
    empty = {"pin": set(), "hold": set(), "forbid": set()}
    try:
        rows = await conn.fetch(
            """
            select kind, resource_id, incident_id::text incident_id
              from operator_overrides
             where city_id = $1 and active
               and (expires_at is null or expires_at > now())
            """,
            city_id,
        )
    except Exception as exc:  # noqa: BLE001 - absent table must not stop planning
        log.info("overrides_unavailable", error=type(exc).__name__)
        return empty
    for r in rows:
        if r["kind"] == "forbid" and r["incident_id"]:
            empty["forbid"].add((r["resource_id"], r["incident_id"]))
        elif r["kind"] in ("pin", "hold"):
            empty[r["kind"]].add(r["resource_id"])
    return empty


_TASK_CANCEL_SUPPORTED: bool | None = None


async def close_tasks(conn: Any, assignment_id: str | None, why: str) -> None:
    """Take a stood-down job off the crew's phone.

    Re-tasking and releasing used to close the assignment and leave its field
    task open, so the crew app kept showing a job the plan had taken away and a
    crew could drive to it. Uses `cancelled` when migration 021 added it to
    `task_status`, `complete` with the reason otherwise.
    """
    global _TASK_CANCEL_SUPPORTED
    if not assignment_id:
        return
    if _TASK_CANCEL_SUPPORTED is None:
        _TASK_CANCEL_SUPPORTED = bool(await conn.fetchval(
            """
            select 1 from pg_enum e join pg_type t on t.oid = e.enumtypid
             where t.typname = 'task_status' and e.enumlabel = 'cancelled'
            """
        ))
    status = "cancelled" if _TASK_CANCEL_SUPPORTED else "complete"
    await conn.execute(
        f"""
        update field_tasks
           set status = '{status}',
               proof_note = coalesce(proof_note, $2),
               completed_at = coalesce(completed_at, now())
         where assignment_id = $1::uuid
           and status::text not in ('complete', 'cancelled')
        """,
        assignment_id, why[:400],
    )


@dataclass(slots=True)
class Change:
    kind: str            # kept | reassigned | assigned | released
    resource_id: str
    resource_label: str
    incident_id: str | None
    incident_title: str
    ward_id: str
    from_incident_id: str | None = None
    from_incident_title: str = ""
    eta_minutes: int = 0
    reason: str = ""


@dataclass(slots=True)
class PlanDiff:
    plan_id: str | None
    engine: str
    runtime_ms: int
    coverage: float
    kept: list[Change] = field(default_factory=list)
    assigned: list[Change] = field(default_factory=list)
    reassigned: list[Change] = field(default_factory=list)
    released: list[Change] = field(default_factory=list)
    uncovered: list[dict[str, Any]] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return len(self.assigned) + len(self.reassigned) + len(self.released)

    @property
    def headline(self) -> str:
        if not self.changed:
            return (
                f"No change. {len(self.kept)} unit(s) stay where they are; the new "
                "information did not outweigh what they are already doing."
            )
        bits = []
        if self.assigned:
            bits.append(f"{len(self.assigned)} newly tasked")
        if self.reassigned:
            bits.append(f"{len(self.reassigned)} re-tasked")
        if self.released:
            bits.append(f"{len(self.released)} released")
        tail = f", {len(self.uncovered)} need(s) still unmet" if self.uncovered else ""
        return f"{', '.join(bits)}; {len(self.kept)} unchanged{tail}."


_CAP_COL: bool | None = None


async def _has_capability_column(conn: Any) -> bool:
    """assignments.capability_id exists once migration 032 is applied."""
    global _CAP_COL
    if _CAP_COL is None:
        _CAP_COL = bool(await conn.fetchval(
            "select exists (select 1 from information_schema.columns "
            "where table_schema='public' and table_name='assignments' and column_name='capability_id')"))
    return _CAP_COL


# ------------------------------------------------------------------ inputs ---
async def _open_demands(conn: Any, city_id: str, sim_run_id: str | None) -> list[Demand]:
    """One demand per unit of unmet capability, per open incident."""
    rows = await conn.fetch(
        """
        select i.id::text as incident_id, i.title, i.ward_id, i.severity, i.category, i.confidence,
               n.capability_id, n.required, w.elderly_share,
               extensions.ST_X(i.location::extensions.geometry) as lng,
               extensions.ST_Y(i.location::extensions.geometry) as lat,
               coalesce(wr.population_at_risk, w.population / 8) as exposed
          from incidents i
          join incident_needs n on n.incident_id = i.id
          join wards w on w.id = i.ward_id
          left join lateral (
            select population_at_risk from ward_risks
             where ward_id = i.ward_id order by created_at desc limit 1
          ) wr on true
         where i.city_id = $1
           and i.sim_run_id is not distinct from $2::uuid
           and i.status <> 'resolved'
           and n.required > 0
         order by i.severity desc, i.updated_at desc
        """,
        city_id, sim_run_id,
    )
    demands: list[Demand] = []
    from app.incidents import fusion
    from app.surge import service as surge
    from app.taxonomy import cache as taxonomy
    for r in rows:
        # Verify first: below the dispatch floor only the assessment need is planned (an assessor or a drone
        # looks); the rest waits until the fused belief clears the floor (app/incidents/fusion.py).
        ref = taxonomy.categories.get(r["category"])
        if (r["confidence"] is not None and r["capability_id"] != "field_assessment"
                and not fusion.gate(float(r["confidence"]), bool(ref and ref.life_safety))):
            continue
        prio = surge.triage_factor(r["ward_id"], r["capability_id"],
                                   float(r["elderly_share"]) if r["elderly_share"] is not None else None)
        for k in range(int(r["required"])):
            demands.append(
                Demand(
                    id=f"{r['incident_id']}:{r['capability_id']}:{k}",
                    ward_id=r["ward_id"],
                    incident_id=r["incident_id"],
                    capability=r["capability_id"],
                    purpose=r["title"],
                    location=(float(r["lng"]), float(r["lat"])),
                    severity=int(r["severity"]),
                    population_at_risk=int(r["exposed"] or 0),
                    priority=prio,
                )
            )
    return demands


async def _fleet(
    conn: Any, city_id: str, sim_run_id: str | None, now: datetime
) -> tuple[list[Unit], dict[str, str], dict[str, float], dict[str, dict]]:
    """Every unit that could be used, plus what it is doing and how far along.

    Units that are already committed are included rather than excluded. That is
    the point: the solver has to be able to move one, and to know what moving it
    would cost.
    """
    rows = await conn.fetch(
        """
        select r.id, r.kind, r.label, r.operator, r.capacity, r.status,
               extensions.ST_X(r.location::extensions.geometry) as lng,
               extensions.ST_Y(r.location::extensions.geometry) as lat,
               a.id::text          as assignment_id,
               a.incident_id::text as incident_id,
               a.eta_minutes,
               a.created_at,
               a.progress,
               a.status::text      as assignment_status,
               -- The road this unit is already driving. Needed to answer the
               -- question nothing was asking: is the path we gave it still
               -- passable?
               extensions.ST_AsGeoJSON(a.route)::json -> 'coordinates' as route,
               i.title             as incident_title
          from resources r
          left join lateral (
            select * from assignments x
             where x.resource_id = r.id
               and x.status = any($3::assignment_status[])
               and x.sim_run_id is not distinct from $2::uuid
             order by x.created_at desc limit 1
          ) a on true
          left join incidents i on i.id = a.incident_id
         where r.city_id = $1
           and r.status <> 'offline'
         order by r.kind, r.label
        """,
        city_id, sim_run_id, list(ACTIVE),
    )

    units: list[Unit] = []
    current: dict[str, str] = {}
    progress: dict[str, float] = {}
    context: dict[str, dict] = {}

    for r in rows:
        units.append(
            Unit(
                id=r["id"], kind=r["kind"], label=r["label"], operator=r["operator"],
                location=(float(r["lng"]), float(r["lat"])), capacity=r["capacity"],
            )
        )
        context[r["id"]] = {
            "assignment_id": r["assignment_id"],
            "route": r["route"] or [],
            "incident_id": r["incident_id"],
            "incident_title": r["incident_title"] or "",
            "label": r["label"],
            "operator": r["operator"],
            "progress": float(r["progress"] or 0.0),
            "assignment_status": r["assignment_status"],
        }
        if r["incident_id"]:
            # Any demand of this incident counts as "where it is already going".
            current[r["id"]] = r["incident_id"]
            eta = max(1, int(r["eta_minutes"] or 1))
            age_s = (now - r["created_at"]).total_seconds()
            # The distance actually driven, when the unit has a road to drive;
            # elapsed time over ETA only when it does not.
            if r["route"] and r["progress"] is not None:
                done = float(r["progress"])
            else:
                done = age_s / 60.0 / eta
            if age_s < MIN_DWELL_SECONDS:
                done = max(done, DWELL_PROGRESS_FLOOR)
            progress[r["id"]] = max(0.0, min(1.0, done))
    return units, current, progress, context


#: What the last set of avoided points was made of, for the decision log.
_BLOCK_MIX: dict[str, Any] = {"reported": 0, "predicted": 0, "model": None}


def _why_unit(alloc: Any, units: Sequence[Any], demands: Sequence[Any], matrix: Any) -> dict:
    """Why this unit and not another: the other units that could do the job and
    how far they were, from the same matrix the solver used."""
    from app import taxonomy
    try:
        dj = next(j for j, d in enumerate(demands) if d.id == alloc.demand.id)
    except StopIteration:
        return {}
    alts = sorted(
        (matrix.durations[i][dj], u.label) for i, u in enumerate(units)
        if u.id != alloc.unit.id and taxonomy.cache.kind_can(u.kind, alloc.demand.capability)
    )
    reach = [a for a in alts if a[0] <= MAX_ETA_MINUTES]
    return {"alternatives": len(reach),
            "next_best": ({"unit": reach[0][1], "eta_minutes": round(reach[0][0], 1)} if reach else None)}


async def _blocked_points(
    conn: Any, city_id: str, sim_run_id: str | None
) -> list[tuple[float, float]]:
    """Places a vehicle should not be routed through.

    Two sources, both real: roads a crew has declared impassable, and open
    incidents whose category means the road itself is the problem. A waterlogged
    basement does not block a street; a collapsed wall across one does, and the
    taxonomy already knows the difference.
    """
    rows = await conn.fetch(
        """
        select extensions.ST_X(location::extensions.geometry) lng,
               extensions.ST_Y(location::extensions.geometry) lat
          from road_blocks where city_id = $1 and active
        union all
        select extensions.ST_X(i.location::extensions.geometry),
               extensions.ST_Y(i.location::extensions.geometry)
          from incidents i
         where i.city_id = $1
           and i.sim_run_id is not distinct from $2::uuid
           and i.status <> 'resolved'
           and i.category in ('flooded_road','structural_damage','power_line','fallen_tree')
        """,
        city_id, sim_run_id,
    )
    points = [(float(r["lng"]), float(r["lat"])) for r in rows]
    _BLOCK_MIX.update({"reported": len(points), "predicted": 0, "model": None})
    # Roads the passability model expects to be blocked by the time a unit gets
    # there (app/nav/live.py, PCMC district): avoided like reported blocks, so a
    # crew is not sent into an underpass that is about to fill. Confirmed blocks
    # come first; predictions fill the rest of the router's 50 avoid points.
    # NAV_PREDICTIVE=0 turns this off (current-status-only routing).
    import os
    if sim_run_id is None and os.getenv("NAV_PREDICTIVE", "1") != "0":
        try:
            from app.nav import live
            pred = await live.predicted_blocks(limit=max(0, min(live.MAX_POINTS, 50 - len(points))))
            if pred:
                points += [(lo, la) for lo, la, _ in pred]
                await live.log_used(pred, "route")
                _BLOCK_MIX.update({"predicted": len(pred),
                                   "model": getattr(live._cache, "model", None)})
        except Exception as exc:  # noqa: BLE001 - prediction is advice; routing must not fail on it
            log.warning("predicted_blocks_failed", error=str(exc)[:200])
    return points


class _Preview(Exception):
    """Raised inside the transaction to roll a dry run back, carrying its diff."""

    def __init__(self, diff: PlanDiff) -> None:
        super().__init__("preview")
        self.diff = diff


async def preview(
    *,
    city_id: str = "pune",
    clock: Clock = WALL,
    trigger: str = "preview",
    caused_by: int | None = None,
    actor: str = "agent:langgraph",
) -> PlanDiff:
    """The plan `replan` would make right now, with nothing written.

    Runs the real solve inside the real transaction, then rolls it back. Used by
    the agent graph so the policy gate (and, if needed, an officer) sees the
    exact re-tasking before it happens, rather than a separate estimate that
    could drift from what the solver actually does.
    """
    try:
        return await replan(city_id=city_id, clock=clock, trigger=trigger,
                            caused_by=caused_by, actor=actor, dry_run=True)
    except _Preview as p:
        return p.diff


# ------------------------------------------------------------------ replan ---
async def replan(
    *,
    city_id: str = "pune",
    clock: Clock = WALL,
    trigger: str = "manual",
    caused_by: int | None = None,
    actor: str = "agent:allocation_planner",
    dry_run: bool = False,
) -> PlanDiff:
    """Re-solve against the current world and return what changed.

    With `dry_run` the whole solve and every write happen, then the transaction
    is rolled back by raising `_Preview`; call `preview()` rather than this.
    """
    now = clock.now()
    sim_run_id = clock.sim_run_id

    async with db.transaction() as conn:
        demands = await _open_demands(conn, city_id, sim_run_id)
        units, current_raw, progress, context = await _fleet(conn, city_id, sim_run_id, now)

        if not demands:
            return PlanDiff(plan_id=None, engine="none", runtime_ms=0, coverage=1.0)

        # `current` maps unit -> the one demand it is already serving.
        current = map_commitments(units, current_raw, demands, progress)

        # Locked units: on scene and working, or pinned by a person. They keep
        # their job and the demand they serve leaves the problem, so no solve
        # can move a crew that is already pumping out a basement, and no
        # re-plan can quietly undo what an officer ordered.
        overrides = await load_overrides(conn, city_id)
        locked = {
            uid for uid, ctx in context.items()
            if current_raw.get(uid) and (
                ctx.get("assignment_status") == "on_site" or uid in overrides["pin"]
            )
        }
        held = overrides["hold"] - set(current_raw)
        served_by_locked = {current[uid] for uid in locked if uid in current}
        total_demands = len(demands)
        demands = [d for d in demands if d.id not in served_by_locked]
        units = [u for u in units if u.id not in locked and u.id not in held]
        current = {u: d for u, d in current.items() if u not in locked}

        # Every hazard the city currently believes in, as a point a route should
        # not pass through. Fed to both the matrix (so a blocked pairing costs
        # more and the solver prefers a unit that can actually get there) and to
        # each route (so the geometry itself goes round).
        blocked = await _blocked_points(conn, city_id, sim_run_id)

        started = time.perf_counter()
        matrix = await routing.travel_matrix(
            [u.location for u in units], [d.location for d in demands], blocked
        )
        # Aircraft do not use roads: straight line at cruise speed plus start-up,
        # and nothing at all when it rains harder than the aircraft's limit.
        _air_matrix(units, demands, matrix)
        # A unit an officer took off an incident must not be sent straight back
        # to it by the next solve. Pricing the pair out of reach does that
        # without a second code path in the solver.
        if overrides["forbid"]:
            for i, u in enumerate(units):
                for j, d in enumerate(demands):
                    if (u.id, d.incident_id) in overrides["forbid"]:
                        matrix.durations[i][j] = MAX_ETA_MINUTES + 1
        # CP-SAT runs up to its time budget; in a thread (OR-tools releases the GIL) the API keeps answering.
        result = await asyncio.to_thread(allocate, demands, units, matrix, current=current, progress=progress)
        runtime = int((time.perf_counter() - started) * 1000)

        plan = await conn.fetchrow(
            """
            insert into allocation_plans
              (hazard, objective, solver, uncovered, sim_run_id, generated_at)
            values ($1,$2,$3,$4,$5::uuid,$6)
            returning id::text
            """,
            "flood", result.objective_text,
            {
                "engine": result.engine, "runtime_ms": result.runtime_ms,
                "variables": result.variables, "constraints": result.constraints,
                "coverage": round(result.coverage, 4), "trigger": trigger,
            },
            [
                {"ward_id": u.demand.ward_id, "incident_id": u.demand.incident_id,
                 "need": u.demand.capability, "reason": u.reason, "shortfall": u.shortfall}
                for u in result.unmet
            ],
            sim_run_id, now,
        )
        plan_id = plan["id"]

        # Coverage over every demand, counting the ones locked units already
        # serve; the solver only saw the rest.
        coverage = (
            (total_demands - len(result.unmet)) / total_demands if total_demands else 1.0
        )
        diff = PlanDiff(
            plan_id=plan_id, engine=result.engine, runtime_ms=runtime,
            coverage=round(coverage, 4),
            uncovered=[
                {"ward_id": u.demand.ward_id, "incident_id": u.demand.incident_id,
                 "capability": u.demand.capability, "reason": u.reason}
                for u in result.unmet
            ],
        )

        plan_event = await ev.append(
            clock=clock, kind=ev.Kind.PLAN_GENERATED, actor=actor,
            subject_type="plan", subject_id=plan_id, city_id=city_id,
            payload={"trigger": trigger, "engine": result.engine,
                     "demands": len(demands), "units": len(units),
                     "coverage": diff.coverage, "avoided": dict(_BLOCK_MIX),
                     "reason": (f"Re-planned after {trigger}: {len(demands)} need(s), {len(units)} unit(s) "
                                f"free or re-taskable; routes avoid {_BLOCK_MIX['reported']} reported closure(s)"
                                + (f" and {_BLOCK_MIX['predicted']} the passability model expects blocked by arrival"
                                   f" ({_BLOCK_MIX['model']})" if _BLOCK_MIX["predicted"] else "")
                                + f"; {int(diff.coverage * 100)}% of needs covered ({result.engine}).")},
            caused_by=caused_by, conn=conn,
        )

        new_by_unit = {a.unit.id: a for a in result.allocations}

        for unit_id, ctx in context.items():
            was = current_raw.get(unit_id)
            if unit_id in locked:
                diff.kept.append(Change(
                    kind="kept", resource_id=unit_id, resource_label=ctx["label"],
                    incident_id=was, incident_title=ctx["incident_title"],
                    ward_id="",
                    reason=(
                        "Pinned here by an officer; the planner may not move it."
                        if unit_id in overrides["pin"]
                        else "On scene and working; not moved mid-job."
                    ),
                ))
                continue
            if unit_id in held and not was:
                continue
            now_alloc = new_by_unit.get(unit_id)
            to = now_alloc.demand.incident_id if now_alloc else None

            if was and to and was == to:
                # Still the right unit — but not necessarily still the right
                # road.
                #
                # This branch used to `continue` immediately, and that was the
                # gap: the replanner re-solved *who goes where* every time the
                # world changed and never redrew the path for the units that
                # stayed. A crew committed at tick 20 kept the geometry it was
                # given at tick 20, so a road reported impassable at tick 40 —
                # by another crew, on the road this one is driving — changed the
                # map, changed the citizen's route, and left the truck being
                # sent into it. The one vehicle that could not see the block was
                # the one being routed through it.
                rerouted = await _reroute_if_blocked(
                    conn, ctx, now_alloc, blocked, clock=clock, actor=actor,
                    caused_by=plan_event.id, city_id=city_id,
                )
                diff.kept.append(Change(
                    kind="kept", resource_id=unit_id, resource_label=ctx["label"],
                    incident_id=to, incident_title=ctx["incident_title"],
                    ward_id=now_alloc.demand.ward_id,
                    eta_minutes=rerouted or now_alloc.eta_minutes,
                    reason=(
                        "Already committed here, and the road it was given is "
                        "now blocked, so it has a new one."
                        if rerouted
                        else "Already committed here and still the right unit for it."
                    ),
                ))
                continue

            if was and to and was != to:
                pct = int(progress.get(unit_id, 0.0) * 100)
                change = Change(
                    kind="reassigned", resource_id=unit_id, resource_label=ctx["label"],
                    incident_id=to, incident_title=now_alloc.demand.purpose,
                    ward_id=now_alloc.demand.ward_id,
                    from_incident_id=was, from_incident_title=ctx["incident_title"],
                    eta_minutes=now_alloc.eta_minutes,
                    reason=(
                        f"Moved from \"{ctx['incident_title']}\" ({pct}% of the way there) "
                        f"because \"{now_alloc.demand.purpose}\" is severity "
                        f"{now_alloc.demand.severity} with "
                        f"{now_alloc.demand.population_at_risk:,} exposed."
                    ),
                )
                diff.reassigned.append(change)
                await conn.execute(
                    "update assignments set status = 'cancelled' where id = $1::uuid",
                    ctx["assignment_id"],
                )
                await close_tasks(conn, ctx["assignment_id"],
                                  f"Re-tasked by the planner: {change.reason}")
                route_out = {}
                await _write_assignment(conn, plan_id, now_alloc, sim_run_id, now, blocked, route_out)
                await ev.append(
                    clock=clock, kind=ev.Kind.ASSIGNMENT_CHANGED, actor=actor,
                    subject_type="resource", subject_id=unit_id, city_id=city_id,
                    ward_id=now_alloc.demand.ward_id,
                    payload={"from_incident": was, "to_incident": to,
                             "progress_pct": pct, "reason": change.reason,
                             "capability": now_alloc.demand.capability,
                             "eta_model": _model_eta(route_out, now_alloc),
                             **_why_unit(now_alloc, units, demands, matrix)},
                    caused_by=plan_event.id, conn=conn,
                )
                continue

            if not was and to and now_alloc:
                why = _why_unit(now_alloc, units, demands, matrix)
                nb = why.get("next_best")
                d = now_alloc.demand
                change = Change(
                    kind="assigned", resource_id=unit_id, resource_label=ctx["label"],
                    incident_id=to, incident_title=d.purpose,
                    ward_id=d.ward_id, eta_minutes=now_alloc.eta_minutes,
                    reason=(f"Sent for {d.capability.replace('_', ' ')} (severity {d.severity}, "
                            f"{d.population_at_risk:,} exposed{', triage x%.1f' % d.priority if d.priority != 1 else ''}): "
                            f"{now_alloc.eta_minutes} min away"
                            + (f"; next best {nb['unit']} at {nb['eta_minutes']:.0f} min" if nb
                               else "; no other unit able to do this could reach it in time")
                            + "."),
                )
                diff.assigned.append(change)
                await conn.execute(
                    "update resources set status = 'assigned', updated_at = $2 where id = $1",
                    unit_id, now,
                )
                route_out: dict = {}
                await _write_assignment(conn, plan_id, now_alloc, sim_run_id, now, blocked, route_out)
                model_eta = _model_eta(route_out, now_alloc)
                await ev.append(
                    clock=clock, kind=ev.Kind.ASSIGNMENT_CREATED, actor=actor,
                    subject_type="assignment", subject_id=to, city_id=city_id,
                    ward_id=now_alloc.demand.ward_id,
                    payload={"resource_id": unit_id, "eta_minutes": now_alloc.eta_minutes,
                             "capability": now_alloc.demand.capability,
                             "severity": now_alloc.demand.severity,
                             "population_at_risk": now_alloc.demand.population_at_risk,
                             "triage_priority": now_alloc.demand.priority,
                             "route_engine": route_out.get("engine"),
                             "eta_model": model_eta, **why,
                             "reason": change.reason + (
                                 f" Response-time model: {model_eta['p50']:.0f} min, 90% within "
                                 f"{model_eta['p90']:.0f}" + (f"; flood risk on route up to {model_eta['riskMax']:.0%}"
                                                               if model_eta.get('riskMax', 0) >= 0.3 else "") + "."
                                 if model_eta else "")},
                    caused_by=plan_event.id, conn=conn,
                )
                continue

            if was and not to:
                diff.released.append(Change(
                    kind="released", resource_id=unit_id, resource_label=ctx["label"],
                    incident_id=None, incident_title="", ward_id="",
                    from_incident_id=was, from_incident_title=ctx["incident_title"],
                    reason="No longer needed there, and nothing else is closer to it.",
                ))
                await conn.execute(
                    "update assignments set status = 'cancelled' where id = $1::uuid",
                    ctx["assignment_id"],
                )
                await close_tasks(conn, ctx["assignment_id"],
                                  "Stood down by the planner: no longer needed there.")
                await conn.execute(
                    "update resources set status = 'available', updated_at = $2 where id = $1",
                    unit_id, now,
                )
                await ev.append(
                    clock=clock, kind=ev.Kind.ASSIGNMENT_CANCELLED, actor=actor,
                    subject_type="resource", subject_id=unit_id, city_id=city_id,
                    payload={"from_incident": was,
                             "reason": "Stood down: the need it was going to is covered or gone, and nothing "
                                       "else is closer to it."},
                    caused_by=plan_event.id, conn=conn,
                )

        # Needs coverage, so the console can show a shortfall rather than make
        # someone derive it from two other screens.
        # One aggregate pass, and only rows whose number actually moved.
        #
        # This was a correlated `select count(*)` evaluated once per need row,
        # inside the same transaction that had just rewritten assignments and
        # resources, while the demo tick loop was moving the whole fleet. Every
        # need row took a row lock whether or not its value changed, the tick's
        # fleet update waited on those locks, and the statement timed out. The
        # `is distinct from` guard is the important half: on a normal re-plan
        # almost nothing changes, so almost nothing is locked.
        await conn.execute(
            """
            with counts as (
              select a.incident_id, count(*)::int n
                from assignments a
               where a.status = any($1::assignment_status[])
               group by a.incident_id
            )
            update incident_needs n
               set met = coalesce(c.n, 0), updated_at = $2
              from incidents i
              left join counts c on c.incident_id = i.id
             where i.id = n.incident_id
               and i.status <> 'resolved'
               and n.met is distinct from coalesce(c.n, 0)
            """ if not await _has_capability_column(conn) else
            # per capability once assignments know which need they cover (032):
            # a fire with a tender and an ambulance shows each need met once,
            # not both needs met twice.
            """
            with counts as (
              select a.incident_id, a.capability_id, count(*)::int n
                from assignments a
               where a.status = any($1::assignment_status[])
               group by a.incident_id, a.capability_id
            )
            , target as (
              -- the join on capability needs the need row itself, which an
              -- UPDATE's FROM cannot see ("invalid reference to FROM-clause
              -- entry for table n"), so the new numbers are worked out first
              select x.incident_id, x.capability_id, coalesce(c.n, 0) met
                from incident_needs x
                join incidents i on i.id = x.incident_id and i.status <> 'resolved'
                left join counts c on c.incident_id = x.incident_id
                                  and c.capability_id = x.capability_id
            )
            update incident_needs n
               set met = t.met, updated_at = $2
              from target t
             where t.incident_id = n.incident_id
               and t.capability_id = n.capability_id
               and n.met is distinct from t.met
            """,
            list(ACTIVE), now,
        )

        for u in result.unmet:
            await ev.append(
                clock=clock, kind=ev.Kind.DEMAND_UNCOVERED, actor=actor,
                subject_type="incident", subject_id=u.demand.incident_id or u.demand.ward_id,
                city_id=city_id, ward_id=u.demand.ward_id,
                payload={"capability": u.demand.capability, "reason": u.reason},
                caused_by=plan_event.id, conn=conn,
            )

        if dry_run:
            raise _Preview(diff)     # rolls the transaction back

    log.info(
        "replan_complete", trigger=trigger, engine=result.engine,
        kept=len(diff.kept), assigned=len(diff.assigned),
        reassigned=len(diff.reassigned), released=len(diff.released),
        uncovered=len(diff.uncovered),
    )
    # The surge ladder reads the plan that was just written (unmet needs, load).
    try:
        from app.surge import service as surge
        await surge.maybe_evaluate()
    except Exception as exc:  # noqa: BLE001
        log.warning("surge_after_replan_failed", error=str(exc)[:200])
    return diff


async def _reroute_if_blocked(
    conn: Any,
    ctx: dict,
    alloc: Any,
    blocked: Sequence[tuple[float, float]],
    *,
    clock: Clock,
    actor: str,
    caused_by: int | None,
    city_id: str,
) -> int | None:
    """Redraw a committed unit's road when the one it has crosses a new block.

    Returns the new ETA when it re-routed, `None` when the existing road is
    still clear — so the caller can say which happened rather than implying a
    reroute every time.

    Only the geometry changes. The unit, the incident and the assignment row are
    the same: this is not a re-tasking and must not read as one, on the diff or
    in the log. `assignment.rerouted` is its own event kind for exactly that
    reason.
    """
    if not blocked:
        return None
    path = ctx.get("route") or []
    if len(path) < 2:
        # No stored geometry to judge. A straight-line fallback assignment has
        # none, and re-routing on no evidence would redraw every unit on every
        # plan.
        return None

    # Only the road still ahead counts. A block the unit has already driven past
    # is not a reason to hand it a new route.
    ahead = routing.remaining_path(path, float(ctx.get("progress") or 0.0))
    exposure = routing.exposure(ahead, blocked)
    if not exposure:
        return None

    if alloc.unit.kind == "helicopter":
        return None                      # flies; road closures do not reroute it
    line = await routing.route_line(alloc.unit.location, alloc.demand.location, blocked)
    geometry = (
        {"type": "LineString", "coordinates": line.coordinates}
        if len(line.coordinates) > 1
        else None
    )
    if geometry is None:
        return None
    # Swap only for a strictly better road. When every way in is exposed the
    # old code swapped one exposed road for another on every pass, which is the
    # flicker people saw on the map.
    if line.passes_near_blocks >= exposure:
        return None

    await conn.execute(
        """
        update assignments
           set route = extensions.ST_SetSRID(
                         extensions.ST_GeomFromGeoJSON($2::text), 4326),
               route_engine = $3,
               eta_minutes = $4,
               distance_km = $5,
               steps = $6,
               -- The new road starts where the unit is now. Keeping the old
               -- progress made the unit jump that fraction along the new line.
               progress = 0
         where id = $1::uuid
        """,
        ctx["assignment_id"],
        json.dumps(geometry),
        line.engine,
        line.minutes if line.is_real_road else alloc.eta_minutes,
        round(line.km if line.is_real_road else alloc.distance_km, 2),
        [
            {"instruction": st.instruction, "street": st.street,
             "distanceM": st.distance_m}
            for st in line.steps
        ],
    )
    await ev.append(
        clock=clock, kind="assignment.rerouted", actor=actor,
        subject_type="resource", subject_id=alloc.unit.id, city_id=city_id,
        ward_id=alloc.demand.ward_id,
        payload={
            "to_incident": alloc.demand.incident_id,
            "reason": (
                f"The road {ctx['label']} was given crosses "
                f"{exposure} newly blocked point(s). Redrawn around them; "
                f"{line.minutes} min now."
            ),
            "blocked_points": exposure,
            "still_exposed": line.passes_near_blocks,
            "avoidance": line.avoidance,
            "eta_minutes": line.minutes,
            "engine": line.engine,
        },
        caused_by=caused_by, conn=conn,
    )
    return line.minutes if line.is_real_road else None


AIR_STARTUP_MIN = 10.0


def _rain_now_mmph() -> float | None:
    try:
        from app.nav import live
        r = live._cache
        if r is not None and len(r.features):
            return float(r.features["rain_1h"].iloc[0])
    except Exception:  # noqa: BLE001
        pass
    return None


def _air_matrix(units: Sequence[Any], demands: Sequence[Any], matrix: Any) -> None:
    from app.nav.profiles import PROFILES
    rain = _rain_now_mmph()
    for i, u in enumerate(units):
        p = PROFILES.get(u.kind)
        if p is None or p.mode != "air" or u.kind == "drone":
            continue
        grounded = rain is not None and p.max_rain_mmph is not None and rain > p.max_rain_mmph
        for j, d in enumerate(demands):
            km = routing.haversine_km(u.location, d.location)
            matrix.durations[i][j] = (MAX_ETA_MINUTES + 1 if grounded
                                      else AIR_STARTUP_MIN + km / (p.cruise_kmh or 150.0) * 60.0)
            matrix.distances[i][j] = km


def _model_eta(route_out: dict, alloc: Any) -> dict | None:
    try:
        from app.nav import eta
        return eta.for_route(route_out.get("coordinates"), alloc.unit.kind, alloc.eta_minutes)
    except Exception:  # noqa: BLE001
        return None


async def _write_assignment(conn: Any, plan_id: str, alloc: Any, sim_run_id: str | None,
                            now: datetime, blocked: Sequence[tuple[float, float]] = (),
                            route_out: dict | None = None) -> str:
    # The road, not the line. A crew given a straight bearing to a flooded
    # junction is being given nothing, and a unit that moves across blocks on
    # the console looks like a simulation rather than a dispatch. The geometry
    # is stored so the unit drives it, the field app can show it, and the
    # console can draw what every committed unit is actually doing.
    if alloc.unit.kind == "helicopter":
        km = routing.haversine_km(alloc.unit.location, alloc.demand.location)
        line = routing.RouteLine(coordinates=[list(alloc.unit.location), list(alloc.demand.location)],
                                 km=km, minutes=int(round(AIR_STARTUP_MIN + km / 180.0 * 60.0)), engine="air")
    else:
        line = None
    if line is None:
        line = await routing.route_line(alloc.unit.location, alloc.demand.location, blocked)
    geometry = (
        {"type": "LineString", "coordinates": line.coordinates}
        if len(line.coordinates) > 1
        else None
    )
    if route_out is not None:
        route_out.update({"coordinates": line.coordinates, "engine": line.engine})
    steps = [
        {"instruction": s.instruction, "street": s.street, "distanceM": s.distance_m}
        for s in line.steps
    ]

    row = await conn.fetchrow(
        """
        insert into assignments
          (plan_id, resource_id, incident_id, ward_id, purpose, eta_minutes,
           distance_km, status, sim_run_id, created_at,
           route, route_engine, progress, steps)
        values ($1::uuid,$2,$3::uuid,$4,$5,$6,$7,'proposed',$8::uuid,$9,
                case when $10::text is null then null
                     else extensions.ST_SetSRID(
                            extensions.ST_GeomFromGeoJSON($10::text), 4326) end,
                $11, 0, $12)
        returning id::text
        """,
        plan_id, alloc.unit.id, alloc.demand.incident_id, alloc.demand.ward_id,
        alloc.demand.purpose,
        # The router's own number when it answered, the solver's estimate when
        # it did not. Never the optimistic one.
        line.minutes if line.is_real_road else alloc.eta_minutes,
        round(line.km if line.is_real_road else alloc.distance_km, 2),
        sim_run_id, now,
        json.dumps(geometry) if geometry else None,
        line.engine,
        steps,
    )
    if await _has_capability_column(conn):
        # which need of the incident this unit covers (migration 032)
        await conn.execute("update assignments set capability_id = $2 where id = $1::uuid",
                           row["id"], alloc.demand.capability)
    await conn.execute(
        """
        insert into field_tasks
          (assignment_id, resource_id, operator, title, instruction, location,
           ward_id, priority, sim_run_id, created_at)
        values ($1::uuid,$2,$3,$4,$5,
                extensions.ST_SetSRID(extensions.ST_MakePoint($6,$7),4326)::extensions.geography,
                $8,$9,$10::uuid,$11)
        """,
        row["id"], alloc.unit.id, alloc.unit.operator, alloc.demand.purpose,
        f"{alloc.demand.purpose}. {alloc.unit.label}, {alloc.eta_minutes} minutes out. "
        "Confirm on arrival and record what you found before closing the task.",
        alloc.demand.location[0], alloc.demand.location[1], alloc.demand.ward_id,
        alloc.demand.severity, sim_run_id, now,
    )
    return row["id"]
