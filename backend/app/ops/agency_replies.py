"""The other agency answers a handoff, and the coordinator acts on the answer.

A request to another agency used to wait forever unless an officer clicked a
button that played the other side. Now the other side replies. The reply is
drawn at random from a set we wrote ourselves (`REPLY_SET`): nobody is
pretending to know what the Red Cross duty officer would actually say, and the
set is the honest stand-in for that person until a real channel exists.

What matters is what the agent does with each kind of answer:

  accept_full          units are staged at that agency's base and join the
                       fleet; the plan is redone so they get assigned.
  accept_partial       the units given join the fleet; the remainder is asked
                       of the next agency that holds the capability.
  delayed              acknowledged, answer again later; for a life-safety
                       capability the agent also asks a second agency (hedge),
                       because a "later" ambulance is not an ambulance.
  need_info            the agent answers with the ward, location, incident and
                       severity, and the agency replies again.
  decline_capacity     the next agency is asked; if nobody else holds the
  decline_jurisdiction capability the gap is escalated to the district EOC.

Every reply and every step is an event (`agency.replied`, `agency.requested`
with `followup_of`, `agency.clarified`, `agency.escalated`), and the exchange is
kept on the request (`replies`), so the handoff board shows the conversation.

Requests made by the surge ladder are answered by `app.surge.service` (they
have an offer with its own acceptance rate) and never get a `reply_due_at`
here, so the two never answer the same request.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.logging import get_logger
from app.db import session as db
from app.world import clock as clocks
from app.world import events as ev

log = get_logger(__name__)

AGENT = "agent:coordinator"

#: Capabilities where waiting is not an answer. A "delayed" reply for one of
#: these makes the agent ask a second agency at once.
LIFE_SAFETY_CAPS = frozenset({"water_rescue", "search_rescue", "medical_transport", "medical_care"})

#: How many requests one shortfall may chain through before the agent stops
#: asking around and escalates.
MAX_CHAIN = 4

#: The reply set. Ours, written for the demo; `{…}` fields are filled from the
#: request. Weights are the chance of each kind on a first answer.
REPLY_SET: dict[str, dict[str, Any]] = {
    "accept_full": {
        "weight": 0.38,
        "texts": [
            "Confirmed. Sending {qty} {kind} from {base}. Crew is rolling now.",
            "Approved by our duty officer. {qty} {kind} released to you from {base}.",
            "Yes, we can cover it. {qty} {kind} leaving {base} now; will report on arrival.",
            "Copy. {qty} {kind} assigned to your request, moving from {base}.",
        ],
    },
    "accept_partial": {
        "weight": 0.18,
        "texts": [
            "We can only spare {given} of the {qty} {kind}; the rest are committed in our own area.",
            "Partial: {given} {kind} released from {base}. We cannot free more without leaving our zone uncovered.",
            "Sending {given} now. The other {rest} are on jobs and we can't pull them.",
        ],
    },
    "delayed": {
        "weight": 0.14,
        "texts": [
            "Acknowledged. All our {kind} are committed right now; earliest release in about {wait} minutes.",
            "Received. We'll free {qty} {kind} once the current job closes, roughly {wait} minutes.",
            "Noted, but our crews are at another site. Expect an answer in {wait} minutes.",
        ],
    },
    "need_info": {
        "weight": 0.10,
        "texts": [
            "Before we commit: exact location, access route and an on-site contact, please.",
            "Need more detail. How many people are affected and is the approach road passable?",
            "Which incident is this for, and how severe? Our duty officer needs it to release units.",
        ],
    },
    "decline_capacity": {
        "weight": 0.15,
        "texts": [
            "Unable to help. Every {kind} we have is already deployed.",
            "Sorry, no spare {kind}. We're stretched in our own jurisdiction.",
            "Declined: our remaining {kind} are held in reserve for our own area.",
        ],
    },
    "decline_jurisdiction": {
        "weight": 0.05,
        "texts": [
            "This is outside our jurisdiction. Please route it through the District EOC.",
            "We can't deploy there without a request from the Collector's office.",
        ],
    },
}

#: Agencies that are real systems answering over their own channel (the drone swarm, see
#: app/drone/swarm_agency.py). They are never answered by the random REPLY_SET and have no DisruptionOps units to
#: stage: an acceptance is recorded as fulfilled with no AID- units.
EXTERNAL_AGENCIES = frozenset({"drone-swarm"})

#: Second answer after a "delayed": mostly they come through.
_AFTER_DELAY = {"accept_full": 0.6, "accept_partial": 0.2, "decline_capacity": 0.2}


# ------------------------------------------------------------------ helpers --
_cols_ok: bool | None = None
_cols_checked = 0.0


async def ready() -> bool:
    """Whether migration 038 is in. Rechecked every minute while it is not, so
    applying the migration needs no restart."""
    global _cols_ok, _cols_checked
    if _cols_ok or (time.monotonic() - _cols_checked < 60 and _cols_ok is not None):
        return bool(_cols_ok)
    _cols_checked = time.monotonic()
    try:
        n = await db.fetchval(
            "select count(*) from information_schema.columns "
            "where table_name = 'agency_requests' and column_name in ('reply_due_at','replies','followup_of')"
        )
        _cols_ok = int(n or 0) == 3
    except Exception:  # noqa: BLE001
        _cols_ok = False
    return bool(_cols_ok)


def _speedup() -> float:
    from app.surge.service import _speedup as surge_speedup

    return surge_speedup()


def _later(minutes: float) -> datetime:
    return datetime.now(UTC) + timedelta(minutes=minutes / _speedup())


def _rng(request_id: str, attempt: int) -> random.Random:
    # Deterministic per request and answer, so a replay of the same run answers
    # the same way, while different requests still differ.
    return random.Random(f"{request_id}:{attempt}")


def _pick(rng: random.Random, weights: dict[str, float]) -> str:
    kinds = list(weights)
    return rng.choices(kinds, weights=[weights[k] for k in kinds], k=1)[0]


def _region(ward_id: str | None) -> str:
    return "ncr" if (ward_id or "").startswith("w-gzb") else "pune"


def _nice(s: str) -> str:
    return s.replace("_", " ")


# --------------------------------------------------------------- scheduling --
async def schedule(request_id: str, *, conn: Any = None, minutes: tuple[float, float] = (3, 12)) -> None:
    """Ask for an answer to arrive in a few (sim-compressed) minutes."""
    if not await ready():
        return
    if await db.fetchval("select to_agency = any($2::text[]) from agency_requests where id = $1::uuid",
                         request_id, list(EXTERNAL_AGENCIES)):
        return                       # an external agency replies over its own channel
    due = _later(random.uniform(*minutes))
    q = "update agency_requests set reply_due_at = $2 where id = $1::uuid"
    if conn is not None:
        await conn.execute(q, request_id, due)
    else:
        await db.execute(q, request_id, due)
    kick(due)


_waiters: set[asyncio.Task] = set()


def kick(due: datetime) -> None:
    """Make sure something processes the reply at `due`, demo running or not."""
    async def _wait() -> None:
        try:
            await asyncio.sleep(max(0.0, (due - datetime.now(UTC)).total_seconds()) + 0.2)
            await process_due()
        except Exception as exc:  # noqa: BLE001
            log.warning("agency_reply_wait_failed", error=str(exc)[:200])

    try:
        t = asyncio.get_running_loop().create_task(_wait())
    except RuntimeError:
        return
    _waiters.add(t)
    t.add_done_callback(_waiters.discard)


async def process_due(limit: int = 10) -> list[dict]:
    """Answer every request whose reply is due. Each is claimed atomically
    (reply_due_at set to null), so the runner tick and a waiter never answer
    the same request twice."""
    if not await ready():
        return []
    out: list[dict] = []
    for _ in range(limit):
        row = await db.fetchrow(
            """
            update agency_requests set reply_due_at = null
             where id = (select id from agency_requests
                          where reply_due_at is not null and reply_due_at <= now()
                            and status in ('requested','acknowledged')
                          order by reply_due_at limit 1
                          for update skip locked)
            returning id::text, incident_id::text incident_id, ward_id, from_agency, to_agency,
                      capability_id, quantity, status, reply_kind, replies, followup_of::text followup_of
            """
        )
        if row is None:
            break
        try:
            out.append(await _answer(dict(row)))
        except Exception as exc:  # noqa: BLE001
            log.warning("agency_reply_failed", request=row["id"], error=str(exc)[:300])
    if out:
        try:
            from app.demo import runner as demo_runner

            demo_runner.state.dirty = True
        except Exception:  # noqa: BLE001
            pass
    return out


# ----------------------------------------------------------------- answering --
async def _answer(req: dict) -> dict:
    replies = req["replies"]
    if isinstance(replies, str):
        replies = json.loads(replies)
    attempt = sum(1 for r in replies if r.get("who") == "agency")
    rng = _rng(req["id"], attempt)

    source = await _source(req["to_agency"], req["capability_id"], req["ward_id"])
    if source is None:
        kind = "decline_capacity"
        text = f"We don't operate {_nice(req['capability_id'])} units."
        return await _apply(req, kind, text, source=None, given=0, replies=replies)

    if req["reply_kind"] == "delayed":
        # While this agency was "later", the hedge may have covered it already.
        covered = await db.fetchval(
            "select coalesce(sum(cardinality(units)), 0) from agency_requests "
            "where followup_of = $1::uuid and status = 'fulfilled'", req["id"])
        if int(covered or 0) >= int(req["quantity"]):
            return await _stand_down(req, replies)
        weights = dict(_AFTER_DELAY)
    else:
        weights = {k: v["weight"] for k, v in REPLY_SET.items()}
        if attempt >= 1:
            weights.pop("need_info", None)
    if int(req["quantity"]) < 2:
        weights.pop("accept_partial", None)
    kind = _pick(rng, weights)

    qty = int(req["quantity"])
    given = qty if kind == "accept_full" else (rng.randint(1, qty - 1) if kind == "accept_partial" else 0)
    wait = rng.choice((15, 20, 25, 30))
    text = rng.choice(REPLY_SET[kind]["texts"]).format(
        qty=qty, given=given, rest=qty - given, wait=wait,
        kind=_nice(source["kind"]) + ("s" if qty != 1 and not source["kind"].endswith("s") else ""),
        base=source["base"],
    )
    return await _apply(req, kind, text, source=source, given=given, replies=replies, wait=wait)


async def record(request_id: str, kind: str, text: str, *, by: str) -> dict:
    """An officer records what the other agency said (a phone call, a radio
    reply). Same next steps as a generated reply, so a real answer and a
    simulated one leave the system in the same state."""
    row = await db.fetchrow(
        "select id::text, incident_id::text incident_id, ward_id, from_agency, to_agency, capability_id, "
        "quantity, status, " + ("reply_kind, replies, followup_of::text followup_of " if await ready()
                                 else "null reply_kind, '[]'::jsonb replies, null followup_of ")
        + "from agency_requests where id = $1::uuid",
        request_id,
    )
    if row is None:
        raise LookupError("No such request.")
    req = dict(row)
    replies = req["replies"]
    if isinstance(replies, str):
        replies = json.loads(replies)
    if await ready():
        await db.execute("update agency_requests set reply_due_at = null where id = $1::uuid", request_id)
    source = await _source(req["to_agency"], req["capability_id"], req["ward_id"])
    given = int(req["quantity"]) if kind == "accept_full" and source else 0
    if kind == "accept_full" and source is None:
        kind, text = "decline_capacity", text or "Recorded as accepted, but this agency has no units of that capability."
    return await _apply(req, kind, text, source=source, given=given, replies=replies, by=by, wait=20)


async def _apply(req: dict, kind: str, text: str, *, source: dict | None, given: int,
                 replies: list, wait: int = 20, by: str | None = None) -> dict:
    now = datetime.now(UTC)
    agency_name = await db.fetchval("select name from agencies where id = $1", req["to_agency"]) or req["to_agency"]
    replies = list(replies) + [{"at": now.isoformat(), "who": "agency", "by": by or agency_name,
                                "kind": kind, "text": text}]
    cap = req["capability_id"]
    units: list[str] = []
    status = req["status"]
    due: datetime | None = None
    steps: list[str] = []
    replan = False

    if kind in ("accept_full", "accept_partial") and source:
        units = [] if source.get("external") else await _stage_units(req, source, given)
        status = "fulfilled"
        replan = bool(units)
        steps.append(f"Staged {len(units)} {_nice(source['kind'])}(s) at {source['base']}; "
                     f"re-planning so the allocator assigns them")
        rest = 0 if source.get("external") else int(req["quantity"]) - len(units)
        if rest > 0:
            steps.append(await _follow_up(req, rest, reason=f"{agency_name} could spare only {len(units)}"))
    elif kind == "delayed":
        status = "acknowledged"
        due = _later(wait)
        steps.append(f"Holding: {agency_name} answers again in ~{wait} min")
        if cap in LIFE_SAFETY_CAPS:
            steps.append(await _follow_up(req, int(req["quantity"]), reason="life-safety need cannot wait; hedging",
                                          hedge=True))
    elif kind == "need_info":
        status = "acknowledged"
        info = await _brief(req)
        replies.append({"at": now.isoformat(), "who": "agent", "by": "Coordinator agent", "kind": "info",
                        "text": info})
        due = _later(random.uniform(3, 8))
        steps.append("Sent the location, access and severity they asked for; awaiting their decision")
        await ev.append(clock=clocks.WALL, kind="agency.clarified", actor=AGENT,
                        subject_type="agency_request", subject_id=req["id"], ward_id=req["ward_id"],
                        payload={"to": req["to_agency"], "text": info})
    else:  # declines
        status = "declined"
        steps.append(await _follow_up(req, int(req["quantity"]),
                                      reason=f"{agency_name} declined ({'jurisdiction' if kind == 'decline_jurisdiction' else 'no capacity'})",
                                      prefer_state=kind == "decline_jurisdiction"))

    next_step = ". ".join(s for s in steps if s)
    responded = status in ("fulfilled", "declined", "acknowledged")
    await db.execute(
        """
        update agency_requests
           set status = $2,
               reply = $3, reply_kind = $4, next_step = $5, replies = $6,
               reply_due_at = $7, units = units || $8::text[],
               responded_at = case when $9::boolean then coalesce(responded_at, now()) else responded_at end,
               responded_by = case when $9::boolean then $10 else responded_by end
         where id = $1::uuid
        """,
        req["id"], status, text, kind, next_step, replies, due, units,
        responded, by or req["to_agency"],
    )
    await ev.append(
        clock=clocks.WALL, kind="agency.replied", actor=f"agency:{req['to_agency']}",
        subject_type="agency_request", subject_id=req["id"], ward_id=req["ward_id"],
        payload={"from": req["from_agency"], "to": req["to_agency"], "capability": cap,
                 "reply": text, "kind": kind, "status": status, "units": units,
                 "next_step": next_step, "recorded_by": by},
    )
    if due:
        kick(due)
    _beat("agency", f"{agency_name}: “{text}” → {next_step or status}")
    if replan:
        from app.ops.operations import _replan_soon

        _replan_soon("agency.fulfilled")
    return {"id": req["id"], "kind": kind, "status": status, "units": units, "nextStep": next_step}


async def _stand_down(req: dict, replies: list) -> dict:
    """The hedge already covered what this agency was going to send later."""
    now = datetime.now(UTC)
    text = "No longer needed: another agency has covered it. Please stand your units down."
    replies = list(replies) + [{"at": now.isoformat(), "who": "agent", "by": "Coordinator agent",
                                "kind": "stand_down", "text": text}]
    step = "Covered by the hedge request; told this agency to stand down"
    await db.execute(
        "update agency_requests set status = 'cancelled', reply_kind = 'stand_down', next_step = $2, "
        "replies = $3, reply_due_at = null where id = $1::uuid",
        req["id"], step, replies)
    await ev.append(clock=clocks.WALL, kind="agency.cancelled", actor=AGENT,
                    subject_type="agency_request", subject_id=req["id"], ward_id=req["ward_id"],
                    payload={"to": req["to_agency"], "reason": step})
    return {"id": req["id"], "kind": "stand_down", "status": "cancelled", "units": [], "nextStep": step}


def _beat(kind: str, text: str) -> None:
    try:
        from app.demo import runner as demo_runner

        if demo_runner.state.running:
            demo_runner.state.beat(kind, text)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------- next steps --
async def _source(agency: str, capability: str, ward_id: str | None) -> dict | None:
    """Which kind of unit this agency would send for this capability, and from
    where. Its standing mutual-aid offer first (that is what it has agreed to
    lend), otherwise one of its own units' bases."""
    if agency in EXTERNAL_AGENCIES:
        return {"external": True, "kind": "drone", "base": "the swarm docks"}
    offer = await db.fetchrow(
        """
        select o.kind, o.stage_lng lng, o.stage_lat lat, coalesce(a.short_name, a.name) || ' base' base
          from aid_offers o
          join agencies a on a.id = o.agency_id
          join resource_kind_capabilities kc on kc.kind_id = o.kind and kc.capability_id = $2
         where o.agency_id = $1 and o.region = $3
         order by o.response_minutes limit 1
        """,
        agency, capability, _region(ward_id),
    )
    if offer:
        return dict(offer)
    own = await db.fetchrow(
        """
        select r.kind, extensions.ST_X(r.base_location::extensions.geometry) lng,
               extensions.ST_Y(r.base_location::extensions.geometry) lat,
               coalesce(a.short_name, a.name) || ' base' base
          from resources r
          join resource_kind_capabilities kc on kc.kind_id = r.kind and kc.capability_id = $2
          join agencies a on a.id = r.agency_id
         where r.agency_id = $1 and r.id not like 'AID-%' and r.base_location is not null
         order by r.id limit 1
        """,
        agency, capability,
    )
    return dict(own) if own else None


