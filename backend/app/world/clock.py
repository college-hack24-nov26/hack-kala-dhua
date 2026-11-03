"""The clock.

Nothing in this codebase calls `datetime.now()` directly any more. Everything
takes a `Clock`.

That sounds fussy for one function. It is the single change that turns the
simulator from a scripted demo into the production code path. When the engine
reads the time from an injected clock, a simulated run is not a different mode
with different code, it is the same code with a different clock. Replay is the
same again. And a report a human types into the citizen portal during a
simulation gets stamped with simulated time, which is what makes it
indistinguishable from a generated one and therefore able to change the
outcome.

Wall time is still recorded separately on every event (`recorded_at`), so we
never lose the audit fact of when something actually reached us. That
distinction also matters for real life: a report relayed over a mesh link can
arrive forty minutes after it happened, and the system has to order it by when
it happened, not when it landed.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """World time. May or may not be wall time."""

    @property
    def sim_run_id(self) -> str | None: ...

    def now(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class WallClock:
    """Live operations. World time is wall time."""

    @property
    def sim_run_id(self) -> str | None:
        return None

    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(slots=True)
class SimClock:
    """Virtual time for a simulation or a replay.

    Advances at `speed` simulated seconds per real second while running, and
    holds still while paused. `advance` exists so a tick loop can step the world
    deterministically rather than depending on how long the previous tick took,
    which is what makes a run with a fixed seed reproducible.
    """

    run_id: str
    clock_start: datetime
    clock_now: datetime
    speed: float = 60.0
    running: bool = False
    _last_wall: float = 0.0

    def __post_init__(self) -> None:
        self._last_wall = time.monotonic()

    @property
    def sim_run_id(self) -> str | None:
        return self.run_id

    def now(self) -> datetime:
        if self.running:
            wall = time.monotonic()
            elapsed = wall - self._last_wall
            self._last_wall = wall
            self.clock_now = self.clock_now + _seconds(elapsed * self.speed)
        return self.clock_now

    def advance(self, sim_seconds: float) -> datetime:
        """Step the world forward by an exact amount of simulated time."""
        self.clock_now = self.clock_now + _seconds(sim_seconds)
        self._last_wall = time.monotonic()
        return self.clock_now

    def start(self) -> None:
        self._last_wall = time.monotonic()
        self.running = True

    def pause(self) -> None:
        self.now()  # bank the elapsed time before stopping
        self.running = False

    @property
    def elapsed_sim_seconds(self) -> float:
        return (self.clock_now - self.clock_start).total_seconds()


def _seconds(value: float):
    from datetime import timedelta

    return timedelta(seconds=value)


#: The clock used when nothing else is specified. Live operations.
WALL = WallClock()

#: Clocks for simulations currently open in this process, by run id.
_sim_clocks: dict[str, SimClock] = {}


def register_sim_clock(clock: SimClock) -> SimClock:
    _sim_clocks[clock.run_id] = clock
    return clock


class UnknownScope(ValueError):
    """The caller named a simulation scope that is not open.

    Deliberately not a silent fallback to the wall clock. Writing a simulated
    report into live operations because a scope id was wrong is the one mistake
    this whole design exists to prevent, so an unusable scope is refused rather
    than quietly reinterpreted.
    """


def get(sim_run_id: str | None) -> Clock:
    """Resolve the clock for a scope. `None`, or blank, means live.

    Blank counts as live because the API docs page sends the literal string
    "string" for an unset optional field, and an empty form input sends "". A
    500 from a placeholder is a bad first impression of an endpoint that is
    working fine.
    """
    if sim_run_id is None:
        return WALL
    scope = sim_run_id.strip()
    if not scope or scope.lower() in ("string", "null", "none"):
        return WALL

    try:
        uuid.UUID(scope)
    except ValueError:
        raise UnknownScope(
            f"{sim_run_id!r} is not a simulation run id. Leave it empty for "
            "live operations, or pass the UUID of an open run."
        ) from None

    clock = _sim_clocks.get(scope)
    if clock is None:
        raise UnknownScope(
            f"Simulation {scope} is not open in this process. Start or resume "
            "the run before writing into its scope."
        )
    return clock


def forget(sim_run_id: str) -> None:
    _sim_clocks.pop(sim_run_id, None)
