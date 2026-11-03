"""Rate limiting for the endpoints a stranger can reach.

`slowapi` has been in `requirements.txt` since the beginning and was never
imported, which meant every public endpoint on this service was unmetered. That
matters more here than on a typical API, because of what the expensive ones do:

  * `POST /citizen/report` is the intake door. It is deliberately open to
    anonymous callers — somebody standing in water should not have to make an
    account first — and each call can reach a language model, write several
    rows, and open an incident that the allocator will then try to cover with a
    real vehicle. A loop around it is not a denial-of-service against the API,
    it is a denial-of-service against the fleet.
  * `report/voice` and `vision/analyse` each spend money per call on an upstream
    model.
  * `citizen/guide` runs the guidance agent and a Mapbox Directions request.

So the limits below are per caller and per path, and they are sized from what
the app itself does rather than from a round number. The citizen app re-solves a
route at most once every eight seconds behind its own cooldown, so thirty guide
calls a minute is far above anything the interface can produce and far below
what a script can.

Keyed per caller and per path. Middleware runs before authentication, so the
caller is identified by a hash of the bearer token when one is present and by
the forwarded client address when it is not — see `_caller` for why that is a
bucket label rather than a claim about identity.

The counters live in this process. With one worker per container that is the
whole service, and with several replicas each enforces its own share, so the
effective limit is per-replica rather than global. That is a real limitation and
the right trade for now: the alternative is a Redis round trip on the hot path
of a disaster response API, to make a limit that exists to stop scripts slightly
more precise against scripts.

Update 2026-10: with `REDIS_URL` set the limit is global across replicas (two
fixed windows weighted into a sliding estimate, one pipelined round trip, only
on the metered paths); without it, per replica as before. Pure ASGI, and the
new ingest doors are metered too.
"""

from __future__ import annotations

import hashlib
import time
from collections import deque
from threading import Lock

from starlette.requests import Request

from app.core.errors import _problem as problem_document
from app.core.logging import get_logger

log = get_logger("ratelimit")

#: path suffix -> (requests, per seconds). Matched with `endswith`, so the
#: `/api/v1` prefix does not have to be repeated.
LIMITS: dict[str, tuple[int, int]] = {
    "/citizen/report/voice": (6, 60),      # Sarvam, per call
    "/citizen/vision/analyse": (6, 60),    # the VLM, per call
    "/citizen/report": (10, 60),           # opens incidents
    "/citizen/arrived": (10, 60),          # writes occupancy
    # 60, not 30. The resident app re-solves on its own whenever the person
    # drifts off the line or a hazard near the route changes, on top of whatever
    # they press — so the legitimate rate for one walking person is already
    # several a minute, and the client's own cooldown is the real limiter here.
    "/citizen/guide": (60, 60),            # Mapbox + the guidance agent
    "/citizen/ask": (20, 60),              # the model, per call (cached per ward)
    "/ingest/reports": (10, 60),           # the queued intake door, same allowance
    "/copilot/ask": (60, 60),              # staff, but each one can reach a model
}

#: Stop the key space growing without bound when the callers are anonymous and
#: numerous. Oldest bucket is dropped first; dropping one costs that caller a
#: fresh allowance, which is the correct failure direction.
MAX_TRACKED = 20_000


class _Windows:
    """Sliding windows, one deque of timestamps per (caller, path).

    Small enough to be worth having in the file that explains it. A fixed window
    lets somebody send the whole allowance at 59.9s and again at 60.1s; a
    sliding one does not, and at these volumes the memory difference is a few
    hundred kilobytes.
    """

    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], deque[float]] = {}
        self._lock = Lock()

    def allow(self, key: tuple[str, str], limit: int, window: int) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            if len(self._hits) > MAX_TRACKED:
                for k in list(self._hits)[: MAX_TRACKED // 4]:
                    self._hits.pop(k, None)
            q = self._hits.setdefault(key, deque())
            cutoff = now - window
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) >= limit:
                retry = int(q[0] + window - now) + 1
                return False, max(retry, 1)
            q.append(now)
            return True, 0


