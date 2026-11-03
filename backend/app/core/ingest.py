"""The ingest bus: accept fast, write in batches, shed the right thing.

Why
---
Every write endpoint used to do its whole job inside the request: a sensor
reading was five database round trips before the gateway got its 200. That is
fine at a bench of two Arduinos and is the ceiling at a city of sensors, phones
and drones: the API's throughput becomes the database's round-trip rate.

The bus separates *accepting* from *processing*:

    request ─► validate ─► enqueue (lane) ─► 202 + ticket          (sub-millisecond)
                                │
                     worker ◄───┘  drains a batch (up to N items or T ms),
                                   one handler call per batch, one transaction

Three lanes, drained in priority order by every worker:

    critical   citizen SOS / reports      never shed: if full, written through synchronously
    standard   mesh packets, drone hits   shed last
    bulk       sensor telemetry           shed first (503 + Retry-After); a gateway re-sends

Idempotency: a client-chosen `Idempotency-Key` (or a content hash) maps to the
first ticket for ten minutes, so a phone that retries on a bad network files
one report, not five.

Backends
--------
* **memory** (default): asyncio queues in this process. Correct for one
  replica; items accepted by a replica are processed by that replica.
* **redis** (`REDIS_URL` set): one Redis Stream per lane with a consumer
  group, so any worker on any replica (or the separate `python -m app.worker`
  deployment) can process any item, and an item a crashed worker took is
  re-claimed after `CLAIM_IDLE_MS`.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from app.core import shared
from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

Lane = Literal["critical", "standard", "bulk"]
LANES: tuple[Lane, ...] = ("critical", "standard", "bulk")
TICKET_TTL_S = 600
CLAIM_IDLE_MS = 30_000
GROUP = "ingest-workers"

Handler = Callable[[list[dict[str, Any]]], Awaitable[list[dict[str, Any]]]]
_HANDLERS: dict[str, tuple[Lane, Handler]] = {}


def handler(kind: str, lane: Lane) -> Callable[[Handler], Handler]:
    """Register the batch processor for one kind of item."""
    def deco(fn: Handler) -> Handler:
        _HANDLERS[kind] = (lane, fn)
        return fn
    return deco


@dataclass
class Accepted:
    accepted: bool
    ticket: str | None = None
    lane: str = ""
    duplicate: bool = False
    shed: bool = False
    retry_after: int = 0
    inline_result: dict[str, Any] | None = None


@dataclass
class _Stats:
    enqueued: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    processed: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    failed: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    shed: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    written_through: int = 0
    duplicates: int = 0
    batches: int = 0
    batch_items: int = 0
    lag_ms_total: float = 0.0
    lag_samples: int = 0


stats = _Stats()


class _TTLMap:
    def __init__(self, cap: int = 200_000) -> None:
        self._d: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._cap = cap

    def get(self, k: str) -> Any:
        v = self._d.get(k)
        if v is None or v[0] < time.monotonic():
            self._d.pop(k, None)
            return None
        return v[1]

    def put(self, k: str, v: Any, ttl: float) -> None:
        self._d[k] = (time.monotonic() + ttl, v)
        self._d.move_to_end(k)
        while len(self._d) > self._cap:
            self._d.popitem(last=False)


_idem = _TTLMap()
_tickets = _TTLMap()
_queues: dict[str, asyncio.Queue] = {}
_workers: list[asyncio.Task] = []
_wake: asyncio.Event | None = None
_consumer = f"{os.uname().nodename if hasattr(os, 'uname') else 'host'}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


def _max_for(lane: str) -> int:
    return {"critical": settings.ingest_critical_max, "standard": settings.ingest_standard_max,
            "bulk": settings.ingest_bulk_max}[lane]


def _queue(lane: str) -> asyncio.Queue:
    q = _queues.get(lane)
    if q is None:
        q = _queues[lane] = asyncio.Queue(maxsize=_max_for(lane))
    return q


def content_key(kind: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256((kind + json.dumps(payload, sort_keys=True, default=str)).encode()).hexdigest()[:32]


# ------------------------------------------------------------------ tickets ---
async def _set_ticket(ticket: str, value: dict[str, Any]) -> None:
    _tickets.put(ticket, value, TICKET_TTL_S)
    r = shared.client()
    if r is not None:
        try:
            await r.set(f"ingest:t:{ticket}", json.dumps(value, default=str), ex=TICKET_TTL_S)
        except Exception as exc:  # noqa: BLE001
            shared.mark_down(exc)


async def ticket(ticket_id: str) -> dict[str, Any] | None:
    got = _tickets.get(ticket_id)
    if got is not None:
        return got
    r = shared.client()
    if r is not None:
        try:
            raw = await r.get(f"ingest:t:{ticket_id}")
            return json.loads(raw) if raw else None
        except Exception as exc:  # noqa: BLE001
            shared.mark_down(exc)
    return None


async def _idempotent(key: str, ticket_id: str) -> str | None:
    """The existing ticket for this key, or None after claiming it."""
    existing = _idem.get(key)
    if existing:
        return existing
    r = shared.client()
    if r is not None:
        try:
            ok = await r.set(f"ingest:i:{key}", ticket_id, nx=True, ex=TICKET_TTL_S)
            if not ok:
                prior = await r.get(f"ingest:i:{key}")
                if prior:
                    return prior.decode() if isinstance(prior, bytes) else str(prior)
        except Exception as exc:  # noqa: BLE001
            shared.mark_down(exc)
    _idem.put(key, ticket_id, TICKET_TTL_S)
    return None


# ------------------------------------------------------------------ enqueue ---
async def submit(kind: str, payload: dict[str, Any], *, idempotency_key: str | None = None) -> Accepted:
    """Accept one item. Never blocks on the database unless the critical lane
    is full, in which case the item is processed inline (written through)."""
    if kind not in _HANDLERS:
        raise KeyError(f"no ingest handler for {kind!r}")
    lane, fn = _HANDLERS[kind]
    ticket_id = uuid.uuid4().hex[:20]
    key = f"{kind}:{idempotency_key or content_key(kind, payload)}"
    prior = await _idempotent(key, ticket_id)
    if prior:
        stats.duplicates += 1
        return Accepted(True, prior, lane, duplicate=True)

    item = {"kind": kind, "payload": payload, "ticket": ticket_id, "at": time.time()}
    r = shared.client()
    if r is not None:
        try:
            depth = await r.xlen(f"ingest:{lane}")
            if depth >= _max_for(lane) and lane != "critical":
                stats.shed[lane] += 1
                return Accepted(False, None, lane, shed=True, retry_after=_retry_after(lane))
            if depth < _max_for(lane):
                await r.xadd(f"ingest:{lane}", {"d": json.dumps(item, default=str)},
                             maxlen=_max_for(lane) * 2, approximate=True)
                stats.enqueued[lane] += 1
                await _set_ticket(ticket_id, {"status": "queued", "lane": lane, "kind": kind})
                return Accepted(True, ticket_id, lane)
        except Exception as exc:  # noqa: BLE001 - fall through to the local queue
            shared.mark_down(exc)

    q = _queue(lane)
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:
        if lane != "critical":
            stats.shed[lane] += 1
            return Accepted(False, None, lane, shed=True, retry_after=_retry_after(lane))
        # A report from a person is never dropped for being one too many.
        stats.written_through += 1
        result = (await fn([payload]) or [{}])[0]
        await _set_ticket(ticket_id, {"status": "done", "lane": lane, "kind": kind, "result": result})
        return Accepted(True, ticket_id, lane, inline_result=result)
    stats.enqueued[lane] += 1
    await _set_ticket(ticket_id, {"status": "queued", "lane": lane, "kind": kind})
    if _wake is not None:
        _wake.set()
    return Accepted(True, ticket_id, lane)


def _retry_after(lane: str) -> int:
    return 2 if lane == "standard" else 5


# ------------------------------------------------------------------ workers ---
async def _process(items: list[dict[str, Any]]) -> None:
    by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for it in items:
        by_kind[it["kind"]].append(it)
    now = time.time()
    for kind, group in by_kind.items():
        lane, fn = _HANDLERS[kind]
        stats.batches += 1
        stats.batch_items += len(group)
        for it in group:
            stats.lag_ms_total += (now - float(it.get("at") or now)) * 1000
            stats.lag_samples += 1
        try:
            results = await fn([it["payload"] for it in group])
        except Exception as exc:  # noqa: BLE001 - a bad batch must not stop the bus
            stats.failed[lane] += len(group)
            log.warning("ingest_batch_failed", kind=kind, size=len(group), error=str(exc)[:200])
            for it in group:
                await _set_ticket(it["ticket"], {"status": "failed", "kind": kind,
                                                 "error": f"{type(exc).__name__}"})
            continue
        stats.processed[lane] += len(group)
        for it, res in zip(group, results or [{}] * len(group)):
            await _set_ticket(it["ticket"], {"status": "done", "kind": kind, "result": res})


def _drain_local(limit: int) -> list[dict[str, Any]]:
    """Critical first, then standard, then bulk, up to `limit` items."""
    out: list[dict[str, Any]] = []
    for lane in LANES:
        q = _queues.get(lane)
        while q is not None and not q.empty() and len(out) < limit:
            out.append(q.get_nowait())
    return out


async def _local_worker() -> None:
    assert _wake is not None
    flush = max(1, settings.ingest_flush_ms) / 1000
    while True:
        batch = _drain_local(settings.ingest_batch_size)
        if not batch:
            _wake.clear()
            try:
                await asyncio.wait_for(_wake.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            continue
        if len(batch) < settings.ingest_batch_size:
            await asyncio.sleep(flush)          # let a batch fill before writing it
            batch += _drain_local(settings.ingest_batch_size - len(batch))
        await _process(batch)


async def _redis_worker() -> None:
    flush_ms = max(1, settings.ingest_flush_ms)
    last_claim = 0.0
    while True:
        r = shared.client()
        if r is None:
            await asyncio.sleep(1.0)
            continue
        try:
            for lane in LANES:
                try:
                    await r.xgroup_create(f"ingest:{lane}", GROUP, id="0", mkstream=True)
                except Exception:  # noqa: BLE001 - BUSYGROUP: it exists
                    pass
            items: list[tuple[str, str, dict]] = []
            if time.monotonic() - last_claim > CLAIM_IDLE_MS / 1000:
                last_claim = time.monotonic()
                for lane in LANES:
                    got = await r.xautoclaim(f"ingest:{lane}", GROUP, _consumer, CLAIM_IDLE_MS,
                                             start_id="0-0", count=settings.ingest_batch_size)
                    for mid, fields in (got[1] if got else []):
                        items.append((lane, mid, fields))
            for lane in LANES:
                room = settings.ingest_batch_size - len(items)
                if room <= 0:
                    break
                resp = await r.xreadgroup(GROUP, _consumer, {f"ingest:{lane}": ">"}, count=room,
                                          block=flush_ms if lane == "bulk" and not items else None)
                for _stream, entries in resp or []:
                    for mid, fields in entries:
                        items.append((lane, mid, fields))
            if not items:
                continue
            decoded = []
            for _lane, _mid, fields in items:
                raw = fields.get(b"d") or fields.get("d")
                decoded.append(json.loads(raw))
            await _process(decoded)
            for lane in LANES:
                ids = [mid for ln, mid, _ in items if ln == lane]
                if ids:
                    await r.xack(f"ingest:{lane}", GROUP, *ids)
                    await r.xdel(f"ingest:{lane}", *ids)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            shared.mark_down(exc)
            await asyncio.sleep(0.5)


async def start(workers: int | None = None) -> None:
    """Start the drainers. The API starts `INGEST_WORKERS` of them; the
    separate worker process starts more and the API can run with 0."""
    global _wake
    n = settings.ingest_workers if workers is None else workers
    if _workers or n <= 0:
        return
    _register_default_handlers()
    _wake = asyncio.Event()
    loop = asyncio.get_running_loop()
    for _ in range(n):
        _workers.append(loop.create_task(_local_worker()))
    if settings.redis_url:
        for _ in range(n):
            _workers.append(loop.create_task(_redis_worker()))
    log.info("ingest_bus_started", workers=n, backend="redis" if settings.redis_url else "memory",
             batch=settings.ingest_batch_size, flush_ms=settings.ingest_flush_ms)


async def stop(drain_s: float = 5.0) -> None:
    """Finish what was accepted (bounded), then stop."""
    deadline = time.monotonic() + drain_s
    while any(not q.empty() for q in _queues.values()) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    for t in _workers:
        t.cancel()
    for t in _workers:
        try:
            await t
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _workers.clear()


def status() -> dict[str, Any]:
    return {
        "backend": "redis" if shared.enabled() else "memory",
        "workers": len(_workers),
        "depth": {lane: (_queues[lane].qsize() if lane in _queues else 0) for lane in LANES},
        "capacity": {lane: _max_for(lane) for lane in LANES},
        "enqueued": dict(stats.enqueued), "processed": dict(stats.processed),
        "failed": dict(stats.failed), "shed": dict(stats.shed),
        "writtenThrough": stats.written_through, "duplicates": stats.duplicates,
        "avgBatch": round(stats.batch_items / stats.batches, 1) if stats.batches else 0,
        "avgLagMs": round(stats.lag_ms_total / stats.lag_samples, 1) if stats.lag_samples else 0,
        "batchSize": settings.ingest_batch_size, "flushMs": settings.ingest_flush_ms,
        "kinds": {k: v[0] for k, v in _HANDLERS.items()},
    }


def reset_for_tests() -> None:
    global stats
    stats = _Stats()
    _queues.clear()
    _idem._d.clear()
    _tickets._d.clear()


# ----------------------------------------------------------------- handlers ---
def _register_default_handlers() -> None:
    """Import-time cheap; the heavy modules load on first batch."""
    if "telemetry" in _HANDLERS and "report" in _HANDLERS:
        return

    @handler("telemetry", "bulk")
    async def _telemetry(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from app.iot import service as iot

        by_gw: dict[tuple[str, str], list[dict]] = defaultdict(list)
        index: list[tuple[tuple[str, str], int]] = []
        for p in payloads:
            k = (p.get("gatewayId") or "lora-gw", p.get("cityId") or "pune")
            index.append((k, len(by_gw[k])))
            by_gw[k].append(p["observation"])
        done: dict[tuple[str, str], list[dict]] = {}
        for (gw, city), obs in by_gw.items():
            res = await iot.ingest_bulk(obs, gateway_id=gw, city_id=city)
            done[(gw, city)] = res["results"]
        return [done[k][i] if i < len(done[k]) else {} for k, i in index]

    @handler("report", "critical")
    async def _report(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from datetime import datetime

        from app.incidents import intake

        sem = asyncio.Semaphore(8)

        async def one(p: dict[str, Any]) -> dict[str, Any]:
            try:
                return await _one(p)
            except Exception as exc:  # noqa: BLE001 - one bad report must not fail the batch
                log.warning("ingest_report_failed", error=f"{type(exc).__name__}: {str(exc)[:160]}")
                return {"error": type(exc).__name__}

        async def _one(p: dict[str, Any]) -> dict[str, Any]:
            async with sem:
                occurred = p.get("occurred_at")
                r = await intake.receive(
                    ward_id=p["ward_id"], category=p["category"],
                    location=(float(p["location"][0]), float(p["location"][1])),
                    note=p.get("note") or "", photo_url=p.get("photo_url"),
                    source=p.get("source") or "app", reporter_id=p.get("reporter_id"),
                    reporter_name=p.get("reporter_name") or "Anonymous",
                    device_id=p.get("device_id"),
                    occurred_at=datetime.fromisoformat(occurred) if occurred else None,
                    city_id=p.get("city_id") or "pune",
                )
                return {"reportId": r.report_id, "incidentId": r.incident_id,
                        "createdIncident": r.created_incident, "trust": r.trust.score,
                        "summary": r.summary}

        return list(await asyncio.gather(*(one(p) for p in payloads), return_exceptions=False))
