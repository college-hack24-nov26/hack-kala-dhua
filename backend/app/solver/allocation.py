"""Resource allocation.

The model proposes and explains; this decides the numbers.

Formulated as a constrained assignment problem and solved with OR-Tools
CP-SAT. If the solver is unavailable or hits its time budget, a greedy
nearest-first heuristic takes over and the result says so — a dispatch plan
that silently degrades is worse than one that admits it degraded.

What the objective actually minimises is population-weighted arrival time:
sending the only free boat to the ward with 40 people rather than the one with
2,300 is arithmetically defensible, and an officer can see why.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from app.core.logging import get_logger
from app.solver.routing import TravelMatrix
from app.taxonomy import cache as taxonomy

log = get_logger(__name__)

#: Beyond this, a unit is not meaningfully responding to the incident.
MAX_ETA_MINUTES = 45
#: How hard it is to pull a unit off a job it is already committed to, relative
#: to the cost of the job itself. Without this the plan thrashes: every new
#: report reshuffles the whole city and a boat halfway to a rescue turns around.
#: With it, a unit moves only when the new demand is genuinely more important.
DEFAULT_SWITCH_PENALTY = 1.6
#: Seconds the solver is allowed. A dispatch decision that takes a minute to
#: compute is not a dispatch decision.
SOLVER_TIME_BUDGET_S = 4.0


@dataclass(frozen=True, slots=True)
class Unit:
    id: str
    kind: str
    label: str
    operator: str
    location: tuple[float, float]
    capacity: int


@dataclass(frozen=True, slots=True)
class Demand:
    """One thing that needs a unit.

    A demand asks for a CAPABILITY, not a vehicle type. "This needs water
    rescue" is answerable by a boat, a rescue team or a fire engine, at
    different effectiveness. Matching on capability rather than on a kind string
    is what lets a city with different equipment, or a hazard we have not seen
    yet, use this solver without a line of it changing.
    """

    id: str
    ward_id: str
    incident_id: str | None
    capability: str
    purpose: str
    location: tuple[float, float]
    severity: int
    population_at_risk: int
    #: Surge triage (app/surge): x2 for life-safety needs and x1.3 for wards with
    #: many elderly people while the region is strained or worse; 1 otherwise.
    priority: float = 1.0

    @property
    def weight(self) -> float:
        """Severity dominates; population breaks ties within a severity band."""
        return (self.severity**2) * (1.0 + self.population_at_risk / 10_000.0) * self.priority


@dataclass(slots=True)
class Allocation:
    demand: Demand
    unit: Unit
    eta_minutes: int
    distance_km: float


@dataclass(slots=True)
class Unmet:
    demand: Demand
    reason: str
    shortfall: int = 1


@dataclass(slots=True)
class AllocationResult:
    allocations: list[Allocation] = field(default_factory=list)
    unmet: list[Unmet] = field(default_factory=list)
    engine: str = "cp-sat"
    runtime_ms: int = 0
    variables: int = 0
    constraints: int = 0

    @property
    def coverage(self) -> float:
        total = len(self.allocations) + len(self.unmet)
        return len(self.allocations) / total if total else 1.0

    @property
    def objective_text(self) -> str:
        return (
            "Minimise total population-weighted time-to-arrival, subject to one "
            "assignment per unit, capability compatibility, a 45-minute "
            "reachability limit, travel times over the current road graph, and "
            "a switching cost on units already committed elsewhere."
        )


def _switch_multiplier(switch_penalty: float, progress: float) -> float:
    """How much more a committed unit costs to move, by how far it has gone.

    A unit that has barely left the depot is cheap to redirect; one nearly on
    scene is not. Both the solver and the greedy fallback call this, so a run
    that degrades produces a worse plan, never a differently-behaved one.
    """
    return 1.0 + (switch_penalty - 1.0) * (0.25 + 0.75 * max(0.0, min(1.0, progress)))


def _cap(capability_id: str) -> str:
    """Human phrasing for a capability id, for the uncovered-demand reasons an
    officer actually reads."""
    ref = taxonomy.capabilities.get(capability_id)
    return (ref.label if ref else capability_id.replace("_", " ")).lower()


def _effectiveness(unit_kind: str, capability_id: str) -> float:
    """1.0 for the right tool, less for a workable substitute, 0 for neither."""
    return taxonomy.effectiveness(unit_kind, capability_id)


def _greedy(
    demands: Sequence[Demand],
    units: Sequence[Unit],
    matrix: TravelMatrix,
    current_by_unit: Mapping[str, str] | None = None,
    switch_penalty: float = DEFAULT_SWITCH_PENALTY,
    progress: Mapping[str, float] | None = None,
) -> AllocationResult:
    """Nearest capable unit, worst demand first. Always terminates.

    The fallback honours the same switching cost as the solver, so degrading to
    greedy changes the quality of the plan but not its behaviour.
    """
    current_by_unit = current_by_unit or {}
    progress = progress or {}
    started = time.perf_counter()
    taken: set[str] = set()
    allocations: list[Allocation] = []
    unmet: list[Unmet] = []

    for d_idx, demand in sorted(
        enumerate(demands), key=lambda p: p[1].weight, reverse=True
    ):
        best: tuple[float, int, float] | None = None
        for u_idx, unit in enumerate(units):
            if unit.id in taken or not taxonomy.kind_can(unit.kind, demand.capability):
                continue
            eta = matrix.durations[u_idx][d_idx]
            if eta > MAX_ETA_MINUTES:
                continue
            # Effective cost: a unit that is only a partial fit pays for it, so
            # the purpose-built vehicle wins when travel times are comparable.
            effective = eta / max(0.1, _effectiveness(unit.kind, demand.capability))
            if current_by_unit.get(unit.id) not in (None, demand.id):
                effective *= _switch_multiplier(
                    switch_penalty, progress.get(unit.id, 0.0)
                )
            if best is None or effective < best[0]:
                best = (effective, u_idx, eta)

        if best is None:
            unmet.append(
                Unmet(
                    demand=demand,
                    reason=(
                        f"No unit able to provide {_cap(demand.capability)} within "
                        f"{MAX_ETA_MINUTES} minutes; the nearest free unit is "
                        "already committed to a higher-severity ward."
                    ),
                )
            )
            continue

        _effective, u_idx, eta = best
        taken.add(units[u_idx].id)
        allocations.append(
            Allocation(
                demand=demand,
                unit=units[u_idx],
                eta_minutes=max(4, round(eta)),
                distance_km=matrix.distances[u_idx][d_idx],
            )
        )

    return AllocationResult(
        allocations=allocations,
        unmet=unmet,
        engine="greedy-fallback",
        runtime_ms=int((time.perf_counter() - started) * 1000),
        variables=len(demands) * len(units),
        constraints=len(units) + len(demands),
    )


def allocate(
    demands: Sequence[Demand],
    units: Sequence[Unit],
    matrix: TravelMatrix,
    *,
    current: Mapping[str, str] | None = None,
    progress: Mapping[str, float] | None = None,
    switch_penalty: float = DEFAULT_SWITCH_PENALTY,
    time_budget_s: float = SOLVER_TIME_BUDGET_S,
) -> AllocationResult:
    """Assign units to demands. Never raises; degrades to greedy instead.

    `current` maps unit id to the demand id it is already committed to, and
    `progress` how far along it is, 0.0 to 1.0. Pass them and this becomes an
    incremental re-plan: keeping a committed unit where it is costs nothing,
    moving it costs `switch_penalty`, scaled up by how far it has already
    travelled. Pass neither and it is a cold solve, which is what the first run
    of an event does.

    This is the difference between a system that re-plans and one that thrashes.
    """
    current = current or {}
    progress = progress or {}

    if not demands or not units:
        return AllocationResult(
            unmet=[Unmet(d, "No units in the fleet for this demand.") for d in demands],
            engine="greedy-fallback",
        )

    try:
        from ortools.sat.python import cp_model
    except ImportError:
        log.warning("ortools_missing", note="falling back to greedy allocation")
        return _greedy(demands, units, matrix, current, switch_penalty, progress)

    started = time.perf_counter()
    model = cp_model.CpModel()

    # x[u][d] = 1 when unit u serves demand d. Only feasible pairs get a var,
    # which keeps the model small enough to solve inside the time budget.
    x: dict[tuple[int, int], object] = {}
    for u_idx, unit in enumerate(units):
        for d_idx, demand in enumerate(demands):
            if not taxonomy.kind_can(unit.kind, demand.capability):
                continue
            if matrix.durations[u_idx][d_idx] > MAX_ETA_MINUTES:
                continue
            x[(u_idx, d_idx)] = model.NewBoolVar(f"x_{u_idx}_{d_idx}")

    if not x:
        return _greedy(demands, units, matrix, current, switch_penalty, progress)

    constraints = 0

    # A unit does one job at a time.
    for u_idx in range(len(units)):
        vars_for_unit = [v for (u, _), v in x.items() if u == u_idx]
        if vars_for_unit:
            model.AddAtMostOne(vars_for_unit)
            constraints += 1

    # A demand is served at most once.
    served: list[object] = []
    for d_idx in range(len(demands)):
        vars_for_demand = [v for (_, d), v in x.items() if d == d_idx]
        if vars_for_demand:
            model.AddAtMostOne(vars_for_demand)
            constraints += 1
            served.append(sum(vars_for_demand))

    # Objective: reward coverage heavily, then minimise weighted arrival time.
    # Integers only - CP-SAT is an integer solver.
    UNSERVED_PENALTY = 10_000
    terms = []
    for (u_idx, d_idx), var in x.items():
        demand = demands[d_idx]
        unit = units[u_idx]
        eta = matrix.durations[u_idx][d_idx]
        # Partial fits pay for the gap, so the purpose-built unit wins a tie.
        fit = max(0.1, _effectiveness(unit.kind, demand.capability))
        cost = demand.weight * eta * 10 / fit
        # Churn cost: moving a committed unit is more expensive the further it
        # has already gone. Keeping it where it is is free.
        committed_to = current.get(unit.id)
        if committed_to is not None and committed_to != demand.id:
            cost *= _switch_multiplier(switch_penalty, progress.get(unit.id, 0.0))
        terms.append(int(cost) * var)

    coverage_bonus = [
        UNSERVED_PENALTY * int(demands[d].weight) * (1 - s)
        for d, s in enumerate(served)
    ]
    model.Minimize(sum(terms) + sum(coverage_bonus))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_budget_s
    solver.parameters.num_search_workers = 4
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        log.warning("cp_sat_no_solution", status=solver.StatusName(status))
        return _greedy(demands, units, matrix, current, switch_penalty, progress)

    allocations: list[Allocation] = []
    assigned_demands: set[int] = set()
    for (u_idx, d_idx), var in x.items():
        if solver.Value(var):
            allocations.append(
                Allocation(
                    demand=demands[d_idx],
                    unit=units[u_idx],
                    eta_minutes=max(4, round(matrix.durations[u_idx][d_idx])),
                    distance_km=matrix.distances[u_idx][d_idx],
                )
            )
            assigned_demands.add(d_idx)

    unmet = [
        Unmet(
            demand=demands[d_idx],
            reason=(
                f"No unit able to provide {_cap(demands[d_idx].capability)} could reach this ward "
                f"within {MAX_ETA_MINUTES} minutes without pulling a unit off a "
                "higher-severity ward."
            ),
        )
        for d_idx in range(len(demands))
        if d_idx not in assigned_demands
    ]

    allocations.sort(key=lambda a: (-a.demand.severity, a.eta_minutes))

    return AllocationResult(
        allocations=allocations,
        unmet=unmet,
        engine="cp-sat",
        runtime_ms=int((time.perf_counter() - started) * 1000),
        variables=len(x),
        constraints=constraints,
    )


def demands_from_actions(
    actions: Sequence[Mapping[str, object]],
) -> list[Demand]:  # pragma: no cover - thin adapter, exercised via the API
    """Flatten `ProposedAction.resource_need` into individual demands.

    `resource_need` is keyed by CAPABILITY, e.g. {"dewatering": 2}. Adapters
    describe what an action needs done, not which vehicle does it; the solver
    decides that from the fleet the city actually has.
    """
    demands: list[Demand] = []
    for action in actions:
        need = action.get("resource_need") or {}
        for capability, count in need.items():  # type: ignore[union-attr]
            for i in range(int(count)):
                demands.append(
                    Demand(
                        id=f"{action['action_key']}:{capability}:{i}",  # type: ignore[index]
                        ward_id=str(action.get("ward_id") or ""),
                        incident_id=action.get("incident_id"),  # type: ignore[arg-type]
                        capability=str(capability),
                        purpose=str(action.get("action") or ""),
                        location=action["location"],  # type: ignore[index]
                        severity=int(action.get("severity") or 3),
                        population_at_risk=int(action.get("population_at_risk") or 0),
                    )
                )
    return demands
