"""Request middleware: correlation id, access log, timing, and the last-resort
error boundary.

The error boundary is here rather than only as an `@app.exception_handler`
because of where Starlette puts things. An app-level `Exception` handler runs in
`ServerErrorMiddleware`, which sits OUTSIDE the user middleware stack, so its
500 response never passes back through `CORSMiddleware` and carries no
`Access-Control-Allow-Origin` header. The browser then reports a CORS failure
and hides the actual error, which sends you looking at your CORS configuration
for a bug that is in a query.

Catching it here, in the innermost middleware, means the response still travels
out through CORS and the frontend sees the real status and message.

Pure ASGI since 2026-10, not `BaseHTTPMiddleware`. The base class wraps every
request in an extra task and two memory streams; on this API's hot paths that
overhead was larger than the handler. Measured on the same box: ~940 req/s
per worker through the old pair of middlewares, see docs/PRODUCTION_HARDENING.md for after.
Access logs are sampled for fast successful requests (`ACCESS_LOG_SAMPLE`);
errors, 4xx/5xx and slow requests are always logged.
"""

from __future__ import annotations

import random
import time
import uuid

from app.core.logging import get_logger, request_id_ctx

log = get_logger("http")

_QUIET = ("/health", "/health/live", "/metrics")
#: Successful requests faster than this are logged at ACCESS_LOG_SAMPLE.
SLOW_MS = 500.0


class RequestContextMiddleware:
    def __init__(self, app, sample: float = 1.0) -> None:
        self.app = app
        self.sample = max(0.0, min(1.0, sample))

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rid = ""
        for k, v in scope.get("headers") or ():
            if k == b"x-request-id":
                rid = v.decode("latin-1")[:64]
                break
        rid = rid or uuid.uuid4().hex[:16]
        token = request_id_ctx.set(rid)
        started = time.perf_counter()
        status_holder = {"status": 500, "started": False}
        rid_header = (b"x-request-id", rid.encode("latin-1"))

        async def send_with_id(message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                status_holder["started"] = True
                message["headers"] = list(message.get("headers") or []) + [rid_header]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception as exc:  # noqa: BLE001 - deliberate boundary
            elapsed = (time.perf_counter() - started) * 1000
            log.exception("request_failed", method=scope.get("method"), path=scope.get("path"),
                          duration_ms=round(elapsed, 1))
            if status_holder["started"]:
                raise
            from app.core.errors import problem_response

            response = problem_response(exc)
            response.headers["X-Request-ID"] = rid
            await response(scope, receive, send)
            return
        finally:
            request_id_ctx.reset(token)

        path = scope.get("path", "")
        if path in _QUIET:
            return
        elapsed = (time.perf_counter() - started) * 1000
        st = status_holder["status"]
        if st >= 400 or elapsed >= SLOW_MS or self.sample >= 1.0 or random.random() < self.sample:
            log.info("request", method=scope.get("method"), path=path, status=st,
                     duration_ms=round(elapsed, 1))
