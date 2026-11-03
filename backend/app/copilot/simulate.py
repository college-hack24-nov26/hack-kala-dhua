"""What-if, without touching the world.

Every number the Copilot quotes about a hypothetical comes from here, and here
re-solves the *same* model the live planner does: the same demands, the same
fleet, the same travel matrix, the same CP-SAT objective. The only difference is
that nothing is written and the inputs are perturbed first.

That matters more than it looks. A "simulation" that scores a hypothetical with
its own simpler arithmetic will disagree with the planner the moment somebody
acts on it, and the first time an officer notices that the preview promised
14 minutes and the dispatch produced 26, they stop trusting the preview — which
is the only part of an advisory system that has to be trusted.

Three perturbations, which is enough to answer the questions a commissioner
actually asks:

* **pin** — "what if we send A12 to Zone 4 instead". The unit is committed by
  hand and the solver optimises around that commitment, which is exactly what
  happens when a person overrules the plan.
* **surge** — "what if Zone 4 gets fifty more medical calls". Synthetic demands
  are appended and the fleet has to absorb them.
* **withdraw** — "what if we hold two ambulances in reserve". Units are taken
  out of the pool.

The comparison is always against a baseline solved in the same call, from the
same snapshot, so the two arms cannot be a minute apart in a fast-moving world.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from app.agents.replan import _blocked_points, _fleet, _open_demands, map_commitments
from app.core.logging import get_logger
from app.db import session as db
from app.solver import routing
from app.solver.allocation import Demand, Unit, allocate
from app.world.clock import WALL

log = get_logger(__name__)

#: How far above a demand's own ETA a unit may be and still be worth pinning.
#: Past this the answer to "can we send it there" is "not usefully".
PIN_ETA_CEILING = 90


@dataclass(slots=True)
class Inputs:
    """One snapshot of the world, solved more than once."""

    demands: list[Demand]
    units: list[Unit]
    current: dict[str, str]
    progress: dict[str, float]
    context: dict[str, dict]
    blocked: list[tuple[float, float]]
    wards: dict[str, str]
    #: Ward centroids, so a projected demand has somewhere to be.
    ward_points: dict[str, tuple[float, float]]
    incidents: dict[str, dict]
    now: datetime
    #: unit id -> demand id it is serving, in the solver's terms. `current` is
    #: unit -> incident, which is what a crew experiences and what `_switches`
    #: counts; the solver needs the demand-level one, exactly as the live
    #: planner builds it. Passing the incident-level map to the solver priced
    #: every committed unit as switching, so the "Now" column of every
    #: comparison was a different plan from the one actually running.
    commit: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Scenario:
    """What one solve produced, in the terms a commissioner asks about."""

    label: str
    assigned: int = 0
    unmet: int = 0
    coverage: float = 1.0
    eta_median: float | None = None
    eta_p90: float | None = None
    switches: int = 0
    engine: str = "cp-sat"
    #: ward id -> best ETA any unit achieves into that ward
    ward_eta: dict[str, int] = field(default_factory=dict)
    #: unit id -> incident id it ends up on
    placement: dict[str, str] = field(default_factory=dict)
    note: str = ""


async def inputs(city_id: str = "pune") -> Inputs:
    """Read the world once. Read-only: no transaction, nothing written."""
    now = WALL.now()
    async with db.acquire() as conn:
        demands = await _open_demands(conn, city_id, None)
        units, current, progress, context = await _fleet(conn, city_id, None, now)
        blocked = await _blocked_points(conn, city_id, None)
        ward_rows = await conn.fetch(
            """
            select id, name,
                   extensions.ST_X(centroid::extensions.geometry) lng,
                   extensions.ST_Y(centroid::extensions.geometry) lat
              from wards where city_id = $1
            """,
            city_id,
        )
        wards = {r["id"]: r["name"] for r in ward_rows}
        ward_points = {
            r["id"]: (float(r["lng"]), float(r["lat"])) for r in ward_rows
        }
        incidents = {
            r["id"]: dict(r)
            for r in await conn.fetch(
                """
                select id::text, title, ward_id, severity, category, status::text status,
                       extensions.ST_X(location::extensions.geometry) lng,
                       extensions.ST_Y(location::extensions.geometry) lat
                  from incidents
                 where city_id = $1 and status <> 'resolved'
                """,
                city_id,
            )
        }
    return Inputs(
        demands=demands, units=units, current=current, progress=progress,
        context=context, blocked=blocked, wards=wards, ward_points=ward_points,
        incidents=incidents, now=now,
        commit=map_commitments(units, current, demands, progress),
    )


async def _solve(
    label: str,
    demands: Sequence[Demand],
    units: Sequence[Unit],
    blocked: Sequence[tuple[float, float]],
    *,
    current: dict[str, str],
    progress: dict[str, float],
    forced: dict[str, tuple[Demand, int]] | None = None,
) -> Scenario:
    """One solve, scored. `forced` is already-decided placements to work around."""
    forced = forced or {}
    if not demands or not units:
        s = Scenario(label=label, unmet=len(demands), coverage=0.0 if demands else 1.0)
        for unit_id, (demand, eta) in forced.items():
            s.placement[unit_id] = demand.incident_id or demand.ward_id
            s.ward_eta[demand.ward_id] = min(s.ward_eta.get(demand.ward_id, eta), eta)
        return s

    matrix = await routing.travel_matrix(
        [u.location for u in units], [d.location for d in demands], list(blocked)
    )
    result = allocate(demands, units, matrix, current=current, progress=progress)

    scenario = Scenario(
        label=label,
        assigned=len(result.allocations) + len(forced),
        unmet=len(result.unmet),
        engine=result.engine,
    )
    etas: list[int] = []
    for a in result.allocations:
        etas.append(a.eta_minutes)
        scenario.placement[a.unit.id] = a.demand.incident_id or a.demand.ward_id
        best = scenario.ward_eta.get(a.demand.ward_id)
        if best is None or a.eta_minutes < best:
            scenario.ward_eta[a.demand.ward_id] = a.eta_minutes
    for unit_id, (demand, eta) in forced.items():
        etas.append(eta)
        scenario.placement[unit_id] = demand.incident_id or demand.ward_id
        best = scenario.ward_eta.get(demand.ward_id)
        if best is None or eta < best:
            scenario.ward_eta[demand.ward_id] = eta

    total = scenario.assigned + scenario.unmet
    scenario.coverage = round(scenario.assigned / total, 4) if total else 1.0
    if etas:
        scenario.eta_median = round(statistics.median(etas), 1)
        ordered = sorted(etas)
        scenario.eta_p90 = float(ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))])
    return scenario


def _switches(after: Scenario, current: dict[str, str]) -> int:
    """Units that end up somewhere other than where they are committed now.

    Counted against the *live* commitments rather than against the baseline
    solve, because that is what the crews would actually experience: a turn in
    the road, not a difference between two hypotheticals.
    """
    return sum(
        1 for unit_id, incident_id in after.placement.items()
        if current.get(unit_id) and current[unit_id] != incident_id
    )


async def baseline(world: Inputs, label: str = "Now") -> Scenario:
    scenario = await _solve(
        label, world.demands, world.units, world.blocked,
        current=world.commit, progress=world.progress,
    )
    scenario.switches = 0
    return scenario


# ------------------------------------------------------------ perturbations ---
async def _pin_cost(unit: Unit, demand: Demand, blocked) -> int:
    matrix = await routing.travel_matrix([unit.location], [demand.location], list(blocked))
    return matrix.eta(0, 0)


async def compare(
    world: Inputs,
    *,
    label: str,
    pin: dict[str, str] | None = None,
    withdraw: Sequence[str] = (),
    surge: Sequence[tuple[str, str, int]] = (),
) -> tuple[Scenario, Scenario]:
    """Solve the world as it is, and as it would be. Returns (now, proposed).

    `pin` maps resource id -> incident id and is the interesting one: the unit
    is removed from the pool, committed by hand, and the solver has to cover
    everything else without it. That is what overruling the plan actually costs,
    and it is a cost the Copilot should state out loud before anybody approves.

    `surge` is (ward_id, capability, count) and `withdraw` a list of resource
    ids held back.
    """
    now_scenario = await baseline(world)

    units = list(world.units)
    demands = list(world.demands)
    by_unit = {u.id: u for u in world.units}

    forced: dict[str, tuple[Demand, int]] = {}
    notes: list[str] = []

    for resource_id, incident_id in (pin or {}).items():
        unit = by_unit.get(resource_id)
        incident = world.incidents.get(incident_id)
        if unit is None or incident is None:
            notes.append(f"{resource_id} or {incident_id} is not in the current world.")
            continue
        target = next(
            (d for d in demands if d.incident_id == incident_id), None
        ) or Demand(
            id=f"pin:{incident_id}", ward_id=incident["ward_id"], incident_id=incident_id,
            capability="", purpose=incident["title"],
            location=(float(incident["lng"]), float(incident["lat"])),
            severity=int(incident["severity"]), population_at_risk=0,
        )
        eta = await _pin_cost(unit, target, world.blocked)
        if eta > PIN_ETA_CEILING:
            notes.append(
                f"{unit.label} is {eta} minutes from {incident['title']}, which is "
                "too far to be a response."
            )
        forced[resource_id] = (target, eta)
        units = [u for u in units if u.id != resource_id]
        demands = [d for d in demands if d.id != target.id]

    if withdraw:
        held = set(withdraw)
        units = [u for u in units if u.id not in held]
        notes.append(f"{len(held)} unit(s) held in reserve.")

    for ward_id, capability, count in surge:
        ward_point = _ward_point(world, ward_id)
        if ward_point is None:
            notes.append(f"Ward {ward_id} is not in this city.")
            continue
        for k in range(int(count)):
            demands.append(
                Demand(
                    id=f"surge:{ward_id}:{capability}:{k}", ward_id=ward_id,
                    incident_id=None, capability=capability,
                    purpose=f"Projected {capability.replace('_', ' ')} demand",
                    location=ward_point, severity=3, population_at_risk=0,
                )
            )

    # A pinned unit is no longer available to keep its old commitment, so it must
    # not appear in `current` either — otherwise the solver is told to preserve
    # something it cannot.
    current = {u: d for u, d in world.commit.items() if u not in forced}
    progress = {u: p for u, p in world.progress.items() if u not in forced}

    proposed = await _solve(
        label, demands, units, world.blocked,
        current=current, progress=progress, forced=forced,
    )
    proposed.switches = _switches(proposed, world.current)
    proposed.note = " ".join(notes)
    return now_scenario, proposed


def _ward_point(world: Inputs, ward_id: str) -> tuple[float, float] | None:
    """Where to put a demand that has not happened yet.

    An open incident in the same ward first, because that is where the trouble
    already is and a projection is more useful near it than at a geometric
    centre; the ward centroid otherwise.
    """
    for inc in world.incidents.values():
        if inc["ward_id"] == ward_id:
            return (float(inc["lng"]), float(inc["lat"]))
    return world.ward_points.get(ward_id)


# ------------------------------------------------------------------ output ---
def as_table(now: Scenario, proposed: Scenario, world: Inputs) -> dict[str, Any]:
    """The comparison, shaped for the UI's `comparison` block."""

    def delta(a: float | None, b: float | None, *, lower_is_better: bool = True) -> dict:
        if a is None or b is None:
            return {"change": None, "better": None}
        diff = round(b - a, 1)
        better = (diff < 0) if lower_is_better else (diff > 0)
        return {"change": diff, "better": None if diff == 0 else better}

    rows = [
        {"metric": "Demands covered", "current": now.assigned,
         "proposed": proposed.assigned, **delta(now.assigned, proposed.assigned,
                                                lower_is_better=False)},
        {"metric": "Unmet demand", "current": now.unmet,
         "proposed": proposed.unmet, **delta(now.unmet, proposed.unmet)},
        {"metric": "Median ETA (min)", "current": now.eta_median,
         "proposed": proposed.eta_median, **delta(now.eta_median, proposed.eta_median)},
        {"metric": "Slowest tenth, ETA (min)", "current": now.eta_p90,
         "proposed": proposed.eta_p90, **delta(now.eta_p90, proposed.eta_p90)},
        {"metric": "Units moved off a job", "current": 0,
         "proposed": proposed.switches, **delta(0, proposed.switches)},
    ]

    ward_rows = []
    for ward_id in sorted(set(now.ward_eta) | set(proposed.ward_eta)):
        a = now.ward_eta.get(ward_id)
        b = proposed.ward_eta.get(ward_id)
        if a == b:
            continue
        ward_rows.append({
            "ward": world.wards.get(ward_id, ward_id),
            "current": a, "proposed": b,
            **delta(a if a is not None else 999, b if b is not None else 999),
        })

    return {
        "rows": rows,
        "wards": sorted(ward_rows, key=lambda r: (r["change"] is None, r["change"] or 0)),
        "engine": proposed.engine,
        "note": proposed.note,
    }
