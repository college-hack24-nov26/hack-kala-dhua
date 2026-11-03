"""Run the real API app (real middleware, real routers) without its database,
for throughput measurement of the HTTP path and the ingest bus's accept path.

    python scripts/bench_server.py --port 8099 [--workers 1]

The lifespan is replaced: no DB pool, no router, no demo. Ingest handlers are
replaced by a no-op that counts, so what is measured is accept + enqueue +
batch drain, not Postgres. Never used in production.
"""
from __future__ import annotations

import argparse
import os
import sys
from contextlib import asynccontextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MESH_GATEWAY_KEY", "bench")
os.environ.setdefault("INDRADHANU_ENV", "development")
os.environ.setdefault("LOG_LEVEL", "WARNING")


def build():
    from app import main
    from app.core import ingest

    @asynccontextmanager
    async def lifespan(_app):
        processed = {"n": 0}

        async def noop(payloads):
            processed["n"] += len(payloads)
            return [{} for _ in payloads]
        ingest._register_default_handlers()
        for kind in list(ingest._HANDLERS):
            lane, _ = ingest._HANDLERS[kind]
            ingest._HANDLERS[kind] = (lane, noop)
        await ingest.start()
        yield
        await ingest.stop()

    main.app.router.lifespan_context = lifespan
    return main.app


app = None
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--workers", type=int, default=1)
    a = ap.parse_args()
    import uvicorn

    uvicorn.run("scripts.bench_server:make", factory=True, host="127.0.0.1", port=a.port,
                workers=a.workers, loop="uvloop", http="httptools", log_level="warning",
                access_log=False, backlog=4096)


def make():
    return build()
