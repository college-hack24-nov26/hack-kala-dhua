"""Let the pure parts of the backend be tested on a machine without the
network-facing dependencies installed (asyncpg, structlog, tenacity, ...).

Only modules that are genuinely missing are stubbed; anything installed is
used for real. Nothing in these tests talks to a database or the network.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from unittest.mock import MagicMock

OPTIONAL = [
    "structlog", "structlog.contextvars", "structlog.processors", "structlog.stdlib",
    "structlog.dev", "structlog.types",
    "asyncpg", "asyncpg.pool", "tenacity", "orjson", "ortools", "ortools.sat",
    "ortools.sat.python", "ortools.sat.python.cp_model", "boto3", "botocore",
    "google.genai",
]


def install() -> None:
    for name in OPTIONAL:
        if name in sys.modules:
            continue
        try:
            if importlib.util.find_spec(name) is not None:
                continue
        except (ModuleNotFoundError, ValueError):
            pass
        mod = MagicMock(name=name)
        mod.__spec__ = types.SimpleNamespace(name=name, loader=None, origin="stub",
                                            submodule_search_locations=[])
        mod.__path__ = []
        sys.modules[name] = mod
    # ortools missing must look missing to allocation.py's lazy import, so it
    # takes its greedy path rather than a MagicMock solver.
    if isinstance(sys.modules.get("ortools"), MagicMock):
        for n in ("ortools", "ortools.sat", "ortools.sat.python", "ortools.sat.python.cp_model"):
            sys.modules.pop(n, None)
