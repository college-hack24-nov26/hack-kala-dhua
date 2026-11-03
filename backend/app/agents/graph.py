"""The agent graph, on LangGraph.

This is the cycle the event router runs whenever the world changes (a report,
an incident, a road block, a crew update). It does the CP-SAT solve with the
steps visible, a human-approval stop for the decisions that should not be
automatic, a checkpoint per step, and three kinds of control around every
agent:

  **Strict state.** Each node is declared with the state keys it may READ and
  the keys it may WRITE (`@contract`). It is handed a read-only view of only its
  reads; anything it returns is checked against its writes and validated
  against the schema (pydantic) before LangGraph merges it. A node that writes a
  key it does not own, or a value of the wrong shape, fails the step instead of
  corrupting the run. Fan-out nodes (`sense`, `domain`) get a validated payload
  and never see the shared state at all.

  **Guardrails** (app/agents/guardrails.py). Input: any text headed for a model
  is cleaned, PII-redacted and injection-checked. Output: model answers must
  pass a schema. Action: a plan is checked for double-assigned units, how many
  units it moves in one cycle, and whether coverage drops; the last two send it
  to an officer. Run: every run has a timeout and a recursion limit.

  **Per-agent memory** (app/agents/agent_memory.py). Each agent has its own
  namespace and an access list; officers' standing orders are readable by all
  and writable by none of them; reads, writes and refusals go to a ledger. The
  memory is used, not decorative: a domain planner that has been short of the
  same capability three cycles running says so and flags mutual aid; the gate
  remembers an officer's rejection and does not ask the same question again for
  thirty minutes.

    START
      └─ guard_in ─(kill switch / run storm)─► held ─► END
            └─ triage ─(S≥5)─► command ─┐
            └──────────────────────┴─► fanout ─Send×4─► sense ─► assess
                                         └─► aerial (tasks the drone swarm) ─► END
                                                                   │
                         ┌──────────Send per domain with a need────┘
                         ▼
                      domain ─► optimise (CP-SAT dry run) ─► validate ─fail<3─► optimise
                                                               │fail×3 ─► give_up ─► END
                                                               ▼
                                                          policy_gate
                    ┌────────────────┬───────────────┬─────────┴──────────┐
                    ▼                ▼               ▼                    ▼
             human_approval       dispatch         held               observe
             (interrupt) ─no─► rejected ─► END     └─► END          (nothing changed)
                    └──yes──► dispatch ─► observe ─► END

Guardrails as graph structure, not just helper calls (2026-10): `guard_in` is
the first node of every run; `validate` runs the action invariants; the
`policy_gate` refuses to strand an S3+ incident without an officer; `dispatch`
re-checks that the plan it is about to write is the plan that was approved
(drift) and audits what it wrote. Every firing lands in the run's
`guardrail_trips`, in the console, and in the event log as `guardrail.tripped`.

When `langgraph` is not installed the router falls back to the direct re-plan,
so the system never depends on this module to function.
"""

from __future__ import annotations

import asyncio
import inspect
import operator
import time
import uuid
from dataclasses import asdict, dataclass, field
from types import MappingProxyType
from typing import Annotated, Any, Callable, TypedDict, get_type_hints

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app.agents import guardrails
from app.agents.agent_memory import MemoryAccessDenied, ScopedMemory
from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

try:  # pragma: no cover - exercised when the dependency is installed
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command, Send, interrupt

    try:
        from langgraph.checkpoint.memory import InMemorySaver as _MemorySaver
    except ImportError:  # older langgraph
        from langgraph.checkpoint.memory import MemorySaver as _MemorySaver
    AVAILABLE = True
    IMPORT_ERROR: str | None = None
except Exception as exc:  # noqa: BLE001 - the app must boot without it
    AVAILABLE = False
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

ACTOR = "agent:langgraph"
MAX_ATTEMPTS = 3
SEVERE = 5
RUN_TIMEOUT_S = 90
RECURSION_LIMIT = 40
#: How long an officer's rejection holds the same re-tasking back.
REJECTION_MEMORY_S = 30 * 60
#: A domain short of the same capability this many recent cycles flags mutual aid.
PERSISTENT_SHORTFALL = 3
#: More cycles than this for one city inside a minute is a storm, not a
#: situation: the extra runs are coalesced (the router's next cycle covers them).
MAX_RUNS_PER_MIN = 20
#: An incident at or above this severity is never left with no unit by the
#: automated loop alone.
STRAND_SEVERITY = 3

DOMAINS: dict[str, str] = {
    "water_rescue": "rescue", "search_rescue": "rescue", "field_assessment": "rescue",
    "fire_suppression": "rescue",
    "medical_transport": "medical", "medical_care": "medical",
    "dewatering": "logistics", "debris_clearance": "logistics",
    "supply_delivery": "logistics", "mass_transport": "logistics",
    "water_supply": "logistics",
}
SOURCES = ("units", "needs", "risk", "facilities", "sensors")


# ----------------------------------------------------------------- schemas ---
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Step(BaseModel):
    model_config = ConfigDict(extra="allow")
    node: str
    text: str = Field(max_length=600)
    at: float


