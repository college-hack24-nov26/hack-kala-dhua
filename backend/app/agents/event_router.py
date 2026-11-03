"""Event router: the always-on listener that turns events into replans.

Before this, the only thing that noticed a change in the world was the demo
runner's tick. With the demo stopped, a citizen report or a crew's "road
blocked" was written to the event log and nothing re-planned. The runner is a
simulator; whether the system reacts must not depend on it.

What it does
------------
Tails the append-only `events` table by id (every write in the system already
lands there, inside the same transaction as the change it describes), and for
each new event decides:

  * re-plan?   new or changed incident, road blocked or cleared, unit status,
               field status, officer cancel / hold / override, incident closed
  * wake the Incident Commander?   a severe incident (severity >= 4) or a road
               block, debounced per key inside `commander.nudge`

Re-plans are coalesced and serialised: a burst of twenty reports is one
re-plan, and two re-plans never write the same rows at once. While the demo
runner is running, the request is handed to it (it holds the world lock and
re-plans on its next tick), so the two never fight.

Events the planner itself writes (plan.generated, assignment.*) are ignored by
kind and by actor, so a re-plan cannot trigger another re-plan.

This is the in-process form of the production design, where the same routing
table consumes Kafka topics. Every replica starts it, but only the replica
holding the `event_router` lease (app/core/lease.py) polls and re-plans; the
others stand by and take over within one lease period if it dies, resuming
from the cursor the leader stored in the lease.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.db import session as db

log = get_logger(__name__)

#: Kinds that change what the plan should be.
REPLAN_KINDS = {
    "incident.opened", "incident.severity_changed", "incident.resolved",
    "road.blocked", "road.cleared",
    "resource.status_changed",
    # a free unit moved more than a kilometre (app/units): its distance to every
    # open need changed, so the plan may no longer be the best one
    "unit.moved",
}
#: Prefixes that do the same (field.on_site, field.route_blocked, ...).
REPLAN_PREFIXES = ("field.", "override.")
#: Written by the planner, or by the demo runner about its own simulated world
#: (it re-plans that itself): reacting to them would loop or double-plan.
PLANNER_ACTORS = ("agent:allocation_planner", "agent:event_router", "agent:replan", "agent:demo:",
                  "agent:langgraph")
IGNORED_KINDS = {
    "plan.generated", "assignment.created", "assignment.changed",
    "assignment.rerouted", "demand.uncovered", "task.created", "task.updated",
    "agent.step", "agent.episode_started", "agent.episode_finished",
}
SEVERE = 4
DEBOUNCE_S = 1.0


@dataclass(slots=True)
class Route:
    replan: str | None = None                 # why, if a re-plan is needed
    nudge: dict[str, Any] | None = None       # commander.nudge kwargs, if any


def _payload(p: Any) -> dict:
    if isinstance(p, dict):
        return p
    if isinstance(p, str):
        try:
            return json.loads(p)
        except ValueError:
            return {}
    return {}


def route(ev: dict[str, Any]) -> Route:
    """Pure: what one event should cause. Unit-tested without a database."""
    kind = str(ev.get("kind") or "")
    actor = str(ev.get("actor") or "")
    p = _payload(ev.get("payload"))
    out = Route()
    if kind in IGNORED_KINDS or actor.startswith(PLANNER_ACTORS):
        return out

    ward = ev.get("ward_id") or p.get("wardId") or p.get("ward_id") or ""
    if kind in REPLAN_KINDS or kind.startswith(REPLAN_PREFIXES):
        out.replan = f"{kind}{' in ' + ward if ward else ''}"
    elif kind == "assignment.cancelled" and not actor.startswith("agent:"):
        out.replan = "assignment cancelled by an officer"

    try:
        severity = int(p.get("severity") or 0)
    except (TypeError, ValueError):
        severity = 0
    if kind in ("incident.opened", "incident.severity_changed") and severity >= SEVERE:
        out.nudge = {
            "kind": kind,
            "summary": f"Severity {severity} {p.get('category') or 'incident'}"
                       f"{' in ' + ward if ward else ''}",
            "key": f"incident:{ev.get('subject_id')}",
            # Tags for the hazard x sector agent board.
            "ward_id": ward or None, "category": p.get("category"),
        }
    elif kind in ("road.blocked", "field.route_blocked"):
        out.nudge = {
            "kind": "road.blocked",
            "summary": f"Road blocked{' in ' + ward if ward else ''}: "
                       f"{str(p.get('reason') or p.get('note') or '')[:120]}",
            "key": f"block:{ward or ev.get('subject_id')}",
            "ward_id": ward or None,
        }
    return out


@dataclass
class _State:
    running: bool = False
    leader: bool = False
    cursor: int = 0
    last_poll_at: float = 0.0
    events_seen: int = 0
    replans_requested: int = 0
    replans_run: int = 0
    replans_handed_to_demo: int = 0
    nudges: int = 0
    last_replan_trigger: str | None = None
    last_error: str | None = None
    pending: list[tuple[str, str]] = field(default_factory=list)   # (city, why)
    leader_holder: str | None = None          # who holds the lease when this process does not
    leader_expires: str | None = None
    released_units: int = 0
    last_reconcile_at: float = 0.0


state = _State()
_tasks: list[asyncio.Task] = []
_wake: asyncio.Event | None = None
_lock = asyncio.Lock()


def running() -> bool:
    return state.running


def request_replan(trigger: str, city_id: str = "pune") -> None:
    """Ask for a re-plan. Coalesced with every other request in the next second."""
    state.replans_requested += 1
    state.pending.append((city_id or "pune", trigger))
    if _wake is not None:
        _wake.set()


def status() -> dict:
    return {
        "running": state.running,
        "leader": state.leader,
        "cursor": state.cursor,
        "secondsSinceLastPoll": round(time.monotonic() - state.last_poll_at, 1) if state.last_poll_at else None,
        "eventsSeen": state.events_seen,
        "replansRequested": state.replans_requested,
        "replansRun": state.replans_run,
        "replansHandedToDemo": state.replans_handed_to_demo,
        "commanderNudges": state.nudges,
        "lastReplanTrigger": state.last_replan_trigger,
        "lastError": state.last_error,
        "pending": len(state.pending),
        "thisProcess": _holder(),
        "leaderHolder": state.leader_holder,
        "leaderLeaseExpires": state.leader_expires,
        "unitsReleased": state.released_units,
    }


def _holder() -> str:
    from app.core import lease
    return lease.HOLDER


#: A unit marked on site / en route with no active assignment is stuck: nothing will ever free it, and the
#: planner then finds no free unit of that kind. Released after this long without an update.
STUCK_AFTER_MIN = 10
RECONCILE_EVERY_S = 60.0


async def _reconcile_units() -> None:
    """Free units whose job no longer exists (the incident was resolved or the assignment closed)."""
    rows = await db.fetch(
        """
        update resources r
           set status = 'available', updated_at = now()
         where r.status::text in ('on_site', 'en_route')
           and r.updated_at < now() - make_interval(mins => $1)
           and not exists (select 1 from assignments a
                            where a.resource_id = r.id and a.sim_run_id is null
                              and a.status::text in ('proposed','approved','en_route','on_site'))
        returning r.id, r.city_id
        """, STUCK_AFTER_MIN)
    if not rows:
        return
    state.released_units += len(rows)
    from app.world import events as ev
    from app.world.clock import WALL
    for r in rows[:200]:
        await ev.append(clock=WALL, kind="resource.status_changed", actor="agent:event_router", subject_type="resource",
                        subject_id=r["id"], city_id=r["city_id"] or "pune",
                        payload={"status": "available", "reason": "released: no active assignment (job closed)"})
    log.info("units_released", count=len(rows))
    for city in {r["city_id"] or "pune" for r in rows}:
        request_replan(f"{len(rows)} unit(s) released: their jobs were closed", city)


#: Events after which the edge network around an incident is swept (cameras,
#: drones, reports, sensors) and the result logged as `evidence.swept`. Sensing,
#: not acting: it runs even under the emergency stop.
EVIDENCE_KINDS = {"incident.opened", "incident.severity_changed", "report.linked"}
#: Events that are new evidence about an incident: its fused belief is recomputed.
BELIEF_KINDS = EVIDENCE_KINDS | {"field.drone_situation", "field.drone_need", "field.on_site", "field.confirmed",
                                 "field.situation", "field.report", "incident.resolved"}


def _evidence_hook(ev: dict[str, Any]) -> None:
    kind = str(ev.get("kind") or "")
    actor = str(ev.get("actor") or "")
    if kind not in BELIEF_KINDS or actor in ("agent:evidence", "agent:fusion"):
        return
    p = _payload(ev.get("payload"))
    iid = ev.get("subject_id") if ev.get("subject_type") == "incident" else (p.get("incidentId") or p.get("incident_id"))
    if not iid or len(str(iid)) != 36:
        return
    from app.incidents import evidence, fusion

    fusion.schedule(str(iid), trigger=kind, caused_by=int(ev["id"]), city_id=ev.get("city_id") or "pune")
    if kind in EVIDENCE_KINDS:
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(
                evidence.on_incident(str(iid), trigger=kind, caused_by=int(ev["id"]), city_id=ev.get("city_id") or "pune"))


async def _poll_once() -> None:
    rows = await db.fetch(
        """
        select id, kind, actor, subject_type, subject_id, ward_id, city_id, payload
          from events where id > $1 order by id limit 500
        """,
        state.cursor,
    )
    state.last_poll_at = time.monotonic()
    if not rows:
        return
    from app.agents import commander

    for r in rows:
        ev = dict(r)
        state.cursor = max(state.cursor, int(ev["id"]))
        state.events_seen += 1
        decision = route(ev)
        _evidence_hook(ev)
        from app.ops import autonomy

        if autonomy.paused():
            if decision.replan:
                autonomy.state.held_replans += 1
            continue
        if decision.replan:
            request_replan(decision.replan, ev.get("city_id") or "pune")
        if decision.nudge:
            nk = decision.nudge
            extra = {k: nk[k] for k in ("ward_id", "category") if nk.get(k)}
            if commander.nudge(nk["kind"], nk["summary"], key=nk["key"],
                               city_id=ev.get("city_id") or "pune", caused_by=int(ev["id"]), **extra):
                state.nudges += 1


async def _poll_loop() -> None:
    from app.core import lease

    row = await db.fetchrow("select coalesce(max(id), 0) as m from events")
    state.cursor = int(row["m"]) if row else 0      # react to the future, not the past
    log.info("event_router_started", cursor=state.cursor)
    while state.running:
        try:
            was = state.leader
            state.leader, meta = await lease.hold("event_router", settings.router_lease_s,
                                                  {"cursor": state.cursor} if was else None)
            if state.leader and not was:
                # Taking over: resume where the previous leader stopped, so the
                # events written while nobody led are not lost. A very old cursor
                # (a lease left from days ago) is not replayed: re-plans
                # coalesce anyway, and the present is what needs planning.
                stored = int(meta.get("cursor") or 0)
                head = int((await db.fetchval("select coalesce(max(id), 0) from events")) or 0)
                state.cursor = stored if stored and head - stored <= 5000 else max(state.cursor, head)
                log.info("event_router_leader", cursor=state.cursor, holder=lease.HOLDER)
            if not state.leader:
                with contextlib.suppress(Exception):
                    row = await db.fetchrow("select holder, expires_at from service_leases where name = 'event_router'")
                    if row:
                        state.leader_holder, state.leader_expires = row["holder"], row["expires_at"].isoformat()
                await asyncio.sleep(settings.router_lease_s / 3)
                continue
            state.leader_holder = None
            if time.monotonic() - state.last_reconcile_at >= RECONCILE_EVERY_S:
                state.last_reconcile_at = time.monotonic()
                with contextlib.suppress(Exception):
                    await _reconcile_units()
            await _poll_once()
            state.last_error = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep listening
            state.last_error = f"poll: {str(exc)[:200]}"
            log.warning("event_router_poll_failed", error=str(exc)[:200])
        await asyncio.sleep(settings.event_router_poll_s)


async def _replan_loop() -> None:
    from app.agents import replan as replanner
    from app.demo import runner as demo_runner

    assert _wake is not None
    while state.running:
        await _wake.wait()
        await asyncio.sleep(DEBOUNCE_S)        # let a burst finish arriving
        _wake.clear()
        batch, state.pending = state.pending, []
        if not batch:
            continue
        from app.ops import autonomy

        if autonomy.paused():
            # Emergency stop: keep listening, count, do not re-plan.
            autonomy.state.held_replans += len(batch)
            continue
        by_city: dict[str, list[str]] = {}
        for city, why in batch:
            by_city.setdefault(city, []).append(why)
        for city, whys in by_city.items():
            trigger = "; ".join(dict.fromkeys(whys))[:240]
            state.last_replan_trigger = trigger
            try:
                if demo_runner.state.running:
                    # The runner holds the world lock; it re-plans on its next tick.
                    demo_runner.state.dirty = True
                    demo_runner.state.last_replan_tick = -10_000
                    state.replans_handed_to_demo += 1
                    continue
                async with _lock:
                    from app.agents import graph as agent_graph

                    if agent_graph.enabled():
                        # The LangGraph cycle: sense -> assess -> preview ->
                        # validate -> policy gate -> (officer) -> dispatch.
                        await agent_graph.run_cycle(city_id=city, trigger=trigger)
                    else:
                        await replanner.replan(city_id=city, trigger=trigger,
                                               actor="agent:allocation_planner")
                state.replans_run += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                state.last_error = f"replan: {str(exc)[:200]}"
                log.warning("event_router_replan_failed", error=str(exc)[:200])


async def start() -> None:
    global _wake
    if state.running or not settings.event_router_enabled:
        return
    _wake = asyncio.Event()
    state.running = True
    loop = asyncio.get_running_loop()
    _tasks[:] = [loop.create_task(_poll_loop()), loop.create_task(_replan_loop())]


async def stop() -> None:
    state.running = False
    for t in _tasks:
        t.cancel()
    for t in _tasks:
        try:
            await t
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _tasks.clear()
