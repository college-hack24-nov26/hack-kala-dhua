"""Move equipment before it is needed — as a proposal, never as a move.

The forecast already knows two things it was never asked to act on: how often a
category of incident happens in a ward, and how many units of each capability
are likely to be wanted city-wide over the horizon. `DemandForecast.shortfall`
is literally "expected demand minus what is free right now". Until this module
existed, that number was drawn on a screen and nothing was done with it.

What this does is the smallest useful act: when a capability is projected short,
it proposes moving spare units of that capability toward the ward most likely to
need them, and puts the proposal through the same delegation gate as everything
else.

Three limits, held deliberately
-------------------------------

**It proposes; it never moves.** `preposition_equipment` is already authorised
by PMC DMP 2023 cl. 6.1 to the Ward Officer, so most of these will auto-issue
under that clause — but issuing a decision is not the same as driving a lorry.
The decision names the units and the ward, an officer sees it in the gate, and
the existing approval path carries it out. Nothing here writes to `resources`.

**It never touches a committed unit.** Prepositioning competes with live
dispatch, and live dispatch wins every time: a boat on its way to somebody in
water is not spare capacity, whatever the forecast says about the next three
hours. Only `available` units are candidates.

**It says how much of the answer is real.** Every proposal carries the evidence
figure from the recurrence it was built on. A forecast resting on the city-wide
prior rather than on this ward's own record says so in the rationale, because an
officer deciding whether to move a pump at 2 a.m. should be told the difference
between "this ward floods every monsoon" and "we have no idea, so we guessed
from the city average".

The threshold exists for the same reason. A shortfall of 0.3 units is noise
dressed as a decision, and an officer who learns that these proposals are noise
will stop reading the ones that are not.
"""

from __future__ import annotations

from typing import Any

from app.agents import forecast as forecasting
from app.agents import gate
from app.core.logging import get_logger
from app.db import session as db
from app.world import clock as clocks

log = get_logger(__name__)

#: Below this projected shortfall, say nothing. Rounded units, not a fraction of
#: one: "we may be half a pump short" is not an instruction anybody can act on.
MIN_SHORTFALL_UNITS = 1.0

#: At most this many proposals per run. The gate is a queue an officer reads,
#: and a queue nobody finishes is the same as no queue.
MAX_PROPOSALS = 3

#: How many spare units one proposal may move. Prepositioning that empties a
#: depot has turned a projection into a gamble.
MAX_UNITS_PER_MOVE = 2


#: Spare units of a capability, nearest first to the ward we would send them to.
#:
#: Two things this query is careful about. `status = 'available'` is the whole
#: safety argument: a committed unit is not spare, and a query that forgot it
#: would quietly re-task live rescues into a forecast. And capability is reached
#: through `resource_kind_capabilities` rather than read off the resource,
#: because that is where capability actually lives — a boat is eligible for
#: water rescue by being a boat, and no row about an individual unit can grant
#: it something its kind cannot do.
_SPARE_SQL = """
with target as (
  select centroid from wards where id = $2
)
select r.id, r.label, r.kind, r.operator,
       round((st_distance(r.location::geography, target.centroid::geography)
              / 1000.0)::numeric, 1) as km
  from resources r
  join resource_kind_capabilities rkc on rkc.kind_id = r.kind
  cross join target
 where r.city_id = $3
   and r.status = 'available'
   and rkc.capability_id = $1
 order by r.location <-> target.centroid
 limit $4
"""

#: What this capability's past proposals were worth. Laplace-smoothed in SQL, so
#: a capability with no record returns 0.5 — "no opinion" — rather than looking
#: either certain or hopeless on one data point.
_HIT_RATE_SQL = "select app.preposition_hit_rate($1, $2) as rate"

#: Record the proposal so the horizon can be judged against it later. Written in
#: the same breath as the decision: a proposal that is not recorded is a
#: prediction nobody ever has to answer for.
_RECORD_SQL = """
insert into preposition_outcomes
       (city_id, decision_id, capability_id, ward_id, unit_ids,
        shortfall, evidence, horizon_hours)
values ($1, $2, $3, $4, $5, $6, $7, $8)
"""

