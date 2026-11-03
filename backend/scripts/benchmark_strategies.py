"""Does any of this actually beat what a control room does today?

Three dispatchers, one event stream, the same city, the same travel times, the
same clock. Everything that differs between the arms is the dispatch policy,
which is the only way the comparison means anything.

    python scripts/benchmark_strategies.py
    python scripts/benchmark_strategies.py --minutes 180 --seed 11 --json out.json

The arms
--------

**nearest**  What a radio and a whiteboard do, and what most "smart dispatch"
software does underneath: when a call comes in, send the closest free unit that
can do the job. No look-ahead, no revisiting. It is a genuinely decent policy
and it is the one to beat.

**oneshot**  Optimisation, committed. The same CP-SAT model this system uses,
run over the demands that are open and the units that are free, but an
assignment once made is never revisited. This is what "we optimise dispatch"
usually means in practice.

**indradhanu**  The same solver, re-run as the world changes, aware of what
every unit is already committed to and how far along it is, paying
`switch_penalty` to move one. The claim being tested is that revisiting beats
committing, and that the switching cost is what stops revisiting from
thrashing.

What is deliberately held constant
----------------------------------

Travel times come from `_fallback_matrix` — straight-line distance at an urban
speed, with road blocks applied — rather than from Mapbox. Live routing would
add network variance and API cost to a measurement that is not about routing,
and all three arms would get the same numbers from it anyway. The event stream
is generated once from a seed and replayed identically into each arm.

What it measures
----------------

* **time to assignment** — sim minutes from a demand appearing to a unit being
  committed to it. Median and p90, because the tail is where people die.
* **time to arrival** — the same, to a unit actually being on scene. This is
  the one that matters and the one a nearest-first policy quietly loses on,
  because its early commitments are still driving when the real call comes in.
* **unmet at close** — demands that never got anybody.
* **duplicate dispatch** — units committed to a demand that was already covered.
  The whole argument for deduplication, measured rather than asserted.
* **switches** — how often a committed unit was moved. Non-zero is the cost of
  the third arm; the point is that it stays small.

Honesty notes
-------------

This is a simulation, not a field trial: arrivals are synthetic, service times
are fixed, and nothing models a crew that stops for fuel. It is a fair test of
*policy*, and it is not evidence about Pune. Anything reported from it should
say so.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
from dataclasses import dataclass, field
from typing import Iterable, Sequence

sys.path.insert(0, ".")

from app.solver import allocation as alloc  # noqa: E402
from app.solver.routing import _fallback_matrix  # noqa: E402
from app import taxonomy  # noqa: E402

#: Sim minutes a unit works once it arrives, before it is free again.
SERVICE_MINUTES = 25.0
#: Sim minutes between planning cycles. Every arm plans on the same cadence, so
#: none of them wins by being asked more often.
CYCLE_MINUTES = 3.0
#: Mean minutes between incidents. Roughly what the live demo generates.
ARRIVAL_MEAN_MINUTES = 2.5


# ----------------------------------------------------------------- the world ---
@dataclass(slots=True)
class Arrival:
    """One demand, and when it appears."""

    at: float
    demand: alloc.Demand


@dataclass(slots=True)
class Commitment:
    unit_id: str
    demand_id: str
    committed_at: float
    arrives_at: float
    free_at: float | None = None

    def progress(self, now: float) -> float:
        span = self.arrives_at - self.committed_at
        if span <= 0:
            return 1.0
        return max(0.0, min(1.0, (now - self.committed_at) / span))


@dataclass(slots=True)
class Outcome:
    name: str
    assigned_at: dict[str, float] = field(default_factory=dict)
    arrived_at: dict[str, float] = field(default_factory=dict)
    switches: int = 0
    duplicate_dispatch: int = 0
    total: int = 0
    #: Which engine actually ran. `greedy-fallback` here means OR-Tools is not
    #: installed, and the two optimised arms are not what they claim to be.
    engine: str = "nearest-first"
    #: How many solves each engine did. `engine` above used to be assigned on
    #: every solve, so it reported *the last one*, and the last solve in a run
    #: is the one with the fewest options left — often a demand no remaining
    #: unit kind can reach, which builds an empty CP-SAT model and returns the
    #: greedy label. A run where CP-SAT did all the real work therefore printed
    #: "greedy-fallback" and the harness told you to install OR-Tools before
    #: reporting any of the numbers. Counting instead of overwriting means the
    #: label describes the run.
    engine_solves: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict:
        waits = [self.assigned_at[d] - t for d, t in self._born.items() if d in self.assigned_at]
        rides = [self.arrived_at[d] - t for d, t in self._born.items() if d in self.arrived_at]
        return {
            "strategy": self.name,
            "engine": (
                max(self.engine_solves, key=self.engine_solves.get)
                if self.engine_solves else self.engine
            ),
            "engine_solves": dict(self.engine_solves),
            "demands": self.total,
            "assigned": len(self.assigned_at),
            "arrived": len(self.arrived_at),
            "unmet_at_close": self.total - len(self.assigned_at),
            "time_to_assignment_median": _round(_median(waits)),
            "time_to_assignment_p90": _round(_pct(waits, 0.9)),
            "time_to_arrival_median": _round(_median(rides)),
            "time_to_arrival_p90": _round(_pct(rides, 0.9)),
            "duplicate_dispatch": self.duplicate_dispatch,
            "switches": self.switches,
        }

    #: Set by the runner; kept off the constructor so the dataclass stays a
    #: record of results rather than of inputs.
    _born: dict[str, float] = field(default_factory=dict)


def _median(xs: Sequence[float]) -> float | None:
    return statistics.median(xs) if xs else None


def _pct(xs: Sequence[float], q: float) -> float | None:
    if not xs:
        return None
    ordered = sorted(xs)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def _round(x: float | None) -> float | None:
    return None if x is None else round(x, 1)


# ------------------------------------------------------------------ the city ---
#: A city to run against with no database at all, so the harness is runnable on
#: a laptop with nothing set up. Shaped like Pune's fleet, not equal to it.
OFFLINE_KINDS = {
    "boat":         {"water_rescue": 1.0, "evacuation": 0.6},
    "rescue_team":  {"water_rescue": 0.8, "extrication": 1.0, "evacuation": 0.5},
    "fire_engine":  {"water_rescue": 0.5, "firefighting": 1.0, "extrication": 0.6},
    "pump":         {"dewatering": 1.0},
    "ambulance":    {"medical": 1.0},
    "bus":          {"mass_transport": 1.0, "evacuation": 0.9, "supply_delivery": 0.45},
    "jcb":          {"debris_clearance": 1.0},
    "supply_truck": {"supply_delivery": 1.0, "mass_transport": 0.4},
    "water_tanker": {"water_supply": 1.0, "supply_delivery": 0.6},
}
OFFLINE_NEEDS = {
    "flooded_road":      ["dewatering"],
    "person_stranded":   ["water_rescue"],
    "structural_damage": ["extrication"],
    "fallen_tree":       ["debris_clearance"],
    "heat_casualty":     ["medical"],
    "supply_shortage":   ["supply_delivery"],
}


def offline_city(seed: int) -> tuple[list[alloc.Unit], list[dict], dict[str, list[str]]]:
    taxonomy.seed_for_tests(kind_capabilities=OFFLINE_KINDS)
    rng = random.Random(seed)
    wards = [
        {"id": f"w{i}", "name": f"Ward {i}", "population": 20_000 + rng.randrange(60_000),
         "lng": 73.80 + rng.random() * 0.18, "lat": 18.45 + rng.random() * 0.22}
        for i in range(1, 41)
    ]
    units: list[alloc.Unit] = []
    per_kind = {"boat": 8, "rescue_team": 6, "fire_engine": 6, "pump": 20,
                "ambulance": 12, "bus": 8, "jcb": 6, "supply_truck": 10,
                "water_tanker": 6}
    n = 0
    for kind, count in per_kind.items():
        for _ in range(count):
            n += 1
            w = rng.choice(wards)
            units.append(alloc.Unit(
                id=f"u{n}", kind=kind, label=f"{kind} {n}", operator="offline",
                location=(w["lng"] + (rng.random() - .5) * .02,
                          w["lat"] + (rng.random() - .5) * .02),
                capacity=4,
            ))
    return units, wards, dict(OFFLINE_NEEDS)


async def load_city(city_id: str) -> tuple[list[alloc.Unit], list[dict], dict[str, list[str]]]:
    # Imported here rather than at module scope so `--offline` runs on a machine
    # with no database driver installed at all.
    from app.db import session as db

    await taxonomy.load()

    units = [
        alloc.Unit(
            id=r["id"], kind=r["kind"], label=r["label"], operator=r["operator"],
            location=(float(r["lng"]), float(r["lat"])), capacity=r["capacity"] or 1,
        )
        for r in await db.fetch(
            """
            select id, kind, label, operator, capacity,
                   extensions.ST_X(coalesce(base_location, location)::extensions.geometry) lng,
                   extensions.ST_Y(coalesce(base_location, location)::extensions.geometry) lat
              from resources where city_id = $1
            """,
            city_id,
        )
    ]

    wards = [
        {"id": r["id"], "name": r["name"], "population": r["population"] or 20_000,
         "lng": float(r["lng"]), "lat": float(r["lat"])}
        for r in await db.fetch(
            """
            select id, name, population,
                   extensions.ST_X(centroid::extensions.geometry) lng,
                   extensions.ST_Y(centroid::extensions.geometry) lat
              from wards where city_id = $1
            """,
            city_id,
        )
    ]

    needs: dict[str, list[str]] = {}
    for r in await db.fetch(
        "select category_id, capability_id from incident_category_needs"
    ):
        needs.setdefault(r["category_id"], []).append(r["capability_id"])

    return units, wards, needs


def build_stream(
    wards: list[dict], needs: dict[str, list[str]], *, minutes: float, seed: int,
    every: float = ARRIVAL_MEAN_MINUTES,
) -> list[Arrival]:
    """One event stream, generated once and replayed into every arm.

    Poisson arrivals, ward chosen with probability proportional to population,
    category chosen uniformly from the ones that declare a need. Severity is
    drawn rather than derived, because deriving it here would smuggle the
    system's own severity model into a test of dispatch.
    """
    rng = random.Random(seed)
    categories = sorted(k for k, v in needs.items() if v)
    if not categories or not wards:
        return []

    weights = [w["population"] for w in wards]
    stream: list[Arrival] = []
    clock = 0.0
    n = 0
    while clock < minutes:
        clock += rng.expovariate(1.0 / every)
        if clock >= minutes:
            break
        ward = rng.choices(wards, weights=weights, k=1)[0]
        category = rng.choice(categories)
        severity = rng.choices([2, 3, 4, 5], weights=[3, 4, 2, 1], k=1)[0]
        # Scatter inside the ward rather than stacking every incident on the
        # centroid, which would make every unit equidistant and flatter the
        # optimiser.
        lng = ward["lng"] + (rng.random() - 0.5) * 0.02
        lat = ward["lat"] + (rng.random() - 0.5) * 0.02
        for capability in needs[category]:
            n += 1
            stream.append(
                Arrival(
                    at=clock,
                    demand=alloc.Demand(
                        id=f"d{n}", ward_id=ward["id"], incident_id=f"i{n}",
                        capability=capability, purpose=category,
                        location=(lng, lat), severity=severity,
                        population_at_risk=int(ward["population"] * 0.1),
                    ),
                )
            )
    return stream


# ---------------------------------------------------------------- the arms ---
def _matrix(units: Sequence[alloc.Unit], demands: Sequence[alloc.Demand]):
    return _fallback_matrix([u.location for u in units], [d.location for d in demands], [])


def _nearest(
    open_demands: list[alloc.Demand],
    free_units: list[alloc.Unit],
) -> list[tuple[alloc.Unit, alloc.Demand, int]]:
    """Closest capable free unit, most severe demand first. No look-ahead.

    Deliberately not the solver in disguise: it takes demands one at a time in
    the order a control room would, and never reconsiders.
    """
    picks: list[tuple[alloc.Unit, alloc.Demand, int]] = []
    remaining = list(free_units)
    for demand in sorted(open_demands, key=lambda d: (-d.severity, d.id)):
        if not remaining:
            break
        matrix = _fallback_matrix([u.location for u in remaining], [demand.location], [])
        best: tuple[int, int] | None = None
        for i, unit in enumerate(remaining):
            if taxonomy.cache.effectiveness(unit.kind, demand.capability) <= 0:
                continue
            eta = matrix.eta(i, 0)
            if eta > alloc.MAX_ETA_MINUTES:
                continue
            if best is None or eta < best[1]:
                best = (i, eta)
        if best is None:
            continue
        unit = remaining.pop(best[0])
        picks.append((unit, demand, best[1]))
    return picks


def run(
    name: str,
    stream: list[Arrival],
    units: list[alloc.Unit],
    *,
    minutes: float,
    revisit: bool,
    optimise: bool,
) -> Outcome:
    """Advance one arm through the whole stream on the shared clock.

    `optimise` picks CP-SAT over nearest-first; `revisit` decides whether a
    committed unit may be moved. The three arms are (False, False),
    (False, True) and (True, True).
    """
    out = Outcome(name=name, total=len({a.demand.id for a in stream}))
    out._born = {a.demand.id: a.at for a in stream}

    pending = sorted(stream, key=lambda a: a.at)
    cursor = 0
    open_demands: dict[str, alloc.Demand] = {}
    served: set[str] = set()
    commitments: dict[str, Commitment] = {}   # unit id -> commitment

    now = 0.0
    # Run past the last arrival so late demands get their chance to be served.
    horizon = minutes + 90.0
    while now <= horizon:
        while cursor < len(pending) and pending[cursor].at <= now:
            d = pending[cursor].demand
            open_demands[d.id] = d
            cursor += 1

        # Arrivals and service completions, before planning: a plan made on a
        # stale picture is a different experiment.
        for unit_id, c in list(commitments.items()):
            if c.free_at is None and now >= c.arrives_at:
                if c.demand_id not in out.arrived_at:
                    out.arrived_at[c.demand_id] = c.arrives_at
                else:
                    # Somebody was already there. This is the dispatch that a
                    # deduplicating system would not have made.
                    out.duplicate_dispatch += 1
                c.free_at = c.arrives_at + SERVICE_MINUTES
                served.add(c.demand_id)
                open_demands.pop(c.demand_id, None)
            if c.free_at is not None and now >= c.free_at:
                del commitments[unit_id]

        wanted = [d for d in open_demands.values() if d.id not in served]
        committed_ids = {c.demand_id for c in commitments.values()}
        if revisit:
            candidates = wanted
            free = list(units)
        else:
            candidates = [d for d in wanted if d.id not in committed_ids]
            free = [u for u in units if u.id not in commitments]

        if candidates and free:
            if optimise:
                current = (
                    {u: c.demand_id for u, c in commitments.items()} if revisit else {}
                )
                progress = (
                    {u: c.progress(now) for u, c in commitments.items()} if revisit else {}
                )
                result = alloc.allocate(
                    candidates, free, _matrix(free, candidates),
                    current=current, progress=progress, time_budget_s=2.0,
                )
                out.engine_solves[result.engine] = (
                    out.engine_solves.get(result.engine, 0) + 1
                )
                picks = [
                    (a.unit, a.demand, a.eta_minutes)
                    for a in result.allocations
                ]
            else:
                picks = _nearest(candidates, free)

            planned = {u.id: d.id for u, d, _ in picks}
            for unit, demand, eta in picks:
                held = commitments.get(unit.id)
                if held is not None:
                    if held.demand_id == demand.id:
                        continue           # already on it; nothing happened
                    if held.free_at is not None:
                        continue           # on scene and working; not movable
                    out.switches += 1
                    # The demand it was pulled off is open again.
                    if held.demand_id not in served:
                        open_demands.setdefault(held.demand_id, _find(stream, held.demand_id))
                commitments[unit.id] = Commitment(
                    unit_id=unit.id, demand_id=demand.id,
                    committed_at=now, arrives_at=now + max(1, eta),
                )
                out.assigned_at.setdefault(demand.id, now)

            if revisit:
                # The plan is authoritative, exactly as it is in the running
                # system: a unit the new plan does not mention has been released
                # and stops driving. Leaving it en route is how you end up with
                # two units converging on one incident and calling it a feature.
                for unit_id, c in list(commitments.items()):
                    if c.free_at is not None or unit_id in planned:
                        continue
                    if planned.get(unit_id) == c.demand_id:
                        continue
                    if any(d == c.demand_id for u, d in planned.items() if u != unit_id):
                        del commitments[unit_id]

        now += CYCLE_MINUTES

    # Anything still in flight at the horizon never arrived; it is counted as
    # assigned but not arrived, which is exactly what it was.
    return out


def _find(stream: Iterable[Arrival], demand_id: str) -> alloc.Demand:
    for a in stream:
        if a.demand.id == demand_id:
            return a.demand
    raise KeyError(demand_id)


# ------------------------------------------------------------------- output ---
def table(rows: list[dict]) -> str:
    cols = [
        ("strategy", "strategy", 12), ("engine", "engine", 16),
        ("demands", "demands", 8),
        ("assigned", "assigned", 9), ("unmet_at_close", "unmet", 6),
        ("time_to_assignment_median", "assign p50", 11),
        ("time_to_assignment_p90", "assign p90", 11),
        ("time_to_arrival_median", "arrive p50", 11),
        ("time_to_arrival_p90", "arrive p90", 11),
        ("duplicate_dispatch", "dupes", 6), ("switches", "switch", 7),
    ]
    head = "  ".join(label.ljust(w) for _, label, w in cols)
    lines = [head, "-" * len(head)]
    for r in rows:
        lines.append("  ".join(
            str(r.get(key, "—") if r.get(key) is not None else "—").ljust(w)
            for key, _, w in cols
        ))
    return "\n".join(lines)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", default="pune")
    ap.add_argument("--minutes", type=float, default=120.0, help="sim minutes of arrivals")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument(
        "--fleet", type=float, default=1.0,
        help="scale the fleet, e.g. 0.3 to run the city three times short. "
             "Scarcity is where dispatch policy stops being a tie.",
    )
    ap.add_argument(
        "--every", type=float, default=ARRIVAL_MEAN_MINUTES,
        help="mean sim minutes between incidents",
    )
    ap.add_argument("--json", help="also write the results to this file")
    ap.add_argument(
        "--offline", action="store_true",
        help="run against a synthetic city instead of the database",
    )
    args = ap.parse_args()

    units, wards, needs = (
        offline_city(args.seed) if args.offline else await load_city(args.city)
    )
    if args.fleet != 1.0:
        keep = max(1, round(len(units) * args.fleet))
        units = random.Random(args.seed).sample(units, keep)
    if not units:
        print(f"No resources for city {args.city!r}. Seed the fleet first.")
        return 1

    stream = build_stream(
        wards, needs, minutes=args.minutes, seed=args.seed, every=args.every
    )
    print(
        f"{len(stream)} demands over {args.minutes:.0f} sim minutes, "
        f"{len(units)} units, {len(wards)} wards, seed {args.seed}.\n"
    )

    arms = [
        ("nearest",     dict(revisit=False, optimise=False)),
        ("oneshot",     dict(revisit=False, optimise=True)),
        ("indradhanu",  dict(revisit=True,  optimise=True)),
    ]
    rows = []
    for name, kwargs in arms:
        outcome = run(name, stream, units, minutes=args.minutes, **kwargs)
        rows.append(outcome.summary())
        print(f"  {name} done")

    print()
    print(table(rows))
    try:
        from ortools.sat.python import cp_model  # noqa: F401
        has_ortools = True
    except ImportError:
        has_ortools = False
    # Asked of the interpreter, not inferred from a label. The old test read the
    # engine of the last solve and cried wolf on runs where CP-SAT had done
    # everything that mattered.
    if not has_ortools:
        print(
            "\nOR-Tools is not installed, so the optimised arms fell back to the "
            "greedy heuristic and this table compares three flavours of greedy. "
            "Install `ortools` before reporting any of these numbers."
        )
    print(
        "\nSimulated arrivals and fixed service times: a fair test of dispatch "
        "policy, not evidence about any real city."
    )

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(
                {"seed": args.seed, "minutes": args.minutes,
                 "units": len(units), "demands": len(stream), "results": rows},
                fh, indent=2,
            )
        print(f"\nWritten to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
