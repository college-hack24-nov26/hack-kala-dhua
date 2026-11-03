"""Mitigation strategies, generated from the world and priced by the solver.

The tempting version of this feature is to ask a language model "what should we
do about the flood" and print the answer. That produces confident, generic,
unfalsifiable advice — preposition resources, coordinate with agencies, monitor
the situation — which is worse than useless in a control room because it sounds
like a plan.

So the division of labour here is strict, and it is the same one the rest of
the system uses:

* **Candidates are generated from state, not from prose.** Every strategy below
  exists because something in the database is true right now: a capability with
  a shortfall, a lifeline projected to saturate, an agency that holds a
  capability we are short of. If none of those is true, no strategy is offered,
  and "hold" is the honest answer.
* **Consequences are computed, not asserted.** Each candidate is run through
  `simulate.compare`, which re-solves the real allocation model. The numbers in
  "expected improvement" come out of CP-SAT.
* **Drawbacks are computed too.** This is the part that makes the feature worth
  having. Any reallocation takes something from somewhere; the simulator knows
  which wards get slower and by how much, and that goes in the drawback list
  whether or not it flatters the recommendation.
* **The model writes the sentence, and only the sentence.** If there is no
  model, the sentence is assembled from the same numbers and nothing is lost
  except some fluency.

Nothing here executes. A strategy is a set of *proposed* actions; applying one
sends each action through the policy gate, which is where authority is decided.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from app.agents import forecast as forecasting
from app.copilot import simulate
from app.core.logging import get_logger
from app.db import session as db
from app import taxonomy

log = get_logger(__name__)

#: Beyond this many candidates the screen stops being a decision and starts
#: being a menu. Three to five is what a commissioner can actually weigh.
MAX_STRATEGIES = 4

#: A unit committed to an incident this severe is not a candidate for being
#: pulled off it, whatever the arithmetic says.
PROTECTED_SEVERITY = 5


@dataclass(slots=True)
class Action:
    """One thing a strategy would do, in the vocabulary the policy gate speaks."""

    action_key: str
    action: str
    target: str
    ward_id: str | None
    #: Everything the executor needs, and nothing it does not.
    params: dict[str, Any] = field(default_factory=dict)
    severity: int = 3

    def as_dict(self) -> dict:
        return {
            "actionKey": self.action_key, "action": self.action,
            "target": self.target, "wardId": self.ward_id,
            "params": self.params, "severity": self.severity,
        }


@dataclass(slots=True)
class Strategy:
    id: str
    title: str
    summary: str
    rationale: str
    actions: list[Action] = field(default_factory=list)
    expected: dict[str, Any] = field(default_factory=dict)
    drawbacks: list[str] = field(default_factory=list)
    risk: str = "medium"
    #: What this rests on, so a reader can check it rather than trust it.
    evidence: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "summary": self.summary,
            "rationale": self.rationale,
            "actions": [a.as_dict() for a in self.actions],
            "expected": self.expected, "drawbacks": self.drawbacks,
            "risk": self.risk, "evidence": self.evidence,
        }


# ------------------------------------------------------------- ingredients ---
async def _worst_uncovered(world: simulate.Inputs) -> list[dict]:
    """Open demands nobody is committed to, worst first.

    This is the list the whole feature turns on: if it is empty there is nothing
    to concentrate on and the honest recommendation is to hold.
    """
    covered = set(world.current.values())
    rows = []
    for d in world.demands:
        if d.incident_id in covered:
            continue
        inc = world.incidents.get(d.incident_id or "", {})
        rows.append({
            "incident_id": d.incident_id, "ward_id": d.ward_id,
            "capability": d.capability, "severity": d.severity,
            "title": inc.get("title") or d.purpose,
            "exposed": d.population_at_risk,
        })
    rows.sort(key=lambda r: (-r["severity"], -r["exposed"]))
    return rows


def _movable(world: simulate.Inputs, capability: str, exclude: set[str]) -> list[str]:
    """Units that could serve this capability and are not doing something worse.

    Free units first, then units committed to lower-severity work. A unit on a
    severity-5 job is never offered, because "we took the boat off the rescue"
    is not a trade-off, it is a mistake.
    """
    kinds = set(taxonomy.cache.kinds_providing(capability))
    free: list[str] = []
    busy: list[tuple[int, str]] = []
    for unit in world.units:
        if unit.id in exclude or unit.kind not in kinds:
            continue
        incident_id = world.current.get(unit.id)
        if incident_id is None:
            free.append(unit.id)
            continue
        severity = int(world.incidents.get(incident_id, {}).get("severity") or 3)
        if severity >= PROTECTED_SEVERITY:
            continue
        busy.append((severity, unit.id))
    busy.sort()
    return free + [u for _, u in busy]


async def _agencies_for(capability: str, city_id: str) -> list[dict]:
    rows = taxonomy.cache.agencies_providing(capability, city_id)
    return [{"id": a.id, "name": a.name} for a in rows]


# -------------------------------------------------------------- candidates ---
async def _concentrate(
    world: simulate.Inputs, uncovered: list[dict], city_id: str
) -> Strategy | None:
    """Put capability where the worst uncovered demand is."""
    if not uncovered:
        return None
    worst = uncovered[0]
    capability = worst["capability"]
    candidates = _movable(world, capability, exclude=set())
    if not candidates:
        return None

    take = candidates[:2]
    pin = {unit_id: worst["incident_id"] for unit_id in take if worst["incident_id"]}
    if not pin:
        return None

    now, proposed = await simulate.compare(
        world, label="Concentrate", pin=pin
    )
    table = simulate.as_table(now, proposed, world)
    labels = {u.id: u.label for u in world.units}
    ward = world.wards.get(worst["ward_id"], worst["ward_id"])

    drawbacks = _drawbacks(table, world, take, proposed)
    return Strategy(
        id="concentrate",
        title=f"Concentrate {capability.replace('_', ' ')} on {ward}",
        summary=(
            f"Commit {', '.join(labels.get(u, u) for u in take)} to "
            f"{worst['title']} and let the solver re-cover everything else."
        ),
        rationale=(
            f"{worst['title']} is severity {worst['severity']} with about "
            f"{worst['exposed']:,} people exposed and nothing committed to it. "
            f"These are the nearest units that can do {capability.replace('_', ' ')} "
            "without being taken off something more serious."
        ),
        actions=[
            Action(
                action_key="reallocate_unit",
                action=f"Move {labels.get(u, u)} to {worst['title']}",
                target=labels.get(u, u), ward_id=worst["ward_id"],
                params={"resource_id": u, "incident_id": worst["incident_id"]},
                severity=int(worst["severity"]),
            )
            for u in take
        ],
        expected=table,
        drawbacks=drawbacks,
        risk="medium" if proposed.switches else "low",
        evidence=[
            f"{len(uncovered)} open demand(s) with nothing committed",
            f"solved with {table['engine']}",
        ],
    )


async def _preposition(
    world: simulate.Inputs, forecast: forecasting.Forecast, city_id: str
) -> Strategy | None:
    """Move capability towards demand that has not happened yet.

    The only strategy here that acts on a prediction rather than on an incident,
    and the only one whose benefit is therefore stated as a projection rather
    than solved. That distinction is kept visible in the text, because a
    commissioner deciding to tie up two ambulances deserves to know which half
    of the argument is arithmetic and which is a forecast.
    """
    short = [d for d in forecast.demand if d.shortfall > 0.5]
    if not short:
        return None
    worst = max(short, key=lambda d: d.shortfall)

    hot = [r for r in forecast.recurrence if r.expected >= 0.5]
    if not hot:
        return None
    ward = max(hot, key=lambda r: r.expected)

    take = _movable(world, worst.capability, exclude=set())[:2]
    if not take:
        return None

    now, proposed = await simulate.compare(world, label="Preposition", withdraw=take)
    table = simulate.as_table(now, proposed, world)
    labels = {u.id: u.label for u in world.units}

    drawbacks = _drawbacks(table, world, take, proposed)
    drawbacks.append(
        f"The benefit is a projection, not a solve: {ward.expected:.1f} incidents "
        f"expected in {ward.ward_name} over the next "
        f"{forecast.horizon_hours:.0f} h, on evidence weight {ward.evidence:.0%}. "
        "If they do not happen, this is two units parked."
    )
    return Strategy(
        id="preposition",
        title=f"Preposition {worst.capability.replace('_', ' ')} in {ward.ward_name}",
        summary=(
            f"Stage {', '.join(labels.get(u, u) for u in take)} in {ward.ward_name} "
            "ahead of demand rather than dispatching them after it."
        ),
        rationale=(
            f"City-wide, about {worst.expected_units:.1f} units of "
            f"{worst.capability.replace('_', ' ')} are expected to be wanted over "
            f"the next {forecast.horizon_hours:.0f} h against {worst.available_now} "
            f"free now — a shortfall of {worst.shortfall:.1f}. {ward.ward_name} is "
            f"the ward most likely to produce it. {ward.explanation}"
        ),
        actions=[
            Action(
                action_key="preposition_equipment",
                action=f"Stage {labels.get(u, u)} in {ward.ward_name}",
                target=labels.get(u, u), ward_id=ward.ward_id,
                params={"resource_id": u, "ward_id": ward.ward_id},
                severity=3,
            )
            for u in take
        ],
        expected=table,
        drawbacks=drawbacks,
        risk="medium",
        evidence=[
            forecast.confidence_note,
            f"evidence weight {ward.evidence:.0%} for {ward.ward_name}",
        ],
    )


async def _protect_lifelines(
    world: simulate.Inputs, forecast: forecasting.Forecast, city_id: str
) -> Strategy | None:
    """Do something about the hospital that is about to fill."""
    pressured = [
        f for f in forecast.facilities
        if f.pressure in ("saturating", "tightening", "full")
    ]
    if not pressured:
        return None
    worst = min(
        pressured,
        key=lambda f: f.hours_to_full if f.hours_to_full is not None else 99.0,
    )
    alternates = [
        f for f in forecast.facilities
        if f.kind == worst.kind and f.id != worst.id and f.pressure == "steady"
    ][:2]

    actions = [
        Action(
            action_key="activate_shelter" if worst.kind != "hospital" else "issue_advisory",
            action=(
                f"Open additional capacity near {worst.name}"
                if worst.kind != "hospital"
                else f"Advise onward routing away from {worst.name}"
            ),
            target=worst.name, ward_id=None,
            params={"lifeline_id": worst.id,
                    "alternates": [a.id for a in alternates]},
            severity=4,
        )
    ]
    return Strategy(
        id="protect_lifelines",
        title=f"Relieve pressure on {worst.name}",
        summary=(
            f"{worst.name} is {worst.pressure}. Route onward arrivals to "
            + (", ".join(a.name for a in alternates) if alternates
               else "the nearest place with room")
            + " before it stops accepting."
        ),
        rationale=worst.explanation,
        actions=actions,
        expected={
            "rows": [
                {"metric": "Spare capacity", "current": worst.spare,
                 "proposed": worst.spare, "change": 0, "better": None},
                {"metric": "Hours to full", "current":
                    round(worst.hours_to_full, 1) if worst.hours_to_full else None,
                 "proposed": None, "change": None, "better": None},
            ],
            "wards": [],
            "engine": "projection",
            "note": "Effect is on where people are sent, which the allocator "
                    "does not model. Stated as a projection, not a solve.",
        },
        drawbacks=[
            "Longer journeys for anybody redirected, and the alternates start "
            "filling sooner than they otherwise would.",
            "Does nothing about the demand itself.",
        ]
        + ([] if alternates else [
            "No comparable facility with spare capacity is recorded, so there is "
            "nowhere specific to send people. This needs a person."
        ]),
        risk="high" if not alternates else "medium",
        evidence=[worst.explanation, forecast.confidence_note],
    )


async def _mutual_aid(
    world: simulate.Inputs, forecast: forecasting.Forecast, city_id: str
) -> Strategy | None:
    """Ask somebody who has what we do not."""
    short = [d for d in forecast.demand if d.shortfall > 1.0 and d.available_now == 0]
    if not short:
        short = [d for d in forecast.demand if d.shortfall > 2.0]
    if not short:
        return None
    worst = max(short, key=lambda d: d.shortfall)
    holders = await _agencies_for(worst.capability, city_id)
    if not holders:
        return None

    return Strategy(
        id="mutual_aid",
        title=f"Request {worst.capability.replace('_', ' ')} from {holders[0]['name']}",
        summary=(
            f"We are {worst.shortfall:.0f} units short of "
            f"{worst.capability.replace('_', ' ')} and "
            f"{holders[0]['name']} holds it."
        ),
        rationale=(
            f"{worst.expected_units:.1f} units expected to be wanted, "
            f"{worst.available_now} free, {worst.committed_now} already committed. "
            "Reallocation cannot close a gap this size; another agency can."
        ),
        actions=[
            Action(
                action_key="request_mutual_aid",
                action=f"Request {worst.capability.replace('_', ' ')} support",
                target=holders[0]["name"], ward_id=None,
                params={"agency_id": holders[0]["id"],
                        "capability": worst.capability,
                        "quantity": max(1, int(round(worst.shortfall)))},
                severity=4,
            )
        ],
        expected={
            "rows": [
                {"metric": f"{worst.capability.replace('_', ' ')} available",
                 "current": worst.available_now,
                 "proposed": worst.available_now + max(1, int(round(worst.shortfall))),
                 "change": max(1, int(round(worst.shortfall))), "better": True},
            ],
            "wards": [], "engine": "projection",
            "note": "Assumes the request is granted in full, which is the "
                    "optimistic case and should be read as one.",
        },
        drawbacks=[
            "Not ours to decide: this waits on another organisation, and the "
            "waiting is not modelled here.",
            "Arrival time depends on where their units are, which we do not hold.",
        ],
        risk="medium",
        evidence=[f"{len(holders)} agency/agencies hold this capability"],
    )


def _hold(world: simulate.Inputs, now: simulate.Scenario) -> Strategy:
    """Do nothing, with its consequences stated like any other option.

    Present unconditionally. A list of three interventions with no null option
    is a list that has already decided.
    """
    return Strategy(
        id="hold",
        title="Hold the current plan",
        summary="Change nothing and let the running allocation finish.",
        rationale=(
            f"{now.assigned} demand(s) covered, {now.unmet} not, median ETA "
            f"{now.eta_median if now.eta_median is not None else '—'} min. "
            "Every alternative below moves a unit that is already committed "
            "somewhere, and this is the option that does not."
        ),
        actions=[],
        expected={
            "rows": [
                {"metric": "Unmet demand", "current": now.unmet,
                 "proposed": now.unmet, "change": 0, "better": None},
                {"metric": "Median ETA (min)", "current": now.eta_median,
                 "proposed": now.eta_median, "change": 0, "better": None},
            ],
            "wards": [], "engine": now.engine, "note": "",
        },
        drawbacks=(
            [f"{now.unmet} demand(s) stay uncovered."] if now.unmet else
            ["Nothing is uncovered right now, so there is little to gain by moving units."]
        ),
        risk="low",
        evidence=["the current solve, unmodified"],
    )


# ------------------------------------------------------------------ shared ---
def _drawbacks(
    table: dict, world: simulate.Inputs, taken: Sequence[str], proposed: simulate.Scenario
) -> list[str]:
    """What this costs, in the words of the wards that pay for it."""
    out: list[str] = []
    worse = [w for w in table["wards"] if w.get("better") is False]
    for row in worse[:3]:
        if row["current"] is None:
            out.append(f"{row['ward']} gains cover it did not have — no cost there.")
        elif row["proposed"] is None:
            out.append(f"{row['ward']} loses its cover entirely.")
        else:
            out.append(
                f"{row['ward']} slows from {row['current']} to {row['proposed']} "
                f"minutes (+{row['change']:.0f})."
            )
    if proposed.switches:
        labels = {u.id: u.label for u in world.units}
        out.append(
            f"{proposed.switches} unit(s) turn around mid-journey"
            + (f", including {labels.get(taken[0], taken[0])}" if taken else "")
            + ". Time already travelled is spent."
        )
    unmet_row = next((r for r in table["rows"] if r["metric"] == "Unmet demand"), None)
    if unmet_row and (unmet_row.get("change") or 0) > 0:
        out.append(
            f"Unmet demand rises by {unmet_row['change']:.0f}: this covers the "
            "worst call by leaving others."
        )
    if not out:
        out.append("No ward gets slower in the solve. Worth re-checking after the move.")
    return out


# -------------------------------------------------------------------- main ---
async def generate(city_id: str = "pune") -> dict[str, Any]:
    """Every strategy the current world actually supports, priced.

    Returns the strategies plus a comparison matrix across them, which is the
    thing a commissioner reads first: same metrics, one column per option.
    """
    world = await simulate.inputs(city_id)
    now = await simulate.baseline(world)

    try:
        forecast = await forecasting.build(city_id=city_id)
    except Exception as exc:  # noqa: BLE001 - a missing forecast loses options, not the feature
        log.warning("strategy_forecast_failed", error=str(exc)[:200])
        forecast = None

    uncovered = await _worst_uncovered(world)

    built: list[Strategy | None] = [_hold(world, now)]
    built.append(await _concentrate(world, uncovered, city_id))
    if forecast is not None:
        built.append(await _preposition(world, forecast, city_id))
        built.append(await _protect_lifelines(world, forecast, city_id))
        built.append(await _mutual_aid(world, forecast, city_id))

    strategies = [s for s in built if s is not None][:MAX_STRATEGIES + 1]

    return {
        "generatedAt": world.now.isoformat(),
        "baseline": {
            "assigned": now.assigned, "unmet": now.unmet,
            "coverage": now.coverage, "etaMedian": now.eta_median,
            "etaP90": now.eta_p90, "engine": now.engine,
        },
        "strategies": [s.as_dict() for s in strategies],
        "matrix": _matrix(strategies),
        "note": (
            "Candidates come from the current state; consequences come from "
            "re-solving the real allocation model. Nothing here has been done."
        ),
    }


def _matrix(strategies: Sequence[Strategy]) -> dict[str, Any]:
    """One row per metric, one column per strategy."""
    metrics = ["Unmet demand", "Median ETA (min)", "Units moved off a job"]
    rows = []
    for metric in metrics:
        row: dict[str, Any] = {"metric": metric}
        for s in strategies:
            cell = next(
                (r for r in s.expected.get("rows", []) if r["metric"] == metric), None
            )
            row[s.id] = cell.get("proposed") if cell else None
        rows.append(row)
    rows.append({"metric": "Risk", **{s.id: s.risk for s in strategies}})
    rows.append({"metric": "Actions", **{s.id: len(s.actions) for s in strategies}})
    return {
        "columns": [{"id": s.id, "title": s.title} for s in strategies],
        "rows": rows,
    }


async def by_id(strategy_id: str, city_id: str = "pune") -> Strategy | None:
    """Re-generate and pick one out.

    Deliberately re-generated rather than cached: a strategy priced ninety
    seconds ago is priced against a world that has moved, and applying it should
    be applying what it says now.
    """
    built = await generate(city_id)
    for raw in built["strategies"]:
        if raw["id"] == strategy_id:
            return Strategy(
                id=raw["id"], title=raw["title"], summary=raw["summary"],
                rationale=raw["rationale"],
                actions=[
                    Action(
                        action_key=a["actionKey"], action=a["action"],
                        target=a["target"], ward_id=a["wardId"],
                        params=a["params"], severity=a["severity"],
                    )
                    for a in raw["actions"]
                ],
                expected=raw["expected"], drawbacks=raw["drawbacks"],
                risk=raw["risk"], evidence=raw["evidence"],
            )
    return None


async def wards_by_name(city_id: str = "pune") -> dict[str, str]:
    return {
        r["name"].lower(): r["id"]
        for r in await db.fetch("select id, name from wards where city_id = $1", city_id)
    }