#: Settle every proposal whose horizon has passed: was any unit it named
#: actually tasked in that ward? `was_used` stays null until then, because
#: counting a proposal made ten minutes ago as a miss would punish it for being
#: recent.
_RESOLVE_SQL = """
update preposition_outcomes o
   set was_used = exists (
         select 1
           from assignments a
           join incidents i on i.id = a.incident_id
          where a.resource_id = any(o.unit_ids)
            and i.ward_id = o.ward_id
            and a.created_at between o.proposed_at
                                and o.proposed_at + make_interval(hours => o.horizon_hours::int)
       ),
       used_at = (
         select min(a.created_at)
           from assignments a
           join incidents i on i.id = a.incident_id
          where a.resource_id = any(o.unit_ids) and i.ward_id = o.ward_id
            and a.created_at >= o.proposed_at
       ),
       resolved_at = now()
 where o.was_used is null
   and now() > o.proposed_at + make_interval(hours => o.horizon_hours::int)
 returning o.id
"""

#: Which ward most wants this capability over the horizon. Joins the learned
#: recurrence to the capability each category actually needs, rather than
#: assuming a pump belongs wherever the last flood was.
_CATEGORIES_FOR_CAPABILITY_SQL = """
select distinct category_id
  from incident_category_needs
 where capability_id = $1
"""


async def propose_all(
    *,
    city_id: str = "pune",
    clock: clocks.Clock = clocks.WALL,
    fc: forecasting.Forecast | None = None,
) -> list[dict[str, Any]]:
    """Look at the forecast and propose what should move. Returns the decisions.

    `fc` is injectable so a caller that has just built a forecast for its own
    screen does not pay for a second one.
    """
    # Settle what is now answerable before proposing anything new, so this pass
    # is scored against the most complete record available.
    settled = await db.fetch(_RESOLVE_SQL)
    if settled:
        log.info("preposition_resolved", city=city_id, settled=len(settled))

    fc = fc or await forecasting.build(city_id=city_id)

    short = [d for d in fc.demand if d.shortfall >= MIN_SHORTFALL_UNITS]
    if not short:
        log.info("preposition_none", city=city_id, reason="no capability short")
        return []

    # Worst shortfall first. An officer reading three proposals should read the
    # one that matters most at the top.
    short.sort(key=lambda d: d.shortfall, reverse=True)

    decisions: list[dict[str, Any]] = []
    for demand in short[:MAX_PROPOSALS]:
        decision = await _propose_one(demand, fc, city_id=city_id, clock=clock)
        if decision is not None:
            decisions.append(decision)

    log.info(
        "preposition_proposed",
        city=city_id,
        short_capabilities=len(short),
        proposed=len(decisions),
    )
    return decisions