async def _stage_units(req: dict, source: dict, n: int) -> list[str]:
    """Put the units the agency released into the fleet, at its base. They are
    `AID-` units, so a demo reset demobilises them and the scarcity drill never
    holds them back."""
    agency = req["to_agency"]
    label = await db.fetchval("select coalesce(short_name, name) from agencies where id = $1", agency) or agency
    made: list[str] = []
    for k in range(n):
        uid = f"AID-HO-{req['id'][:8]}-{k + 1}"
        await db.execute(
            "insert into resources (id, kind, label, operator, base_location, location, capacity, status, "
            "city_id, agency_id, crew_available, fuel_pct, last_reported_at, updated_at, status_note) "
            "select $1, $2, $3, $4, g, g, rk.default_capacity, 'available', 'pune', $5, true, 90, now(), now(), $6 "
            "from (select extensions.ST_SetSRID(extensions.ST_MakePoint($7,$8),4326)::extensions.geography g) x, "
            "resource_kinds rk where rk.id = $2 "
            "on conflict (id) do update set status = 'available', location = excluded.location, "
            "status_note = excluded.status_note, unavailable_reason = null, updated_at = now()",
            uid, source["kind"], f"{label} {_nice(source['kind'])} (handoff) #{k + 1}", label, agency,
            "Released by handoff", float(source["lng"]) + 0.0015 * k, float(source["lat"]),
        )
        made.append(uid)
    return made


