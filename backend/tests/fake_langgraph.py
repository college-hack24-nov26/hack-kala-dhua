"""A small stand-in for `langgraph`, enough to execute app/agents/graph.py in a
test run where the real package cannot be installed. It implements the
semantics the graph relies on: supersteps, reducers from `Annotated`, static
and conditional edges, `Send` fan-out, `interrupt()` + `Command(resume=...)`
against a checkpointer keyed by thread id, and `aget_state`.

It is not langgraph. The production path imports the real one."""
from __future__ import annotations

import asyncio
import contextvars
import inspect
import sys
import types
from dataclasses import dataclass, field
from typing import Any, get_type_hints

START, END = "__start__", "__end__"
_resume: contextvars.ContextVar = contextvars.ContextVar("resume", default=None)


@dataclass
class Send:
    node: str
    arg: Any


@dataclass
class Command:
    resume: Any = None


class _Interrupt(Exception):
    def __init__(self, value: Any) -> None:
        self.value = value


@dataclass
class Interrupt:
    value: Any


def interrupt(value: Any) -> Any:
    box = _resume.get()
    if box is not None:
        return box[0]
    raise _Interrupt(value)


@dataclass
class _Task:
    name: str
    interrupts: tuple = ()


@dataclass
class _Snapshot:
    values: dict
    next: tuple
    tasks: tuple


class MemorySaver:
    def __init__(self) -> None:
        self.threads: dict[str, dict] = {}


InMemorySaver = MemorySaver


class StateGraph:
    def __init__(self, schema: Any) -> None:
        self.reducers = {}
        for k, t in get_type_hints(schema, include_extras=True).items():
            meta = getattr(t, "__metadata__", ())
            if meta and callable(meta[0]):
                self.reducers[k] = meta[0]
        self.nodes: dict[str, Any] = {}
        self.edges: dict[str, list[str]] = {}
        self.cond: dict[str, tuple] = {}

    def add_node(self, name, fn):
        self.nodes[name] = fn

    def add_edge(self, a, b):
        self.edges.setdefault(a, []).append(b)

    def add_conditional_edges(self, a, fn, path_map=None):
        self.cond[a] = (fn, path_map)

    def compile(self, checkpointer=None):
        return _Compiled(self, checkpointer or MemorySaver())


class _Compiled:
    def __init__(self, g: StateGraph, saver: MemorySaver) -> None:
        self.g, self.saver = g, saver

    def _apply(self, state: dict, upd: dict | None) -> None:
        for k, v in (upd or {}).items():
            r = self.g.reducers.get(k)
            state[k] = r(state.get(k) or [], v) if r else v

    def _next(self, name: str, state: dict) -> list:
        out: list = []
        for b in self.g.edges.get(name, []):
            out.append(b)
        if name in self.g.cond:
            fn, pm = self.g.cond[name]
            res = fn(state)
            for x in (res if isinstance(res, list) else [res]):
                if isinstance(x, Send):
                    out.append(x)
                else:
                    out.append(pm[x] if isinstance(pm, dict) else x)
        return out

    async def _run(self, tasks: list, state: dict, thread: dict, resume_box=None) -> None:
        while tasks:
            async def one(t):
                name, arg = (t.node, t.arg) if isinstance(t, Send) else (t, dict(state))
                fn = self.g.nodes[name]
                tok = _resume.set(resume_box)
                try:
                    r = fn(arg)
                    if inspect.isawaitable(r):
                        r = await r
                    return name, r, None
                except _Interrupt as i:
                    return name, None, i
                finally:
                    _resume.reset(tok)
            results = await asyncio.gather(*(one(t) for t in tasks))
            resume_box = None
            pending = [(t, i) for t, (_, _, i) in zip(tasks, results) if i]
            for name, upd, i in results:
                if not i:
                    self._apply(state, upd)
            if pending:
                thread["state"], thread["pending"] = state, pending
                return
            nxt: list = []
            seen: set = set()
            for name, _, _ in results:
                for x in self._next(name, state):
                    if isinstance(x, Send):
                        nxt.append(x)
                    elif x != END and x not in seen:
                        seen.add(x)
                        nxt.append(x)
            tasks = nxt
        thread["state"], thread["pending"] = state, []

    async def ainvoke(self, payload, config):
        tid = config["configurable"]["thread_id"]
        thread = self.saver.threads.setdefault(tid, {"state": {}, "pending": []})
        if isinstance(payload, Command):
            tasks = [t for t, _ in thread["pending"]]
            await self._run(tasks, thread["state"], thread, resume_box=(payload.resume,))
        else:
            state: dict = {}
            self._apply(state, payload)
            await self._run(self._next(START, state), state, thread)
        return dict(thread["state"])

    async def aget_state(self, config):
        thread = self.saver.threads.get(config["configurable"]["thread_id"], {"state": {}, "pending": []})
        tasks = tuple(_Task(t.node if isinstance(t, Send) else t, (Interrupt(i.value),))
                      for t, i in thread["pending"])
        return _Snapshot(values=dict(thread["state"]), next=tuple(t.name for t in tasks), tasks=tasks)

    def get_graph(self):
        raise NotImplementedError


def install() -> None:
    root = types.ModuleType("langgraph")
    graph = types.ModuleType("langgraph.graph")
    graph.StateGraph, graph.START, graph.END = StateGraph, START, END
    t = types.ModuleType("langgraph.types")
    t.Send, t.Command, t.interrupt = Send, Command, interrupt
    cp = types.ModuleType("langgraph.checkpoint")
    mem = types.ModuleType("langgraph.checkpoint.memory")
    mem.InMemorySaver = mem.MemorySaver = MemorySaver
    for name, mod in {"langgraph": root, "langgraph.graph": graph, "langgraph.types": t,
                      "langgraph.checkpoint": cp, "langgraph.checkpoint.memory": mem}.items():
        sys.modules[name] = mod
