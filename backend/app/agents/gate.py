"""One decision, through the policy gate, at any point in a run.

The hazard orchestrator already gates its proposals, but it only runs once, at
the start. Everything that happened afterwards — an incident opening, a ward
crossing a severity threshold, a crew reporting a road impassable — produced
assignments and no decisions at all, so the decision gate and the alerts screen
sat empty through an entire live run while the map filled up.

This is that same gate, extracted so anything can call it:

    authority -> gate -> decision row -> (if authorised) alert row

Two things it deliberately does not do. It does not decide *whether* an action
is warranted; the caller does, from the world, and passes a confidence. And it
never promotes an action past its clause: `requisition_ndrf` is reserved to the
District Disaster Management Authority at every severity, so it waits for a
person no matter how certain the system is. That refusal is the product.
"""

from __future__ import annotations

from typing import Any

from app.agents import policy
from app.core.logging import get_logger
from app.db import session as db
from app.schemas.domain import DecisionStatus
from app.world import clock as clocks
from app.world import events as ev

log = get_logger(__name__)

#: Alerts are a consequence of an authorised public-communication decision, and
#: of nothing else. A dispatch does not buzz anybody's phone.
ALERTING = ("issue_advisory", "issue_warning")


async def propose(
    *,
    action_key: str,
    action: str,
    target: str,
    ward_id: str | None,
    rationale: str,
    confidence: float,
    severity: int,
    hazard: str = "flood",
    city_id: str = "pune",
    clock: clocks.Clock = clocks.WALL,
    actor: str = "agent:policy_retriever",
    caused_by: int | None = None,
    params: dict | None = None,
    conn: Any | None = None,
) -> dict:
    """Record a proposed action, gated. Returns the decision as the API shows it.

    `params` is what the action would need in order to be carried out — which
    unit, which incident, which ward. It is stored with the decision rather than
    re-derived at approval time, because a decision approved twenty minutes
    later must move the unit it named, not whichever unit the world would
    nominate now.
    """
    authority = await policy.authority_for(action_key, severity)
    status = policy.gate(authority, confidence)
    reason = policy.gate_reason(authority, confidence)
    now = clock.now()

    async def _run(c: Any) -> dict:
        row = await c.fetchrow(
            """
            insert into decisions
              (hazard, action_key, action, target, ward_id, rationale,
               confidence, authority, status, sim_run_id, created_at, params)
            values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::uuid,$11,$12)
            returning id::text
            """,
            hazard, action_key, action, target, ward_id,
            f"{rationale} {reason}",
            round(float(confidence), 4),
            authority.model_dump(),
            status.value, clock.sim_run_id, now, params or {},
        )
        decision_id = row["id"]

        proposed = await ev.append(
            clock=clock, kind=ev.Kind.DECISION_PROPOSED, actor=actor,
            subject_type="decision", subject_id=decision_id, city_id=city_id,
            ward_id=ward_id,
            payload={"action_key": action_key, "action": action,
                     "confidence": round(float(confidence), 4)},
            caused_by=caused_by, conn=c,
        )
        await ev.append(
            clock=clock, kind=ev.Kind.DECISION_GATED, actor=actor,
            subject_type="decision", subject_id=decision_id, city_id=city_id,
            ward_id=ward_id,
            payload={"action_key": action_key, "status": status.value,
                     "clause": authority.clause,
                     "within_delegation": authority.within_delegation,
                     "reason": reason},
            caused_by=proposed.id, conn=c,
        )

        alert_id = None
        if status is DecisionStatus.AUTO_ISSUED and action_key in ALERTING and ward_id:
            alert_id = await _issue_alert(
                c, decision_id=decision_id, ward_id=ward_id, hazard=hazard,
                severity=severity, action=action, city_id=city_id, clock=clock,
                caused_by=proposed.id,
            )

        return {
            "id": decision_id,
            "params": params or {},
            "action": action,
            "actionKey": action_key,
            "target": target,
            "wardId": ward_id,
            "status": status.value,
            "clause": authority.clause,
            "delegatedTo": authority.delegated_to,
            "withinDelegation": authority.within_delegation,
            "reason": reason,
            "alertId": alert_id,
        }

    if conn is not None:
        return await _run(conn)
    async with db.transaction() as c:
        return await _run(c)


async def _issue_alert(
    conn: Any,
    *,
    decision_id: str,
    ward_id: str,
    hazard: str,
    severity: int,
    action: str,
    city_id: str,
    clock: clocks.Clock,
    caused_by: int | None,
) -> str | None:
    """Compose the public message, and name a real place to go.

    The safe location is a row in `lifelines` with room in it, not a direction.
    "Move to higher ground" is what a system says when it does not know where
    anybody should actually go.
    """
    ward = await conn.fetchrow(
        """
        select w.name,
               extensions.ST_X(w.centroid::extensions.geometry) lng,
               extensions.ST_Y(w.centroid::extensions.geometry) lat,
               coalesce(r.population_at_risk, w.population / 8) reach
          from wards w
          left join lateral (
            select population_at_risk from ward_risks
             where ward_id = w.id order by created_at desc limit 1
          ) r on true
         where w.id = $1
        """,
        ward_id,
    )
    if ward is None:
        return None

    shelter = await conn.fetchrow(
        """
        select name,
               extensions.ST_X(location::extensions.geometry) lng,
               extensions.ST_Y(location::extensions.geometry) lat,
               extensions.ST_Distance(
                 location,
                 extensions.ST_SetSRID(extensions.ST_MakePoint($1,$2),4326)::extensions.geography
               ) / 1000.0 km
          from lifelines
         where kind = 'shelter' and city_id = $3
           and status not in ('full','closed')
           and coalesce(occupancy,0) < coalesce(capacity, 2147483647)
         order by km asc limit 1
        """,
        float(ward["lng"]), float(ward["lat"]), city_id,
    )

    safe = None
    instruction = (
        "Stay off low-lying roads and underpasses and move to the highest floor "
        "available. Keep your phone charged."
    )
    if shelter:
        safe = {
            "name": shelter["name"],
            "location": [float(shelter["lng"]), float(shelter["lat"])],
            "distance_km": round(float(shelter["km"]), 2),
        }
        instruction = (
            f"Move to {shelter['name']}, {safe['distance_km']} km away, before "
            "water reaches the road. Take medicines and identity documents."
        )

    row = await conn.fetchrow(
        """
        insert into alerts
          (decision_id, ward_id, hazard, severity, headline, action, by_time,
           safe_location, channels, language, reach, issued_at, sim_run_id)
        values ($1::uuid,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::uuid)
        returning id::text
        """,
        decision_id, ward_id, hazard, severity,
        f"{action} for {ward['name']}",
        instruction,
        clock.now(), safe, ["push", "sms"], "English",
        int(ward["reach"] or 0), clock.now(), clock.sim_run_id,
    )
    await ev.append(
        clock=clock, kind=ev.Kind.ALERT_ISSUED, actor=ev.agent("guidance_agent"),
        subject_type="alert", subject_id=row["id"], city_id=city_id,
        ward_id=ward_id,
        payload={"severity": severity, "reach": int(ward["reach"] or 0),
                 "decision_id": decision_id, "audience": ward["name"]},
        caused_by=caused_by, conn=conn,
    )
    return row["id"]