async def _asked(req: dict) -> set[str]:
    """Agencies already asked about this shortfall (same incident, or same ward
    when there is no incident, and same capability)."""
    rows = await db.fetch(
        """
        select distinct to_agency from agency_requests
         where capability_id = $1 and status <> 'cancelled'
           and (($2::uuid is not null and incident_id = $2::uuid)
                or ($2::uuid is null and incident_id is null and ward_id = $3))
        """,
        req["capability_id"], req["incident_id"], req["ward_id"],
    )
    return {r["to_agency"] for r in rows} | {req["from_agency"]}


async def _chain_length(req: dict) -> int:
    n = await db.fetchval(
        """
        with recursive up as (
          select id, followup_of, 1 depth from agency_requests where id = $1::uuid
          union all
          select r.id, r.followup_of, up.depth + 1 from agency_requests r join up on r.id = up.followup_of
           where up.depth < 20
        ) select max(depth) from up
        """,
        req["id"],
    )
    return int(n or 1)


async def _next_agency(req: dict, *, prefer_state: bool) -> dict | None:
    """The quickest agency that holds the capability and has not been asked.
    After a jurisdiction refusal, state and national forces first (that is
    who the District EOC would route it to)."""
    asked = await _asked(req)
    rows = await db.fetch(
        """
        select a.id, a.name, min(o.response_minutes) minutes
          from agencies a
          left join aid_offers o on o.agency_id = a.id and o.region = $2
               and exists (select 1 from resource_kind_capabilities kc
                            where kc.kind_id = o.kind and kc.capability_id = $1)
         where (o.id is not null
                or exists (select 1 from resources r
                             join resource_kind_capabilities kc on kc.kind_id = r.kind and kc.capability_id = $1
                            where r.agency_id = a.id and r.id not like 'AID-%'))
         group by a.id, a.name
        """,
        req["capability_id"], _region(req["ward_id"]),
    )
    cands = [dict(r) for r in rows if r["id"] not in asked]
    if not cands:
        return None
    state_forces = {"sdrf", "ndrf", "ndrf8", "army", "iaf", "stateaviation"}
    cands.sort(key=lambda c: ((c["id"] not in state_forces) if prefer_state else 0,
                              c["minutes"] if c["minutes"] is not None else 45))
    return cands[0]