class ChangeM(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: str
    resource_id: str
    resource_label: str = ""
    incident_id: str | None = None
    incident_title: str = ""
    ward_id: str = ""
    from_incident_id: str | None = None
    from_incident_title: str = ""
    eta_minutes: int = 0
    reason: str = ""


class Proposal(BaseModel):
    model_config = ConfigDict(extra="ignore")
    plan_id: str | None = None
    engine: str = ""
    runtime_ms: int = 0
    coverage: float = Field(default=1.0, ge=0.0, le=1.0)
    kept: list[ChangeM] = Field(default_factory=list)
    assigned: list[ChangeM] = Field(default_factory=list)
    reassigned: list[ChangeM] = Field(default_factory=list)
    released: list[ChangeM] = Field(default_factory=list)
    uncovered: list[dict[str, Any]] = Field(default_factory=list)
    changed: int = 0
    headline: str = ""
    error: str | None = None


class Approval(_Strict):
    """What comes back from the console. Validated strictly: it is outside input."""
    approved: bool
    by: str = Field(default="officer", max_length=80)
    note: str | None = Field(default=None, max_length=300)


class SensePayload(_Strict):
    source: str
    city_id: str
    run_id: str


class DomainPayload(_Strict):
    domain: str
    needs: dict[str, int]
    units: dict[str, Any] = Field(default_factory=dict)
    city_id: str
    run_id: str


# ------------------------------------------------------------------- state ---
class GraphState(TypedDict, total=False):
    run_id: str
    city_id: str
    trigger: str
    max_severity: int
    commander_woken: bool
    orders_in_force: int
    situation: Annotated[list[dict[str, Any]], operator.add]
    current_coverage: float
    shortfall: dict[str, int]
    domain_plans: Annotated[list[dict[str, Any]], operator.add]
    attempts: int
    proposal: dict[str, Any]
    valid: bool
    validation: str
    needs_officer: bool
    held: bool
    gate_reason: str
    approval: dict[str, Any]
    dispatched: dict[str, Any]
    outcome: str
    trigger_injection: bool
    guard_ok: bool
    guardrail_trips: Annotated[list[dict[str, Any]], operator.add]
    trace: Annotated[list[dict[str, Any]], operator.add]
    aerial: dict[str, Any]


_HINTS = get_type_hints(GraphState, include_extras=True)
_ADAPTERS = {k: TypeAdapter(v) for k, v in _HINTS.items()}
#: Keys every node may read.
_ALWAYS = ("run_id", "city_id", "trigger")


class StateViolation(RuntimeError):
    """A node wrote something it does not own, or of the wrong shape."""


def _validate_update(node: str, update: Any, writes: frozenset[str]) -> dict[str, Any]:
    if update is None:
        return {}
    if not isinstance(update, dict):
        raise StateViolation(f"{node} returned {type(update).__name__}, not a state update")
    extra = set(update) - writes - {"trace", "guardrail_trips"}
    if extra:
        raise StateViolation(f"{node} may not write {sorted(extra)}")
    for k, v in update.items():
        try:
            _ADAPTERS[k].validate_python(v)
            if k == "proposal":
                Proposal.model_validate(v)
            elif k == "approval":
                Approval.model_validate(v)
            elif k == "trace":
                TypeAdapter(list[Step]).validate_python(v)
        except ValidationError as exc:
            raise StateViolation(f"{node} wrote an invalid {k}: {exc.errors()[:1]}") from None
    return update


def contract(node: str, *, reads: tuple[str, ...] = (), writes: tuple[str, ...] = (),
             agent: str | None = None, payload: type[BaseModel] | None = None) -> Callable:
    """Declare what a node may see and change; enforce it on every call.

    The wrapped function is called as `fn(view, memory)`: `view` is a read-only
    mapping of only the declared reads (or the validated payload for a fan-out
    node), `memory` the agent's `ScopedMemory` (or None)."""
    allowed = frozenset(writes)

    def deco(fn: Callable) -> Callable:
        # No functools.wraps: LangGraph inspects a node's signature for the
        # parameters it injects, and the inner (view, memory) signature must
        # not leak through.
        async def run(inp: dict[str, Any]) -> dict[str, Any]:
            if payload is not None:
                view: Any = MappingProxyType(payload.model_validate(inp).model_dump())
            else:
                view = MappingProxyType({k: inp[k] for k in (*_ALWAYS, *reads) if k in inp})
            mem = (ScopedMemory(agent, city_id=view.get("city_id", "pune"), run_id=view.get("run_id"))
                   if agent else None)
            out = fn(view, mem)
            if inspect.isawaitable(out):
                out = await out
            return _validate_update(node, out, allowed)

        run.__name__ = run.__qualname__ = fn.__name__
        run.__doc__ = fn.__doc__
        run.__contract__ = {"node": node, "reads": reads, "writes": writes, "agent": agent}  # type: ignore[attr-defined]
        return run
    return deco


def _step(node: str, text: str, **extra: Any) -> list[dict[str, Any]]:
    return [{"node": node, "text": text[:600], "at": time.time(), **extra}]


def _trip(rule: str, detail: str, *, run_id: str = "", blocking: bool = True) -> list[dict[str, Any]]:
    """Record a guardrail firing in the shared ledger and hand back the state entry."""
    t = guardrails.trip(rule, detail, subject=run_id, blocking=blocking)
    return [{"rule": t.rule, "layer": t.layer, "detail": t.detail, "blocking": t.blocking, "at": t.at}]


def _diff_dict(diff: Any) -> dict[str, Any]:
    if diff is None:
        return {}
    d = asdict(diff) if hasattr(diff, "__dataclass_fields__") else dict(diff)
    d["changed"] = len(d.get("assigned", [])) + len(d.get("reassigned", [])) + len(d.get("released", []))
    try:
        d["headline"] = diff.headline
    except Exception:  # noqa: BLE001
        d["headline"] = ""
    return Proposal.model_validate(d).model_dump()


# ------------------------------------------------------------------- nodes ---
@contract("guard_in", reads=("trigger_injection",), writes=("guard_ok", "gate_reason", "outcome"))
def guard_in(view, _mem) -> dict[str, Any]:
    """The first node of every run: may this cycle run at all?"""
    from app.ops import autonomy

    run_id = view.get("run_id", "")
    trips: list[dict[str, Any]] = []
    if view.get("trigger_injection"):
        # The trigger is a label in the trace and the Commander's summary; it
        # was cleaned in run_cycle. Recorded, not blocking: the re-plan itself
        # is deterministic and reads nothing from it.
        trips += _trip("graph.trigger_injection", "instruction-like text in the trigger was neutralised",
                       run_id=run_id, blocking=False)
    if autonomy.paused():
        reason = f"Automation paused by {autonomy.state.by or 'an officer'}; nothing is sensed or written."
        return {"guard_ok": False, "gate_reason": reason, "outcome": "held",
                "guardrail_trips": trips + _trip("graph.kill_switch", reason, run_id=run_id),
                "trace": _step("guard_in", reason)}
    now = time.time()
    recent = sum(1 for r in RUNS.values()
                 if r.city_id == view["city_id"] and now - r.started_at < 60 and r.run_id != run_id)
    if recent >= MAX_RUNS_PER_MIN:
        reason = f"{recent} cycles for {view['city_id']} in the last minute; coalesced into the next."
        return {"guard_ok": False, "gate_reason": reason, "outcome": "held",
                "guardrail_trips": trips + _trip("graph.run_storm", reason, run_id=run_id),
                "trace": _step("guard_in", reason)}
    return {"guard_ok": True, "guardrail_trips": trips,
            "trace": _step("guard_in", "Input guard passed: autonomy on, run rate normal"
                           + (", trigger neutralised." if trips else "."))}


def after_guard_in(state: GraphState) -> str:
    return "triage" if state.get("guard_ok") else "held"


@contract("triage", writes=("max_severity", "attempts", "orders_in_force"), agent="triage")
async def triage(view, mem: ScopedMemory) -> dict[str, Any]:
    from app.db import session as db

    sev = int(await db.fetchval(
        "select coalesce(max(severity), 0) from incidents where city_id = $1 and status <> 'resolved'",
        view["city_id"],
    ) or 0)
    orders = await mem.recall(["orders"], limit=20)
    await mem.remember("triage", f"Cycle for {view['trigger']}: worst open S{sev}",
                       data={"severity": sev}, ttl_minutes=60, importance=2)
    return {"max_severity": sev, "attempts": 0, "orders_in_force": len(orders),
            "trace": _step("triage", f"Trigger: {view['trigger']}. Worst open incident S{sev}. "
                                     f"{len(orders)} standing order(s) in force.")}


def after_triage(state: GraphState) -> str:
    return "command" if state.get("max_severity", 0) >= SEVERE else "fanout"


@contract("command", reads=("max_severity",), writes=("commander_woken",))
async def command(view, _mem) -> dict[str, Any]:
    from app.agents import commander

    summary = guardrails.clean_input(
        f"Severity {view.get('max_severity')} incident open; {view['trigger']}", limit=240)
    woke = commander.nudge("graph.severe", summary.text, key=f"graph:{view['city_id']}",
                           city_id=view["city_id"])
    return {"commander_woken": bool(woke),
            "trace": _step("command", "Incident Commander woken." if woke
                           else "Incident Commander already working (debounced).")}


@contract("fanout")
def fanout(view, _mem) -> dict[str, Any]:
    return {"trace": _step("fanout", f"Reading {len(SOURCES)} sources in parallel.")}


def to_sensors(state: GraphState) -> list[Any]:
    return [Send("sense", {"source": s, "city_id": state["city_id"], "run_id": state["run_id"]})
            for s in SOURCES]


@contract("sense", writes=("situation",), payload=SensePayload)
async def sense(view, _mem) -> dict[str, Any]:
    from app.db import session as db

    src, city = view["source"], view["city_id"]
    if src == "units":
        row = await db.fetchrow(
            """select count(*) total, count(*) filter (where status::text = 'available') available
                 from resources where city_id = $1""", city)
        data = {"total": int(row["total"] or 0), "available": int(row["available"] or 0)} if row else {}
        text = f"{data.get('available', 0)} of {data.get('total', 0)} units available."
    elif src == "needs":
        rows = await db.fetch(
            """select n.capability_id, sum(n.required) required, sum(coalesce(n.met, 0)) met
                 from incident_needs n join incidents i on i.id = n.incident_id
                where i.city_id = $1 and i.status <> 'resolved'
                group by n.capability_id""", city)
        data = {r["capability_id"]: {"required": int(r["required"] or 0), "met": int(r["met"] or 0)}
                for r in rows}
        text = f"{len(data)} capabilities in demand."
    elif src == "risk":
        rows = await db.fetch(
            "select distinct on (ward_id) ward_id, severity from ward_risks order by ward_id, created_at desc")
        data = {"severe_wards": [r["ward_id"] for r in rows if (r["severity"] or 0) >= 4]}
        text = f"{len(data['severe_wards'])} wards at risk severity 4+."
    elif src == "sensors":
        from app.iot import service as iot
        try:
            field_by_ward = await iot.ward_field(city)
        except Exception:  # noqa: BLE001 - sensors are evidence, never a dependency
            field_by_ward = {}
        hot = sorted(((w, f) for w, f in field_by_ward.items() if f["overall"] >= 0.4),
                     key=lambda wf: -wf[1]["overall"])
        data = {"live_nodes": sum(f["nodes"] for f in field_by_ward.values()),
                "wards_covered": len(field_by_ward),
                "hot_wards": [{"ward_id": w, "overall": round(f["overall"], 2),
                               "human": round(f["human"], 2), "structural": round(f["structural"], 2),
                               "environmental": round(f["environmental"], 2), "node": f["top"],
                               "flags": f["flags"][:5]} for w, f in hot[:8]]}
        text = (f"{data['live_nodes']} field sensors live over {data['wards_covered']} wards; "
                + (", ".join(f"{h['ward_id']} {h['overall']:.0%}" for h in data["hot_wards"][:3])
                   + " flagging." if hot else "nothing flagging."))
    else:
        row = await db.fetchrow(
            """select count(*) filter (where capacity > 0 and occupancy >= capacity) full_, count(*) total
                 from lifelines where city_id = $1""", city)
        data = {"full": int(row["full_"] or 0) if row else 0, "total": int(row["total"] or 0) if row else 0}
        text = f"{data['full']} of {data['total']} facilities full."
    return {"situation": [{"source": src, "data": data}], "trace": _step("sense", text, source=src)}


@contract("assess", reads=("situation",), writes=("shortfall", "current_coverage"))
def assess(view, _mem) -> dict[str, Any]:
    needs = next((s["data"] for s in view.get("situation", []) if s["source"] == "needs"), {})
    short = {cap: v["required"] - min(v["met"], v["required"])
             for cap, v in needs.items() if v["required"] > v["met"]}
    req = sum(v["required"] for v in needs.values())
    met = sum(min(v["met"], v["required"]) for v in needs.values())
    text = ("Unmet: " + ", ".join(f"{q}× {c}" for c, q in short.items())) if short else \
        "Every need is covered by the current plan."
    return {"shortfall": short, "current_coverage": (met / req) if req else 1.0,
            "trace": _step("assess", text)}


def to_domains(state: GraphState) -> list[Any] | str:
    short = state.get("shortfall") or {}
    by: dict[str, dict[str, int]] = {}
    for cap, q in short.items():
        by.setdefault(DOMAINS.get(cap, "logistics"), {})[cap] = q
    if not by:
        return "optimise"
    units = next((s["data"] for s in state.get("situation", []) if s["source"] == "units"), {})
    return [Send("domain", {"domain": d, "needs": caps, "units": units,
                            "city_id": state["city_id"], "run_id": state["run_id"]})
            for d, caps in by.items()]


@contract("domain", writes=("domain_plans",), payload=DomainPayload)
async def domain(view, _mem) -> dict[str, Any]:
    """A domain planner. Sees only its payload; remembers in its own namespace."""
    d, needs = view["domain"], dict(view["needs"])
    mem = ScopedMemory(d, city_id=view["city_id"], run_id=view["run_id"])
    recent = await mem.recall([d], limit=20, max_age_s=6 * 3600)
    persistent = sorted(c for c in needs
                        if sum(1 for m in recent if c in (m.get("data") or {}).get("needs", {})) + 1
                        >= PERSISTENT_SHORTFALL)
    await mem.remember(d, f"{d} short: " + ", ".join(f"{q}× {c}" for c, q in needs.items()),
                       data={"needs": needs}, ttl_minutes=360, importance=2)
    total = sum(needs.values())
    avail = int((view.get("units") or {}).get("available") or 0)
    note = (f"{d}: {total} unit(s) short ({', '.join(needs)}); "
            + ("free units exist, the solver can cover some." if avail else
               "no free units: coverage depends on re-tasking."))
    if persistent:
        note += f" Short of {', '.join(persistent)} for {PERSISTENT_SHORTFALL}+ cycles: request mutual aid."
    return {"domain_plans": [{"domain": d, "needs": needs, "note": note, "mutual_aid": persistent}],
            "trace": _step("domain", note, domain=d)}


@contract("optimise", reads=("attempts",), writes=("attempts", "proposal"), agent="planner")
async def optimise(view, mem: ScopedMemory) -> dict[str, Any]:
    """CP-SAT, as a dry run: the exact plan, nothing written yet."""
    from app.agents import replan as replanner

    attempt = int(view.get("attempts") or 0) + 1
    try:
        diff = await replanner.preview(city_id=view["city_id"], trigger=view["trigger"], actor=ACTOR)
        proposal = _diff_dict(diff)
        await mem.remember("planner", f"Plan: {proposal['headline']}",
                           data={"coverage": proposal["coverage"], "changed": proposal["changed"]},
                           ttl_minutes=120, importance=1)
        text = f"Attempt {attempt}: {proposal['headline'] or 'plan computed'} (coverage {proposal['coverage']:.0%})."
        return {"attempts": attempt, "proposal": proposal, "trace": _step("optimise", text)}
    except Exception as exc:  # noqa: BLE001 - validate decides what to do
        return {"attempts": attempt, "proposal": Proposal(error=str(exc)[:300]).model_dump(),
                "trace": _step("optimise", f"Attempt {attempt} failed: {str(exc)[:160]}")}


@contract("validate", reads=("proposal", "current_coverage"), writes=("valid", "validation", "needs_officer"))
def validate(view, _mem) -> dict[str, Any]:
    p = view.get("proposal") or {}
    if p.get("error"):
        return {"valid": False, "validation": p["error"], "trace": _step("validate", "Solver error.")}
    check = guardrails.check_plan(p, current_coverage=view.get("current_coverage"))
    if not check.ok:
        return {"valid": False, "validation": "; ".join(check.reasons),
                "guardrail_trips": [{"rule": "plan.invalid", "layer": "action", "detail": r,
                                     "blocking": True, "at": time.time()} for r in check.reasons],
                "trace": _step("validate", "Rejected by guardrail: " + "; ".join(check.reasons))}
    text = f"Plan valid; {len(p.get('uncovered', []))} need(s) still unmet."
    if check.needs_officer:
        text += " Guardrail: " + "; ".join(check.reasons) + "."
    return {"valid": True, "validation": "; ".join(check.reasons) or "ok",
            "needs_officer": check.needs_officer,
            "guardrail_trips": [{"rule": "plan.needs_officer", "layer": "action", "detail": r,
                                 "blocking": False, "at": time.time()} for r in check.reasons],
            "trace": _step("validate", text)}


def after_validate(state: GraphState) -> str:
    if state.get("valid"):
        return "policy_gate"
    return "optimise" if int(state.get("attempts") or 0) < MAX_ATTEMPTS else "give_up"


@contract("give_up", writes=("outcome",))
def give_up(view, _mem) -> dict[str, Any]:
    return {"outcome": "failed",
            "trace": _step("give_up", f"No valid plan after {MAX_ATTEMPTS} attempts; current assignments stand.")}


def _pulled_from(p: dict[str, Any]) -> list[str]:
    return sorted({c.get("from_incident_id") or c.get("incident_id")
                   for c in p.get("reassigned", []) + p.get("released", [])
                   if c.get("from_incident_id") or c.get("incident_id")})


@contract("policy_gate", reads=("proposal", "needs_officer", "validation"),
          writes=("needs_officer", "held", "gate_reason"), agent="gate")
async def policy_gate(view, mem: ScopedMemory) -> dict[str, Any]:
    """Automatic unless the plan pulls a unit off a severe incident, trips a
    guardrail, or repeats something an officer just rejected."""
    from app.db import session as db

    from app.ops import autonomy

    p = view.get("proposal") or {}
    if autonomy.paused():
        reason = f"Automation paused by {autonomy.state.by or 'an officer'}; nothing is written."
        return {"needs_officer": False, "held": True, "gate_reason": reason,
                "trace": _step("policy_gate", reason)}
    if not p.get("changed"):
        return {"needs_officer": False, "held": False, "gate_reason": "no change",
                "trace": _step("policy_gate", "Nothing changes; nothing to authorise.")}
    pulled = _pulled_from(p)
    # Memory: did an officer reject pulling units off these incidents recently?
    for inc in pulled:
        hit = await mem.recall(["gate"], about=inc, max_age_s=REJECTION_MEMORY_S, limit=1)
        if hit and (hit[0].get("data") or {}).get("decision") == "rejected":
            reason = (f"An officer rejected moving units off this incident "
                      f"{int((time.time() - float(hit[0].get('at') or time.time())) / 60)} min ago; holding.")
            return {"needs_officer": False, "held": True, "gate_reason": reason,
                    "trace": _step("policy_gate", reason)}
    reasons: list[str] = []
    trips: list[dict[str, Any]] = []
    threshold = int(settings.agent_graph_approval_severity or 0)
    rows = (await db.fetch(
        "select id::text, title, severity from incidents where id::text = any($1::text[])", pulled)
        if pulled else [])
    if threshold and rows:
        severe = [f"{r['title']} (S{r['severity']})" for r in rows if int(r["severity"] or 0) >= threshold]
        if severe:
            reasons.append("re-tasks units away from " + "; ".join(severe[:3]))
    stranded = set(guardrails.stranded_incidents(p))
    left = [f"{r['title']} (S{r['severity']})" for r in rows
            if r["id"] in stranded and int(r["severity"] or 0) >= STRAND_SEVERITY]
    if left:
        reasons.append("leaves " + "; ".join(left[:3]) + " with no unit")
        trips += _trip("plan.strands_incident", reasons[-1], run_id=view.get("run_id", ""), blocking=False)
    if view.get("needs_officer"):
        reasons.append(view.get("validation") or "guardrail")
    if reasons:
        reason = "; ".join(reasons)
        reason = reason[:1].upper() + reason[1:]
        return {"needs_officer": True, "held": False, "gate_reason": reason, "guardrail_trips": trips,
                "trace": _step("policy_gate", reason + " — an officer must approve.")}
    return {"needs_officer": False, "held": False, "gate_reason": "within delegation",
            "trace": _step("policy_gate", "Within the planner's delegation; issuing.")}


def after_gate(state: GraphState) -> str:
    if state.get("held"):
        return "held"
    if state.get("needs_officer"):
        return "human_approval"
    return "dispatch" if (state.get("proposal") or {}).get("changed") else "observe"


@contract("held", reads=("gate_reason",), writes=("outcome",))
def held(view, _mem) -> dict[str, Any]:
    return {"outcome": "held", "trace": _step("held", f"Held: {view.get('gate_reason') or 'by the gate'}; "
                                                      "nothing written.")}


@contract("human_approval", reads=("proposal", "gate_reason"), writes=("approval",), agent="gate")
async def human_approval(view, mem: ScopedMemory) -> dict[str, Any]:
    """Pause the run. The console resumes it with {approved, by, note}."""
    p = view.get("proposal") or {}
    raw = interrupt({
        "question": "Approve this re-tasking?",
        "reason": view.get("gate_reason"),
        "headline": p.get("headline"),
        "reassigned": p.get("reassigned", []),
        "released": p.get("released", []),
        "assigned": p.get("assigned", []),
    })
    try:
        answer = Approval.model_validate(raw if isinstance(raw, dict) else {"approved": bool(raw)})
    except ValidationError:
        answer = Approval(approved=False, by="guardrail", note="Malformed approval; treated as a rejection.")
    decision = "approved" if answer.approved else "rejected"
    await mem.remember(
        "gate", f"Officer {answer.by} {decision} moving units off {', '.join(_pulled_from(p)) or 'incidents'}"
                + (f": {answer.note}" if answer.note else ""),
        data={"decision": decision, "incidents": _pulled_from(p), "by": answer.by},
        ttl_minutes=REJECTION_MEMORY_S // 60, importance=4,
    )
    return {"approval": answer.model_dump(),
            "trace": _step("human_approval", f"{decision.capitalize()} by {answer.by}"
                           + (f": {answer.note}" if answer.note else "."))}


def after_approval(state: GraphState) -> str:
    return "dispatch" if (state.get("approval") or {}).get("approved") else "rejected"


@contract("rejected", writes=("outcome",))
def rejected(view, _mem) -> dict[str, Any]:
    return {"outcome": "rejected", "trace": _step("rejected", "Plan rejected; current assignments stand.")}


@contract("dispatch", reads=("proposal", "approval", "needs_officer"), writes=("dispatched", "outcome"))
async def dispatch(view, _mem) -> dict[str, Any]:
    """Commit. Re-solves against the world as it is now and writes it.

    Two guardrails here, because this is the only node that writes:

    * **Drift.** An officer approved a specific plan, possibly minutes ago.
      Before writing, the solve is previewed again; if it would now move a
      unit the approved plan did not, nothing is written and the run ends
      `held` (the router's next cycle brings a fresh plan to the gate).
    * **Post-commit audit.** A plan written without an officer is re-checked;
      if it would have needed one, it is flagged on the console. It has already
      been written: the audit is how a gap in the gate gets found, not undone.
    """
    from app.agents import replan as replanner
    from app.ops import autonomy

    run_id = view.get("run_id", "")
    if autonomy.paused():   # an approval answered after an emergency stop
        return {"outcome": "held", "guardrail_trips": _trip("graph.kill_switch", "paused before dispatch",
                                                            run_id=run_id),
                "trace": _step("dispatch", "Automation paused; not written.")}
    approval = view.get("approval") or {}
    if approval.get("approved") and approval.get("by") not in (None, "", "guardrail", "timeout"):
        try:
            fresh = _diff_dict(await replanner.preview(city_id=view["city_id"], trigger=view["trigger"],
                                                       actor=ACTOR))
        except Exception as exc:  # noqa: BLE001 - cannot verify, so do not write
            reason = f"could not re-verify the approved plan ({type(exc).__name__}); not written"
            return {"outcome": "held", "guardrail_trips": _trip("plan.dispatch_drift", reason, run_id=run_id),
                    "trace": _step("dispatch", reason[:1].upper() + reason[1:] + ".")}
        extra = guardrails.check_drift(view.get("proposal") or {}, fresh)
        if extra:
            reason = (f"The world changed since approval: the solve would now also move "
                      f"{', '.join(extra[:4])}. Not written; the next cycle re-asks.")
            return {"outcome": "held", "guardrail_trips": _trip("plan.dispatch_drift", reason, run_id=run_id),
                    "trace": _step("dispatch", reason)}
    diff = await replanner.replan(city_id=view["city_id"], trigger=view["trigger"], actor=ACTOR)
    d = _diff_dict(diff)
    trips: list[dict[str, Any]] = []
    if not approval.get("approved"):
        audit = guardrails.check_plan(d)
        if audit.needs_officer:
            trips = _trip("plan.post_commit_audit", "; ".join(audit.reasons), run_id=run_id, blocking=False)
    return {"dispatched": d, "outcome": "dispatched", "guardrail_trips": trips,
            "trace": _step("dispatch", (d.get("headline") or "Plan written.")
                           + (" Audit flagged: " + trips[0]["detail"] if trips else ""))}


@contract("aerial", reads=("max_severity",), writes=("aerial",))
async def aerial(view, _mem) -> dict[str, Any]:
    """The aerial agent. Runs on every cycle in parallel with the sensors and tasks the drone swarm itself
    (app/drone/swarm_link.py): drones to verify unconfirmed reports, eyes on severe / life-safety incidents,
    supply drops where supplies are short; keeps the swarm in step (severity) and stands it down on resolved
    incidents. What the drones then report comes back as field.* events, which re-enter this graph."""
    try:
        from app.drone import swarm_link
        link = swarm_link.link()
    except Exception:  # noqa: BLE001 - the swarm is optional
        link = None
    if link is None:
        return {"aerial": {"skipped": "swarm link off"}, "trace": _step("aerial", "Drone swarm not linked; no aerial tasking.")}
    try:
        out = await asyncio.wait_for(link.agent_cycle(city_id=view["city_id"], trigger=view.get("trigger", "")), timeout=20)
    except Exception as exc:  # noqa: BLE001 - never fail the cycle over the swarm
        return {"aerial": {"skipped": f"{type(exc).__name__}"}, "trace": _step("aerial", f"Drone swarm step failed: {type(exc).__name__}.")}
    if out.get("skipped"):
        text = f"Drone swarm: {out['skipped']}."
    elif out["decisions"]:
        text = "Tasked the swarm: " + "; ".join(
            f"{d['action']} {str(d.get('incident_id') or d.get('swarm_incident_id') or '')[:8]} ({d['why']})" for d in out["decisions"][:6])
    else:
        text = (f"Swarm already covers what needs aerial eyes ({out.get('drones_busy', 0)} drones on "
                f"{out.get('open_swarm_incidents', 0)} incidents); nothing new to send.")
    return {"aerial": {k: v for k, v in out.items() if k in ("decisions", "skipped", "drones_busy", "open_swarm_incidents")},
            "trace": _step("aerial", text)}


@contract("observe", reads=("dispatched", "proposal", "outcome"), writes=("outcome",))
def observe(view, _mem) -> dict[str, Any]:
    d = view.get("dispatched") or view.get("proposal") or {}
    unmet = len(d.get("uncovered", []))
    return {"outcome": view.get("outcome") or "unchanged",
            "trace": _step("observe", f"Coverage {float(d.get('coverage') or 1):.0%}; {unmet} unmet. "
                           "The router re-enters this graph on the next change.")}


NODES = (
    ("guard_in", guard_in), ("triage", triage), ("command", command), ("fanout", fanout), ("sense", sense),
    ("assess", assess), ("domain", domain), ("optimise", optimise), ("validate", validate),
    ("give_up", give_up), ("policy_gate", policy_gate), ("held", held),
    ("human_approval", human_approval), ("rejected", rejected), ("dispatch", dispatch),
    ("observe", observe), ("aerial", aerial),
)


# ------------------------------------------------------------------- build ---
def build(checkpointer: Any = None) -> Any:
    g = StateGraph(GraphState)
    for name, fn in NODES:
        g.add_node(name, fn)
    g.add_edge(START, "guard_in")
    g.add_conditional_edges("guard_in", after_guard_in, {"triage": "triage", "held": "held"})
    g.add_conditional_edges("triage", after_triage, {"command": "command", "fanout": "fanout"})
    g.add_edge("command", "fanout")
    g.add_conditional_edges("fanout", to_sensors, ["sense"])
    # The aerial agent runs beside the sensors on every cycle and ends on its own: drone tasking never waits
    # for the ground plan's approval, and the ground plan never waits for the swarm.
    g.add_edge("fanout", "aerial")
    g.add_edge("aerial", END)
    g.add_edge("sense", "assess")
    g.add_conditional_edges("assess", to_domains, ["domain", "optimise"])
    g.add_edge("domain", "optimise")
    g.add_edge("optimise", "validate")
    g.add_conditional_edges("validate", after_validate,
                            {"policy_gate": "policy_gate", "optimise": "optimise", "give_up": "give_up"})
    g.add_edge("give_up", END)
    g.add_conditional_edges("policy_gate", after_gate,
                            {"human_approval": "human_approval", "dispatch": "dispatch",
                             "observe": "observe", "held": "held"})
    g.add_edge("held", END)
    g.add_conditional_edges("human_approval", after_approval,
                            {"dispatch": "dispatch", "rejected": "rejected"})
    g.add_edge("rejected", END)
    g.add_edge("dispatch", "observe")
    g.add_edge("observe", END)
    return g.compile(checkpointer=checkpointer)


def contracts() -> list[dict[str, Any]]:
    """Who reads, writes and remembers what: for the console and the docs."""
    out = []
    for name, fn in NODES:
        c = getattr(fn, "__contract__", {})
        out.append({"node": name, "reads": list(c.get("reads", ())), "writes": list(c.get("writes", ())),
                    "agent": c.get("agent") or (name if name == "domain" else None)})
    return out


# ----------------------------------------------------------------- runtime ---
@dataclass
class Run:
    run_id: str
    city_id: str
    trigger: str
    started_at: float
    status: str = "running"          # running | waiting | done | failed
    outcome: str | None = None
    trace: list[dict[str, Any]] = field(default_factory=list)
    pending: dict[str, Any] | None = None
    guardrails: list[dict[str, Any]] = field(default_factory=list)
    finished_at: float | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


RUNS: dict[str, Run] = {}
_ORDER: list[str] = []
_graph: Any = None
_saver: Any = None
_saver_cm: Any = None
_init_lock = asyncio.Lock()


def enabled() -> bool:
    return AVAILABLE and bool(settings.agent_graph_enabled)


async def _compiled() -> Any:
    global _graph, _saver, _saver_cm
    if _graph is not None:
        return _graph
    async with _init_lock:
        if _graph is not None:
            return _graph
        saver = None
        if settings.agent_graph_checkpointer == "postgres":
            try:  # pragma: no cover - needs the optional package and a DSN
                from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

                _saver_cm = AsyncPostgresSaver.from_conn_string(settings.dsn)
                saver = await _saver_cm.__aenter__()
                await saver.setup()
            except Exception as exc:  # noqa: BLE001
                log.warning("graph_postgres_checkpointer_unavailable", error=str(exc)[:200])
                saver = None
        _saver = saver or _MemorySaver()
        _graph = build(_saver)
        return _graph


def _remember(run: Run) -> None:
    RUNS[run.run_id] = run
    _ORDER.insert(0, run.run_id)
    for old in _ORDER[30:]:
        if RUNS.get(old) and RUNS[old].status != "waiting":
            RUNS.pop(old, None)
    del _ORDER[30:]


async def _sector_events(run: Run, values: dict[str, Any]) -> None:
    """One `agent.graph_sector` event per sector the cycle's plan touched.

    The cycle itself is city-wide; the officer reading the board thinks in
    sectors and hazards. Each change in the plan names its ward, the ward names
    its sector (app/nav/sectors.py, mirrored in the `ward_sectors` table that
    tags the event), and its incident's category names the hazard.
    """
    from app.nav.hazards import CATEGORY_HAZARD
    from app.nav.sectors import sector_of

    plan = values.get("dispatched") or values.get("proposal") or {}
    changes = [(k, c) for k in ("assigned", "reassigned", "released") for c in plan.get(k, [])]
    if not changes:
        return
    try:
        from app.db import session as db
        from app.world import clock as clocks
        from app.world import events as ev

        ids = sorted({c.get("incident_id") for _, c in changes if c.get("incident_id")})
        cats = {r["id"]: r["category"] for r in await db.fetch(
            "select id::text, category from incidents where id::text = any($1::text[])", ids)} if ids else {}
        by: dict[str, dict[str, Any]] = {}
        for kind, c in changes:
            ward = c.get("ward_id") or ""
            sid = sector_of(ward) or "city"
            hz = CATEGORY_HAZARD.get(cats.get(c.get("incident_id") or "", ""), "unknown")
            b = by.setdefault(f"{sid}|{hz}", {"ward": ward, "sector": sid, "hazard": hz,
                                              "assigned": 0, "reassigned": 0, "released": 0, "units": []})
            b[kind] += 1
            b["units"].append(c.get("resource_label") or c.get("resource_id"))
        rows = [{"kind": "agent.graph_sector", "actor": ACTOR, "subject_type": "agent",
                 "subject_id": run.run_id, "ward_id": b["ward"] or None, "caused_by": None,
                 "payload": {"runId": run.run_id, "outcome": run.outcome or run.status,
                             "trigger": run.trigger, "hazardTag": b["hazard"], "sectorId": b["sector"],
                             "assigned": b["assigned"], "reassigned": b["reassigned"],
                             "released": b["released"], "units": b["units"][:10],
                             "guardrails": [t["rule"] for t in run.guardrails][:10]}}
                for b in by.values()]
        await ev.append_many(clock=clocks.WALL, rows=rows, city_id=run.city_id)
    except Exception as exc:  # noqa: BLE001 - the board is best-effort, the plan stands
        log.info("graph_sector_events_failed", error=type(exc).__name__)


async def _event(run: Run, kind: str) -> None:
    try:
        from app.world import clock as clocks
        from app.world import events as ev

        await ev.append(clock=clocks.WALL, kind=kind, actor=ACTOR, subject_type="agent",
                        subject_id=run.run_id, city_id=run.city_id,
                        payload={"trigger": run.trigger, "outcome": run.outcome,
                                 "steps": [t["text"] for t in run.trace][-14:],
                                 "pending": bool(run.pending)})
    except Exception as exc:  # noqa: BLE001 - the trace is best-effort
        log.info("graph_event_failed", error=type(exc).__name__)


async def _drive(run: Run, payload: Any) -> Run:
    graph = await _compiled()
    config = {"configurable": {"thread_id": run.run_id}, "recursion_limit": RECURSION_LIMIT}
    try:
        await asyncio.wait_for(graph.ainvoke(payload, config), timeout=RUN_TIMEOUT_S)
        snap = await graph.aget_state(config)
        values = dict(snap.values or {})
        run.trace = list(values.get("trace") or [])
        run.guardrails = list(values.get("guardrail_trips") or [])
        interrupts = [i for t in (snap.tasks or ()) for i in (getattr(t, "interrupts", ()) or ())]
        if snap.next and interrupts:
            run.status = "waiting"
            run.pending = getattr(interrupts[0], "value", None) or {}
        else:
            run.status = "done"
            run.pending = None
            run.outcome = values.get("outcome") or "unchanged"
            run.finished_at = time.time()
    except (StateViolation, MemoryAccessDenied) as exc:
        run.status, run.outcome, run.error = "failed", "blocked", f"{type(exc).__name__}: {exc}"[:300]
        run.finished_at = time.time()
        log.warning("graph_run_blocked", run=run.run_id, error=run.error)
    except asyncio.TimeoutError:
        run.status, run.outcome, run.error = "failed", "timeout", f"run exceeded {RUN_TIMEOUT_S}s"
        run.finished_at = time.time()
    except Exception as exc:  # noqa: BLE001
        run.status, run.error = "failed", str(exc)[:300]
        run.finished_at = time.time()
        log.warning("graph_run_failed", run=run.run_id, error=run.error)
    await _event(run, "agent.graph_waiting" if run.status == "waiting" else "agent.graph_run")
    if run.status in ("done", "waiting"):
        try:
            snap_values = dict((await (await _compiled()).aget_state(
                {"configurable": {"thread_id": run.run_id}})).values or {})
        except Exception:  # noqa: BLE001
            snap_values = {}
        await _sector_events(run, snap_values)
    if run.status == "waiting":
        asyncio.get_running_loop().create_task(_timeout(run.run_id))
    return run


async def run_cycle(*, city_id: str = "pune", trigger: str = "manual") -> Run:
    """One pass of the graph. Returns when it finishes or pauses for an officer."""
    cleaned = guardrails.clean_input(trigger, limit=240)
    trigger = cleaned.text or "manual"
    if cleaned.injection:
        trigger = "[neutralised] " + guardrails.INJECTION.sub("…", trigger)[:200]
    run = Run(run_id=f"g-{uuid.uuid4().hex[:10]}", city_id=city_id, trigger=trigger,
              started_at=time.time())
    _remember(run)
    return await _drive(run, {"run_id": run.run_id, "city_id": city_id, "trigger": trigger,
                              "trigger_injection": cleaned.injection,
                              "situation": [], "domain_plans": [], "guardrail_trips": [], "trace": []})


async def resume(run_id: str, *, approved: bool, by: str = "officer", note: str | None = None) -> Run:
    run = RUNS.get(run_id)
    if run is None or run.status != "waiting":
        raise KeyError(run_id)
    answer = Approval(approved=approved, by=(by or "officer")[:80],
                      note=guardrails.clean_input(note, limit=300).text if note else None)
    run.status = "running"
    return await _drive(run, Command(resume=answer.model_dump()))


async def _timeout(run_id: str) -> None:
    """An unanswered approval does not hold the city: after the timeout the
    safe default (keep what units are doing) is taken."""
    await asyncio.sleep(max(30, int(settings.agent_graph_approval_timeout_s or 300)))
    run = RUNS.get(run_id)
    if run and run.status == "waiting":
        try:
            await resume(run_id, approved=False, by="timeout",
                         note="No officer answered in time; current assignments kept.")
        except Exception:  # noqa: BLE001
            pass


def status() -> dict[str, Any]:
    from app.agents import agent_memory

    runs = [RUNS[r].as_dict() for r in _ORDER if r in RUNS]
    return {
        "available": AVAILABLE,
        "enabled": enabled(),
        "importError": IMPORT_ERROR,
        "checkpointer": type(_saver).__name__ if _saver is not None else settings.agent_graph_checkpointer,
        "approvalSeverity": settings.agent_graph_approval_severity,
        "guardrails": {
            "maxRetaskPerCycle": guardrails.MAX_RETASK_PER_CYCLE,
            "coverageDropTolerance": guardrails.COVERAGE_DROP_TOLERANCE,
            "runTimeoutS": RUN_TIMEOUT_S, "recursionLimit": RECURSION_LIMIT,
            "maxAttempts": MAX_ATTEMPTS, "rejectionMemoryMin": REJECTION_MEMORY_S // 60,
            "maxRunsPerMin": MAX_RUNS_PER_MIN, "strandSeverity": STRAND_SEVERITY,
            "maxAutoEtaMin": guardrails.MAX_AUTO_ETA_MIN,
            "ledger": guardrails.summary(),
        },
        "contracts": contracts(),
        "memoryAccess": agent_memory.access_map(),
        "waiting": [r for r in runs if r["status"] == "waiting"],
        "runs": runs,
    }


def mermaid() -> str | None:
    if not AVAILABLE:
        return None
    try:
        g = _graph or build(None)
        return g.get_graph().draw_mermaid()
    except Exception:  # noqa: BLE001
        return None