async def _propose_one(
    demand: forecasting.DemandForecast,
    fc: forecasting.Forecast,
    *,
    city_id: str,
    clock: clocks.Clock,
) -> dict[str, Any] | None:
    target = await _neediest_ward(demand.capability, fc)
    if target is None:
        # The capability is short city-wide but no ward's own record points
        # anywhere in particular. Moving equipment on that basis is choosing a
        # destination at random and calling it a forecast.
        log.info("preposition_skipped", capability=demand.capability,
                 reason="no ward attributable")
        return None

    ward_id, ward_name, evidence, explanation = target

    units = await db.fetch(
        _SPARE_SQL, demand.capability, ward_id, city_id, MAX_UNITS_PER_MOVE
    )
    if not units:
        # Nothing spare to move. This is not a failure and must not read as one:
        # it is the scarcity the allocator is already solving under, and the
        # honest response is to say nothing rather than propose an impossible
        # move.
        log.info("preposition_skipped", capability=demand.capability,
                 reason="no spare units")
        return None

    named = ", ".join(f"{u['label']} ({u['km']} km)" for u in units)
    shortfall = demand.shortfall

    rationale = (
        f"{demand.capability.replace('_', ' ')} is projected {shortfall:.1f} "
        f"unit(s) short over the next {fc.horizon_hours:.0f} h: "
        f"{demand.expected_units:.1f} expected against {demand.available_now} "
        f"free and {demand.committed_now} already committed. "
        f"{ward_name} is the likeliest call — {explanation} "
        f"Proposes moving {named}. "
        f"Confidence in the ward is {evidence:.0%} this ward's own history, "
        f"{1 - evidence:.0%} the city-wide rate. {fc.confidence_note}"
    )

    # Severity is the shortfall expressed on the 1-5 scale the rest of the
    # system reads, capped at 4: prepositioning is preparation, and a decision
    # that presents as a life-safety emergency when nothing has happened yet
    # devalues the ones that are.
    severity = max(1, min(4, int(round(shortfall)) + 1))

    # Confidence combines two different kinds of doubt, and they are not
    # interchangeable. `evidence` asks how much of the ward choice came from this
    # ward's own history rather than the city prior. `hit_rate` asks whether
    # prepositioning this capability has been worth doing before — the learned
    # half, which starts at 0.5 and moves only as proposals are settled against
    # what units actually did. A capability that keeps being proposed and never
    # used drifts down until it needs a human to agree, which is the loop
    # working rather than the loop failing.
    rate_row = await db.fetchrow(_HIT_RATE_SQL, demand.capability, city_id)
    hit_rate = float(rate_row["rate"]) if rate_row and rate_row["rate"] is not None else 0.5
    confidence = round(0.30 + 0.40 * evidence + 0.30 * hit_rate, 3)

    decision = await gate.propose(
        action_key="preposition_equipment",
        action=(
            f"Move {len(units)} {demand.capability.replace('_', ' ')} unit(s) "
            f"toward {ward_name}"
        ),
        target=ward_name,
        ward_id=ward_id,
        rationale=rationale,
        confidence=confidence,
        severity=severity,
        city_id=city_id,
        clock=clock,
        actor="agent:forecast:preposition",
        params={
            "capability": demand.capability,
            "wardId": ward_id,
            "unitIds": [u["id"] for u in units],
            "unitLabels": [u["label"] for u in units],
            "shortfall": round(shortfall, 2),
            "expectedUnits": round(demand.expected_units, 2),
            "availableNow": demand.available_now,
            "committedNow": demand.committed_now,
            "evidence": round(evidence, 3),
            "hitRate": hit_rate,
            "horizonHours": fc.horizon_hours,
        },
    )

    await db.execute(
        _RECORD_SQL, city_id, str(decision.get("id") or decision.get("decisionId") or ""),
        demand.capability, ward_id, [u["id"] for u in units],
        round(shortfall, 2), round(evidence, 3), fc.horizon_hours,
    )
    return decision


async def _neediest_ward(
    capability: str, fc: forecasting.Forecast
) -> tuple[str, str, float, str] | None:
    """The ward with the largest expected count among categories needing this.

    Weighted by expected incidents rather than by probability: two near-certain
    incidents want a unit more than one near-certain incident does, and
    `p_at_least_one` cannot tell those apart.
    """
    rows = await db.fetch(_CATEGORIES_FOR_CAPABILITY_SQL, capability)
    categories = {r["category_id"] for r in rows}
    if not categories:
        return None

    best: forecasting.Recurrence | None = None
    totals: dict[str, float] = {}
    for rec in fc.recurrence:
        if rec.category not in categories:
            continue
        totals[rec.ward_id] = totals.get(rec.ward_id, 0.0) + rec.expected
        if best is None or rec.expected > best.expected:
            best = rec

    if best is None or best.expected <= 0:
        return None

    # The ward with the highest *total* across all relevant categories, using
    # the single strongest recurrence for the human-readable reason.
    ward_id = max(totals, key=lambda k: totals[k])
    anchor = next(
        (r for r in fc.recurrence
         if r.ward_id == ward_id and r.category in categories),
        best,
    )
    return anchor.ward_id, anchor.ward_name, anchor.evidence, anchor.explanation
