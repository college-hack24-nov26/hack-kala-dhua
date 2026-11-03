"""Event-loop watchdog: when the loop stops answering, record where it is stuck.

Everything in the API shares one asyncio loop. A synchronous call on it (a solver,
a model, a big JSON parse) freezes every request, the event router and the
drone link at once, and from outside that looks like timeouts and CORS errors.

A coroutine stamps a heartbeat every 0.5 s. A daemon thread checks it; when the
heartbeat is older than LOOPWATCH_BLOCK_S (2 s) it samples the main thread's
stack (file:function:line only, no variables) and keeps the worst recent
blocks for GET /api/v1/status/loop.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
import traceback
from collections import deque

BLOCK_S = float(os.environ.get("LOOPWATCH_BLOCK_S", "2"))
_beat = time.monotonic()
_main_ident: int | None = None
_blocks: deque = deque(maxlen=20)
_current: dict | None = None
_started = False
_stats = {"blocks": 0, "longest_s": 0.0, "since": time.time()}


def _stack() -> list[str]:
    frame = sys._current_frames().get(_main_ident or 0)
    if frame is None:
        return []
    out = []
    for fs in traceback.extract_stack(frame)[-14:]:
        path = fs.filename.replace("\\", "/")
        short = path.split("/app/", 1)[-1] if "/app/" in path else "/".join(path.split("/")[-2:])
        out.append(f"{short}:{fs.name}:{fs.lineno}")
    return out


def _watch() -> None:
    global _current
    while True:
        time.sleep(0.25)
        lag = time.monotonic() - _beat
        if lag >= BLOCK_S:
            st = _stack()
            if _current is None:
                _current = {"at": time.time() - lag, "samples": {}}
            key = " <- ".join(reversed(st[-6:]))
            _current["samples"][key] = _current["samples"].get(key, 0) + 1
            _current["lag_s"] = round(lag, 1)
        elif _current is not None:
            blk, _current = _current, None
            top = sorted(blk["samples"].items(), key=lambda kv: -kv[1])[:3]
            _blocks.appendleft({"at": blk["at"], "blocked_s": blk.get("lag_s"), "where": [{"stack": k, "samples": n} for k, n in top]})
            _stats["blocks"] += 1
            _stats["longest_s"] = max(_stats["longest_s"], blk.get("lag_s") or 0)


async def _heartbeat() -> None:
    global _beat
    while True:
        _beat = time.monotonic()
        await asyncio.sleep(0.5)


def start() -> None:
    global _started, _main_ident
    if _started or os.environ.get("LOOPWATCH", "1") == "0":
        return
    _started = True
    _main_ident = threading.get_ident()
    asyncio.get_running_loop().create_task(_heartbeat())
    threading.Thread(target=_watch, daemon=True, name="loopwatch").start()


def status() -> dict:
    cur = None
    if _current is not None:
        cur = {"blocked_for_s": _current.get("lag_s"), "where": sorted(_current["samples"], key=lambda k: -_current["samples"][k])[:2]}
    return {"threshold_s": BLOCK_S, **_stats, "blocked_now": cur, "recent": list(_blocks)}
