"""Keeping model spend flat while traffic is not.

How production LLM systems keep token cost low under thousands of callers, and
which of those this module does. The order is the order of savings:

1. **Don't call the model.** Already the design here: keyword rules, the
   zero-shot classifier and the solver answer first; the model is only asked
   in the ambiguous band. Every task below records *why* a call did not happen
   (cache, budget, bulkhead, fallback), so the saving is measured, not claimed.
2. **Don't call it twice.** A response cache keyed on (task, model tier,
   system prompt, guarded prompt) — or on a caller-chosen semantic key, like
   one ward's facts plus a normalised question — with a TTL per task. During a
   flood a thousand residents ask the same five questions about the same ward.
3. **Don't call it concurrently for the same thing.** Single-flight: identical
   requests already in flight wait for the one answer instead of starting
   their own. This is the thundering-herd saving and it is the largest one
   under burst.
4. **Use the smallest model that can do the job.** Each task has a tier.
   Routing, category, severity and duplicate yes/no go to the small model;
   narration and the Commander go to the large one.
5. **Spend fewer tokens per call.** A hard `max_output_tokens` per task (a
   category is one word, not 8k tokens of budget), the system prompt sent as a
   *system instruction* so the provider's prefix cache can reuse it, and
   prompts capped by the gateway guardrail.
6. **Cap the spend.** A token budget per minute and per day. Near the limit,
   low-priority work (narration, resident Q&A) degrades to the deterministic
   answer first; life-safety work (severity, the Commander) keeps its share
   until the very end.
7. **Bulkhead.** A bounded number of model calls in flight. When it is full,
   low-priority calls degrade at once rather than queue behind life-safety.

With `REDIS_URL` set the cache and the budget are shared by every replica;
without it they are per process, which is correct for one replica.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Hashable, Literal

from app.core import shared
from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

Tier = Literal["small", "large"]
Priority = Literal["critical", "normal", "low"]


@dataclass(frozen=True, slots=True)
class TaskProfile:
    tier: Tier
    max_tokens: int
    cache_ttl_s: int
    priority: Priority


#: One row per kind of model call in the codebase. A task not listed here is
#: "general": large model, no cache, normal priority.
PROFILES: dict[str, TaskProfile] = {
    "route":       TaskProfile("small", 160, 300, "normal"),    # copilot intent router
    "classify":    TaskProfile("small", 16, 3600, "normal"),    # report category: one word
    "severity":    TaskProfile("small", 160, 3600, "critical"), # life safety
    "adjudicate":  TaskProfile("small", 120, 1800, "normal"),   # same incident? yes/no
    "narrate":     TaskProfile("large", 320, 60, "low"),        # prose over a table
    "citizen_ask": TaskProfile("small", 220, 300, "low"),       # resident Q&A
    "commander":   TaskProfile("large", 400, 0, "critical"),    # agent loop: never cached
    "general":     TaskProfile("large", 600, 0, "normal"),
}

#: Share of the per-minute budget each priority may consume before it degrades.
PRIORITY_CEILING: dict[str, float] = {"low": 0.60, "normal": 0.85, "critical": 1.00}


def profile(task: str) -> TaskProfile:
    return PROFILES.get(task, PROFILES["general"])


def estimate_tokens(*texts: str) -> int:
    """~4 characters per token. Only used for admission; usage is recorded
    from what the provider reports when it reports it."""
    return sum(len(t or "") for t in texts) // 4 + 1


# ------------------------------------------------------------------ prices ---
def _prices() -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    for part in (settings.llm_prices or "").split(","):
        if "=" not in part or "/" not in part:
            continue
        name, rate = part.split("=", 1)
        try:
            i, o = rate.split("/", 1)
            out[name.strip()] = (float(i), float(o))
        except ValueError:
            continue
    return out


_PRICES = _prices()


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> float | None:
    """Cost of one call, or None when the model's price is not configured.
    An unknown price is reported as unknown, never as a guess."""
    p = _PRICES.get(model)
    if p is None:
        return None
    return (tokens_in * p[0] + tokens_out * p[1]) / 1_000_000


# ------------------------------------------------------------------- cache ---
class ResponseCache:
    """LRU + TTL + single-flight. Redis is a shared second level when set."""

    def __init__(self, max_entries: int) -> None:
        self._data: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._inflight: dict[str, asyncio.Future] = {}
        self._max = max_entries
        self.hits = self.misses = self.coalesced = self.shared_hits = 0

    @staticmethod
    def key(parts: Hashable) -> str:
        raw = json.dumps(parts, default=str, sort_keys=True) if not isinstance(parts, str) else parts
        return "llm:" + hashlib.sha256(raw.encode()).hexdigest()[:40]

    def _get_local(self, k: str) -> str | None:
        item = self._data.get(k)
        if item is None:
            return None
        expires, value = item
        if expires < time.monotonic():
            self._data.pop(k, None)
            return None
        self._data.move_to_end(k)
        return value

    def _put_local(self, k: str, value: str, ttl: int) -> None:
        self._data[k] = (time.monotonic() + ttl, value)
        self._data.move_to_end(k)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    async def get_or_compute(self, k: str, ttl: int,
                             compute: Callable[[], Awaitable[tuple[str, bool]]]) -> tuple[str, str]:
        """Returns (text, how): how is hit | shared | coalesced | computed | uncached.

        `compute` returns (text, cacheable). Fallback answers are not cacheable:
        caching "the model was down" would keep serving it after the model is back.
        """
        local = self._get_local(k)
        if local is not None:
            self.hits += 1
            return local, "hit"
        r = shared.client()
        if r is not None:
            try:
                got = await r.get(k)
                if got is not None:
                    value = got.decode() if isinstance(got, bytes) else str(got)
                    self._put_local(k, value, ttl)
                    self.shared_hits += 1
                    return value, "shared"
            except Exception as exc:  # noqa: BLE001
                shared.mark_down(exc)
        waiting = self._inflight.get(k)
        if waiting is not None:
            self.coalesced += 1
            text, _ = await asyncio.shield(waiting)
            return text, "coalesced"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[k] = fut
        try:
            self.misses += 1
            text, cacheable = await compute()
            fut.set_result((text, cacheable))
        except BaseException as exc:
            fut.set_exception(exc)
            # Nobody may be awaiting it; do not leave an unretrieved exception.
            fut.exception()
            raise
        finally:
            self._inflight.pop(k, None)
        if cacheable and ttl > 0:
            self._put_local(k, text, ttl)
            r = shared.client()
            if r is not None:
                try:
                    await r.set(k, text.encode(), ex=ttl)
                except Exception as exc:  # noqa: BLE001
                    shared.mark_down(exc)
            return text, "computed"
        return text, "uncached"

    def clear(self) -> None:
        self._data.clear()

    def stats(self) -> dict[str, Any]:
        total = self.hits + self.shared_hits + self.coalesced + self.misses
        saved = self.hits + self.shared_hits + self.coalesced
        return {"entries": len(self._data), "hits": self.hits, "sharedHits": self.shared_hits,
                "coalesced": self.coalesced, "misses": self.misses,
                "hitRate": round(saved / total, 3) if total else 0.0}


cache = ResponseCache(settings.llm_cache_max_entries)


# ------------------------------------------------------------------ budget ---
class Budget:
    """Tokens per rolling minute and per UTC day, with priority ceilings."""

    def __init__(self) -> None:
        self._minute: deque[tuple[float, int]] = deque()
        self._minute_total = 0
        self._day = time.strftime("%Y-%m-%d", time.gmtime())
        self._day_total = 0
        self.refused: dict[str, int] = defaultdict(int)

    def _roll(self, now: float) -> None:
        while self._minute and self._minute[0][0] < now - 60:
            _, n = self._minute.popleft()
            self._minute_total -= n
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if day != self._day:
            self._day, self._day_total = day, 0

    async def _shared_usage(self) -> tuple[int, int] | None:
        r = shared.client()
        if r is None:
            return None
        try:
            minute = int(time.time() // 60)
            pipe = r.pipeline()
            pipe.get(f"llm:tok:m:{minute}")
            pipe.get(f"llm:tok:m:{minute - 1}")
            pipe.get(f"llm:tok:d:{self._day}")
            cur, prev, day = await pipe.execute()
            frac = (time.time() % 60) / 60
            # Sliding estimate over two fixed windows: the standard trick.
            used_min = int(int(cur or 0) + int(prev or 0) * (1 - frac))
            return used_min, int(day or 0)
        except Exception as exc:  # noqa: BLE001
            shared.mark_down(exc)
            return None

    async def admit(self, priority: str, estimate: int) -> tuple[bool, str]:
        now = time.monotonic()
        self._roll(now)
        used = await self._shared_usage()
        used_min, used_day = used if used is not None else (self._minute_total, self._day_total)
        per_min = max(1, settings.llm_tokens_per_minute)
        if used_day + estimate > settings.llm_tokens_per_day and priority != "critical":
            self.refused[priority] += 1
            return False, f"daily token budget ({settings.llm_tokens_per_day:,}) reached"
        ceiling = PRIORITY_CEILING.get(priority, 0.85) * per_min
        if used_min + estimate > ceiling:
            self.refused[priority] += 1
            return False, (f"{priority} calls capped at {PRIORITY_CEILING.get(priority, 0.85):.0%} "
                           f"of {per_min:,} tokens/min")
        return True, ""

    async def record(self, tokens: int) -> None:
        now = time.monotonic()
        self._minute.append((now, tokens))
        self._minute_total += tokens
        self._day_total += tokens
        r = shared.client()
        if r is not None:
            try:
                minute = int(time.time() // 60)
                pipe = r.pipeline()
                pipe.incrby(f"llm:tok:m:{minute}", tokens)
                pipe.expire(f"llm:tok:m:{minute}", 180)
                pipe.incrby(f"llm:tok:d:{self._day}", tokens)
                pipe.expire(f"llm:tok:d:{self._day}", 2 * 86400)
                await pipe.execute()
            except Exception as exc:  # noqa: BLE001
                shared.mark_down(exc)

    def stats(self) -> dict[str, Any]:
        self._roll(time.monotonic())
        return {"tokensLastMinute": self._minute_total, "tokensToday": self._day_total,
                "perMinute": settings.llm_tokens_per_minute, "perDay": settings.llm_tokens_per_day,
                "ceilings": PRIORITY_CEILING, "refused": dict(self.refused),
                "shared": shared.enabled()}

    def reset(self) -> None:
        self.__init__()


budget = Budget()


# ---------------------------------------------------------------- bulkhead ---
class Bulkhead:
    def __init__(self, size: int) -> None:
        self.size = max(1, size)
        self._sem = asyncio.Semaphore(self.size)
        self.in_flight = 0
        self.shed = 0

    def full_for(self, priority: str) -> bool:
        if priority == "low":
            return self.in_flight >= int(self.size * 0.75)
        return False

    async def __aenter__(self) -> "Bulkhead":
        await self._sem.acquire()
        self.in_flight += 1
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.in_flight -= 1
        self._sem.release()


bulkhead = Bulkhead(settings.llm_max_concurrency)


# ------------------------------------------------------------------- usage ---
@dataclass
class _Row:
    calls: int = 0
    model_calls: int = 0
    cache_saved: int = 0
    fallbacks: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    cost_usd: float = 0.0
    cost_known: bool = True
    latency_ms: float = 0.0


_USAGE: dict[str, _Row] = defaultdict(_Row)
_REASONS: dict[str, int] = defaultdict(int)


def record(task: str, *, how: str, tokens_in: int = 0, tokens_out: int = 0, tokens_cached: int = 0,
           model: str = "", latency_ms: float = 0.0, fallback_reason: str | None = None) -> None:
    row = _USAGE[task]
    row.calls += 1
    if how in ("hit", "shared", "coalesced"):
        row.cache_saved += 1
    elif how in ("computed", "uncached") and not fallback_reason:
        row.model_calls += 1
        row.tokens_in += tokens_in
        row.tokens_out += tokens_out
        row.tokens_cached += tokens_cached
        row.latency_ms += latency_ms
        c = cost_usd(model, tokens_in, tokens_out)
        if c is None:
            row.cost_known = False
        else:
            row.cost_usd += c
    if fallback_reason:
        row.fallbacks += 1
        _REASONS[fallback_reason.split(":")[0][:40]] += 1


def usage() -> dict[str, Any]:
    tasks = {}
    for task, r in sorted(_USAGE.items()):
        tasks[task] = {
            "calls": r.calls, "modelCalls": r.model_calls, "servedFromCache": r.cache_saved,
            "deterministic": r.fallbacks, "tokensIn": r.tokens_in, "tokensOut": r.tokens_out,
            "tokensCachedByProvider": r.tokens_cached,
            "avoidedRate": round(1 - r.model_calls / r.calls, 3) if r.calls else 0.0,
            "costUsd": round(r.cost_usd, 6) if r.cost_known else None,
            "avgLatencyMs": round(r.latency_ms / r.model_calls, 1) if r.model_calls else None,
            "profile": {"tier": profile(task).tier, "maxTokens": profile(task).max_tokens,
                        "cacheTtlS": profile(task).cache_ttl_s, "priority": profile(task).priority},
        }
    calls = sum(r.calls for r in _USAGE.values())
    model_calls = sum(r.model_calls for r in _USAGE.values())
    return {
        "totals": {"calls": calls, "modelCalls": model_calls,
                   "avoidedRate": round(1 - model_calls / calls, 3) if calls else 0.0,
                   "tokensIn": sum(r.tokens_in for r in _USAGE.values()),
                   "tokensOut": sum(r.tokens_out for r in _USAGE.values())},
        "tasks": tasks,
        "degradeReasons": dict(_REASONS),
        "cache": cache.stats(),
        "budget": budget.stats(),
        "bulkhead": {"size": bulkhead.size, "inFlight": bulkhead.in_flight, "shed": bulkhead.shed},
    }


def reset_for_tests() -> None:
    _USAGE.clear()
    _REASONS.clear()
    cache.clear()
    cache.hits = cache.misses = cache.coalesced = cache.shared_hits = 0
    budget.reset()
    bulkhead.shed = 0