async def _follow_up(req: dict, qty: int, *, reason: str, hedge: bool = False,
                     prefer_state: bool = False) -> str:
    """Ask the next agency, or escalate when there is nobody left to ask."""
    nxt = None
    if await _chain_length(req) < MAX_CHAIN:
        nxt = await _next_agency(req, prefer_state=prefer_state)
    if nxt is None:
        await ev.append(
            clock=clocks.WALL, kind="agency.escalated", actor=AGENT,
            subject_type="agency_request", subject_id=req["id"], ward_id=req["ward_id"],
            payload={"capability": req["capability_id"], "quantity": qty, "reason": reason,
                     "to": "District EOC"},
        )
        return (f"{reason}; no other agency holds {_nice(req['capability_id'])} for this — "
                f"escalated to the District EOC, the need stays on the uncovered list")
    note = (f"Follow-up by the coordinator agent: {reason}. "
            f"{qty} × {_nice(req['capability_id'])} still needed.")
    rid = await db.fetchval(
        """
        insert into agency_requests
          (city_id, incident_id, ward_id, from_agency, to_agency, capability_id, quantity,
           status, note, followup_of)
        values ('pune', $1::uuid, $2, $3, $4, $5, $6, 'requested', $7, $8::uuid)
        returning id::text
        """,
        req["incident_id"], req["ward_id"], req["from_agency"], nxt["id"], req["capability_id"],
        qty, note, req["id"],
    )
    await ev.append(
        clock=clocks.WALL, kind="agency.requested", actor=AGENT,
        subject_type="agency_request", subject_id=rid, ward_id=req["ward_id"],
        payload={"from": req["from_agency"], "to": nxt["id"], "capability": req["capability_id"],
                 "quantity": qty, "followup_of": req["id"], "hedge": hedge, "reason": reason},
    )
    await schedule(rid)
    verb = "Also asked" if hedge else "Asked"
    return f"{verb} {nxt['name']} for {qty} × {_nice(req['capability_id'])} ({reason})"


async def _brief(req: dict) -> str:
    """What the agent tells an agency that asked for details."""
    row = await db.fetchrow(
        """
        select i.title, i.severity, i.report_count, i.street,
               extensions.ST_Y(i.location::extensions.geometry) lat,
               extensions.ST_X(i.location::extensions.geometry) lng,
               w.name ward
          from wards w
          left join incidents i on i.id = $1::uuid
         where w.id = $2
        """,
        req["incident_id"], req["ward_id"],
    )
    if row is None:
        return f"Ward {req['ward_id']}; {req['quantity']} × {_nice(req['capability_id'])}."
    parts = [f"Ward: {row['ward']}"]
    if row["title"]:
        parts.append(f"Incident: {row['title']} (severity {row['severity']}/5)")
    if row["street"]:
        parts.append(f"Street: {row['street']}")
    if row["report_count"]:
        parts.append(f"{row['report_count']} independent report(s) so far")
    if row["lat"] is not None:
        parts.append(f"Location: {row['lat']:.5f}, {row['lng']:.5f}")
    parts.append("Approach: follow the live passable route on the shared map; "
                 "on-site contact is the PMC control room")
    return ". ".join(parts) + "."
