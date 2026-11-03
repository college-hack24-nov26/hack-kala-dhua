"""The emergency stop: pause everything the system does on its own.

"Stop everything" from an officer means: no machine may move a unit, issue an
alert or start an agent until a person says otherwise. It does NOT mean
recalling crews already on the road or on scene. Pulling a boat away from a
family mid-rescue is a decision, not a safety default, so it stays with a person
(cancel a unit by name, from the copilot or the operations screen).

While paused:
  * the event router keeps listening and counts what it would have re-planned,
    but does not re-plan;
  * the LangGraph cycle does not run, and its dispatch step refuses to write;
  * the policy gate issues nothing automatically: every decision waits for an
    officer, whatever its delegation;
  * the LLM Incident Commander does not start episodes;
  * the demo runner is stopped.
Humans keep every control: approving, cancelling, re-tasking by hand.

The flag is read synchronously (the policy gate is a pure function) from memory,
and persisted in `control_state` so a restart does not silently resume.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass

from app.core.logging import get_logger

log = get_logger(__name__)

KEY = "autonomy"


@dataclass
class _State:
    paused: bool = False
    by: str | None = None
    reason: str | None = None
    since: float | None = None
    held_replans: int = 0


state = _State()


def paused() -> bool:
    return state.paused


def status() -> dict:
    d = asdict(state)
    d["pausedForS"] = int(time.time() - state.since) if state.paused and state.since else 0
    return d


async def load() -> None:
    """Restore the flag after a restart. A missing table means never paused."""
    try:
        from app.db import session as db

        row = await db.fetchrow("select value from control_state where key = $1", KEY)
        if row and isinstance(row["value"], dict):
            v = row["value"]
            state.paused = bool(v.get("paused"))
            state.by, state.reason, state.since = v.get("by"), v.get("reason"), v.get("since")
            if state.paused:
                log.warning("autonomy_paused_at_startup", by=state.by, reason=state.reason)
    except Exception as exc:  # noqa: BLE001
        log.info("autonomy_state_unavailable", error=type(exc).__name__)


async def _persist(city_id: str, kind: str) -> None:
    try:
        from app.db import session as db
        from app.world import clock as clocks
        from app.world import events as ev

        await db.execute(
            """insert into control_state (key, value, updated_at) values ($1, $2::jsonb, now())
               on conflict (key) do update set value = excluded.value, updated_at = now()""",
            KEY, {"paused": state.paused, "by": state.by, "reason": state.reason, "since": state.since},
        )
        await ev.append(clock=clocks.WALL, kind=kind, actor=f"person:{state.by or 'officer'}",
                        subject_type="system", subject_id="autonomy", city_id=city_id,
                        payload={"reason": state.reason, "held_replans": state.held_replans})
    except Exception as exc:  # noqa: BLE001 - the in-memory flag is what enforces it
        log.warning("autonomy_persist_failed", error=str(exc)[:200])


async def pause(*, by: str, reason: str | None = None, city_id: str = "pune") -> dict:
    already = state.paused
    state.paused, state.by = True, by
    state.reason = (reason or "Emergency stop from the console").strip()[:200]
    if not already:
        state.since, state.held_replans = time.time(), 0
    stopped_demo = False
    try:
        from app.demo import runner as demo_runner

        if demo_runner.state.running:
            await demo_runner.stop()
            stopped_demo = True
    except Exception:  # noqa: BLE001
        pass
    await _persist(city_id, "system.autonomy_paused")
    log.warning("autonomy_paused", by=by, reason=state.reason)
    return {**status(), "stoppedDemo": stopped_demo, "alreadyPaused": already}


async def resume(*, by: str, city_id: str = "pune") -> dict:
    was = state.paused
    held = state.held_replans
    state.paused, state.by, state.reason, state.since = False, by, None, None
    await _persist(city_id, "system.autonomy_resumed")
    if was:
        # Catch up on what changed while paused: one re-plan against the world now.
        try:
            from app.agents import event_router

            event_router.request_replan(f"resumed by {by} after a pause", city_id)
        except Exception:  # noqa: BLE001
            pass
    log.warning("autonomy_resumed", by=by, held=held)
    return {**status(), "wasPaused": was, "heldReplans": held}