_windows = _Windows()


def _limit_for(path: str) -> tuple[str, int, int] | None:
    for suffix, (limit, window) in LIMITS.items():
        if path.endswith(suffix):
            return suffix, limit, window
    return None


def _caller(request: Request) -> str:
    """Who to count against.

    Middleware runs before the dependency that authenticates a request, so there
    is no verified user id here to key on. The bearer token is used instead —
    not as proof of anything, only as a bucket label, because two signed-in
    people behind one municipal NAT should not share an allowance. It is hashed
    rather than kept: this is a dictionary key, and a table of live access tokens
    sitting in process memory is a worse problem than the one being solved.

    No token, or no match, and it falls back to the forwarded address. Render
    terminates TLS at its edge, so the socket address is the edge and
    `X-Forwarded-For` is the caller — the same reason uvicorn runs with
    `--proxy-headers`.
    """
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and len(auth) > 24:
        return "t:" + hashlib.sha256(auth[7:].encode()).hexdigest()[:24]

    ip = (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )

    # The resident app needs no account, so every anonymous phone behind one
    # municipal NAT used to be one caller with one allowance. At a demo that is
    # every phone in the room sharing thirty guidance calls a minute, and the
    # first person to be refused is refused mid-sentence.
    #
    # `X-Indradhanu-Device` is the same per-install id the trust scorer already
    # counts reports against. It is not a credential and is not treated as one:
    # it is appended to the address rather than replacing it, so a caller can
    # still be found by IP in the log. The honest trade is that a script can
    # mint device ids and buy itself more buckets — which is a real weakening,
    # accepted because these limits exist to slow scripts down and not to
    # authorise anybody, and because punishing everyone who shares an IP is the
    # worse failure of the two.
    device = request.headers.get("X-Indradhanu-Device", "")
    if device and len(device) <= 64:
        return f"ip:{ip}|d:{hashlib.sha256(device.encode()).hexdigest()[:12]}"
    return f"ip:{ip}"


async def _allow_shared(key: tuple[str, str], limit: int, window: int) -> tuple[bool, int] | None:
    """Global across replicas, or None when Redis is not in use."""
    from app.core import shared

    r = shared.client()
    if r is None:
        return None
    try:
        now = time.time()
        slot = int(now // window)
        base = "rl:" + hashlib.sha1(f"{key[0]}|{key[1]}".encode()).hexdigest()[:20]
        pipe = r.pipeline()
        pipe.incr(f"{base}:{slot}")
        pipe.expire(f"{base}:{slot}", window * 2)
        pipe.get(f"{base}:{slot - 1}")
        cur, _, prev = await pipe.execute()
        weight = 1 - (now % window) / window
        estimate = int(cur) + int(prev or 0) * weight
        if estimate > limit:
            return False, max(1, int(window - now % window))
        return True, 0
    except Exception as exc:  # noqa: BLE001 - fall back to per-replica
        shared.mark_down(exc)
        return None


class RateLimitMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return
        found = _limit_for(scope.get("path", ""))
        if found is None:
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        suffix, limit, window = found
        caller = _caller(request)
        got = await _allow_shared((caller, suffix), limit, window)
        ok, retry = got if got is not None else _windows.allow((caller, suffix), limit, window)
        if ok:
            await self.app(scope, receive, send)
            return

        log.warning("rate_limited", path=scope.get("path"), caller=caller,
                    limit=limit, window=window)
        response = problem_document(
            status_code=429,
            problem="rate-limited",
            title="Too many requests",
            # Said plainly, because a resident who is told "429" learns nothing
            # and a resident who is told to wait will.
            detail=(
                f"That is more than {limit} requests a minute to this endpoint. "
                f"Wait {retry} second(s) and try again. If this is urgent and "
                "the app will not send, call 112."
            ),
            retryAfter=retry,
        )
        response.headers["Retry-After"] = str(retry)
        await response(scope, receive, send)
