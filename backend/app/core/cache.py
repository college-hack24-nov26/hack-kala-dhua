"""A small in-process cache with single-flight.

Why this exists
---------------

`/citizen/state` costs about 1.5 seconds and every resident polls it every four
seconds. One viewer is fine. A street full of people during a flood is the
failure: a hundred phones asking the same question of the same ward produce a
hundred identical sets of database round trips, and the instance that was
answering in 1.5s starts answering in twenty.

The plan on record was "cache per ward". That is the right instinct and the
wrong key. Look at what the endpoint actually does: `locate_ward` is a
point-in-polygon test, and the five reads behind it are *radius around the
caller*, not *rows belonging to a ward*. Keying those by ward would quietly
change what the endpoint means — two people in the same ward but three
kilometres apart do not have the same incidents near them, and a cache that
told them they did would be wrong in the direction that matters.

So the key is the rounded position, and the honest win is smaller and real:

* **Single-flight is the point, not the TTL.** When fifty requests for the same
  key arrive while one query is in flight, forty-nine wait for that answer
  instead of starting their own. That is the thundering herd, and it is what
  actually falls over.
* **The TTL is deliberately shorter than the poll.** At three seconds against a
  four-second poll, a lone viewer still gets a fresh read almost every time —
  so a demo behaves exactly as it did before. The saving is entirely in
  concurrency, which is exactly where the problem was.
* **Rounding to four decimal places lays an ~11 m grid over the city.** A grid,
  not a radius: two people six metres apart either land in the same cell and
  share an answer, or straddle a boundary and do not. That asymmetry is fine in
  the only direction that matters — the worst case is a cache miss and a fresh
  query, never a stale answer served to the wrong place. Sharing within a cell
  is safe because nothing on this screen is precise to eleven metres: distances
  render in kilometres to one decimal, or in metres rounded to the nearest ten.

Deliberately in-process and not Redis. With one worker per container this is
per-replica, so N replicas do N times the work of one — still a hundredfold
better than N times the *viewers*, and it adds no new thing to run, keep alive
or explain. A shared cache is the next step if replicas ever outnumber the
saving, and the interface here does not change when it is.

Nothing that a person *writes* goes through this. It is for reads that are
already a snapshot of a world that changes on a cadence of minutes.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Hashable

from app.core.logging import get_logger

log = get_logger(__name__)


class _Entry:
    __slots__ = ("value", "expires_at", "lock")

    def __init__(self) -> None:
        self.value: Any = None
        self.expires_at: float = 0.0
        # Held while one caller computes; everybody else waits on it rather
        # than starting a second identical computation.
        self.lock = asyncio.Lock()


class TTLCache:
    """Keyed, time-bounded, single-flight.

    Not an LRU. Entries are dropped when they expire and a sweep runs on write,
    because the key space here is bounded by "distinct rounded positions people
    are standing at", which is small and self-limiting. A size cap exists anyway
    so that a client sending unrounded coordinates cannot grow this without
    bound — that bug has happened once already on the other side of this call.
    """

    def __init__(self, *, name: str, max_entries: int = 2048) -> None:
        self._name = name
        self._entries: dict[Hashable, _Entry] = {}
        self._max = max_entries
        self.hits = 0
        self.misses = 0

    async def get_or_set(
        self,
        key: Hashable,
        ttl_seconds: float,
        compute: Callable[[], Awaitable[Any]],
    ) -> Any:
        now = time.monotonic()
        entry = self._entries.get(key)

        if entry is not None and entry.expires_at > now:
            self.hits += 1
            return entry.value

        if entry is None:
            if len(self._entries) >= self._max:
                self._sweep(now)
            entry = self._entries.setdefault(key, _Entry())

        async with entry.lock:
            # Re-check inside the lock. Whoever held it may have just filled
            # this in, and recomputing here is precisely the work the lock was
            # taken to avoid.
            now = time.monotonic()
            if entry.expires_at > now:
                self.hits += 1
                return entry.value

            self.misses += 1
            value = await compute()
            entry.value = value
            entry.expires_at = time.monotonic() + ttl_seconds
            return value

    def peek(self, key: Hashable) -> Any:
        """Read without computing, and without extending the entry's life.

        `get_or_set` is the wrong shape when there is nothing to fall back to:
        redeeming a token that has expired should answer "no" rather than invent
        a value for it. Returns None for a miss, which is also what a genuinely
        stored None looks like — fine here, because nothing stores None.
        """
        entry = self._entries.get(key)
        if entry is None or entry.expires_at <= time.monotonic():
            self.misses += 1
            return None
        self.hits += 1
        return entry.value

    def _sweep(self, now: float) -> None:
        """Drop what has expired; if that frees nothing, drop the oldest half.

        The second clause is the one that matters: it is what makes an unbounded
        key space merely wasteful rather than fatal.
        """
        dead = [k for k, e in self._entries.items() if e.expires_at <= now]
        for k in dead:
            self._entries.pop(k, None)
        if not dead and self._entries:
            oldest = sorted(self._entries.items(), key=lambda kv: kv[1].expires_at)
            for k, _ in oldest[: len(oldest) // 2]:
                self._entries.pop(k, None)
        log.info(
            "cache_sweep", cache=self._name, dropped=len(dead), size=len(self._entries)
        )

    def stats(self) -> dict[str, int | float]:
        total = self.hits + self.misses
        return {
            "size": len(self._entries),
            "hits": self.hits,
            "misses": self.misses,
            "hitRate": round(self.hits / total, 3) if total else 0.0,
        }

    def clear(self) -> None:
        """Used by the demo reset. A cached world outliving the world it
        described is the reset appearing not to have worked."""
        self._entries.clear()


#: Keyed by (rounded lng, rounded lat, city, radius). See the module docstring
#: for why the key is a position and not a ward.
citizen_state = TTLCache(name="citizen_state")

#: Ward risk genuinely *is* ward-shaped — one row per ward, rewritten by the
#: hazard agent on its own cadence — so this one is keyed the way the plan said.
ward_risk = TTLCache(name="ward_risk")

#: A photo assessment, held between "look at this" and "now file it".
#:
#: The app shows somebody what their photo contributes *before* the report is
#: filed, which means the assessment exists a few seconds earlier than the
#: report it belongs to. The alternative shapes are both worse: analysing twice
#: costs a second model call on a phone in a flood, and letting the app hand the
#: assessment back at file time means the score a report is trusted on is a
#: number the client could edit. So the server keeps it, briefly, and the client
#: gets an opaque token.
#:
#: Five minutes. Long enough to look at what the model said and decide, short
#: enough that a token found later is worth nothing.
photo_evidence = TTLCache(name="photo_evidence")
PHOTO_TOKEN_TTL = 300.0


def position_key(lng: float, lat: float, *args: Any) -> tuple:
    """Round to ~11 m before keying.

    The client already rounds to five decimals; this rounds again rather than
    trusting it, because the cache's memory footprint must not depend on a
    caller getting that right.
    """
    return (round(lng, 4), round(lat, 4), *args)
