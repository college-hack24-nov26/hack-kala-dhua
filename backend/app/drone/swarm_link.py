"""Two-way link: DisruptionOps command centre <-> the agentic drone swarm (drone_swarm repo, disruptionops.swarm.v2).

Builds on ``swarm_agency.SwarmAgency`` (DisruptionOps incidents -> swarm ``agency_request``; swarm status ->
agency replies and intake) and adds what the command centre needs to *run* the swarm:

1. **Filtered drone log.** Of everything the swarm sends (~10 snapshots/s, bids, heartbeats, its own internal
   chatter) only what an officer acts on is kept: situation reports whose content changed, alerts, accept /
   decline, key statuses (launched, on scene, briefing, supply delivered, handed over, completed), unit arrivals
   and declines. Kept in memory for the panel and written to the event log as ``drone.*`` events.
2. **Tasking from the panel.** assign / unassign drones and ground units, recall a drone, recheck (ask for a
   fresh situation report), update people / severity, close / cancel, or send a new task to a map point.
3. **Reasoner: recheck and replan.** Every material situation report or alert is re-read. The shortfall is
   worked out (people to extract vs. capacity on the way, injured vs. ambulances, fire, blocked access, drones
   on scene, worsening trend) and written as a plan: observations -> reasoning -> actions. The plan is applied
   (auto) or waits for an officer (propose; always when the emergency stop is on). After acting it **rechecks**:
   it expects the units it asked for to be assigned and asks the swarm for a fresh report; if a unit never
   moved, it unassigns it, excludes it and re-plans with the next best unit. Results are written on the plan.

DisruptionOps' own planner is told too: a material change on an incident that came from DisruptionOps is
written as ``field.drone_situation`` (the event router re-plans on ``field.*``).

Environment (read from the process env, then from backend/.env):
    SWARM_AGENCY=1  SWARM_WS_URL=wss://<host>/ops/ws  SWARM_INTEGRATION_TOKEN=<token>
    SWARM_LINK_AUTO=1          apply plans automatically (0: officer approves each)
    SWARM_TAKE_COMMAND=1       on connect tell the swarm DisruptionOps dispatches (control orchestrator=external)
    SWARM_RECHECK_S=25         seconds to wait for a planned result before rechecking / re-planning
    SWARM_GROUND=disruptionops DisruptionOps owns ground units (default): its real units, positions and
                               assignments are pushed to the swarm (external_resource_update) and a
                               drone's need for a unit is filed into DisruptionOps' planner, never
                               assigned in the swarm. `swarm` restores the swarm's own simulated units.
    SWARM_GROUND_PUSH_S=3      how often the ground picture is re-sent when it changed
URL and token can also be changed at runtime from the panel (POST /drone/link/config).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from app.drone.swarm_agency import (ANCHOR, KIND_WORDS, PROTOCOL, SwarmAgency, parse_anchor, to_geo, to_local)

# ------------------------------------------------------------------ config --
_DOTENV: dict[str, str] | None = None


def env(key: str, default: str = "") -> str:
    """os.environ first, then backend/.env (pydantic Settings reads .env but does not export it)."""
    global _DOTENV
    if key in os.environ:
        return os.environ[key]
    if _DOTENV is None:
        _DOTENV = {}
        for p in (Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"):
            if p.is_file():
                for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        _DOTENV.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return _DOTENV.get(key, default)


def _on(key: str, default: str) -> bool:
    return env(key, default).lower() in ("1", "true", "yes", "on")


RECHECK_S = float(env("SWARM_RECHECK_S", "25"))
GROUND_PUSH_S = float(env("SWARM_GROUND_PUSH_S", "3"))
GROUND_KEEPALIVE_S = 30.0
NEED_REFILE_S = 300.0
#: DisruptionOps resource kind -> the swarm's kind (the viewer's model and its reasoning words)
DOPS_TO_SWARM_KIND = {"rescue_team": "rescue", "jcb": "engineering"}
#: swarm kind a drone asks for -> the DisruptionOps capability the planner covers
SWARM_KIND_CAPABILITY = {"boat": "water_rescue", "rescue": "search_rescue", "fire_engine": "fire_suppression",
                         "ambulance": "medical_transport", "engineering": "debris_clearance", "bus": "mass_transport",
                         "pump": "dewatering"}
#: swarm incident category -> DisruptionOps category for a drone-seen incident it files
SWARM_TO_DOPS_CATEGORY = {"flood": "person_stranded", "trapped": "person_stranded", "rooftop": "person_stranded",
                          "fire": "fire", "medical": "person_stranded", "collapse": "structural_damage",
                          "blocked_route": "fallen_tree", "crowding": "shelter_full", "supply": "supply_shortage"}
REPLAN_COOLDOWN_S = 8.0
LOG_MAX, PLAN_MAX = 400, 80

#: statuses worth an officer's attention (swarm -> command_center ``status`` messages)
KEY_STATUSES = {"launched", "on_scene", "briefing", "supply_delivered", "ground_arrived", "handed_over", "completed",
                "declined", "recalled", "retasked", "resource_request", "drone_fault", "queued"}
#: unit states worth logging (agency node -> command_center ``unit_status``)
KEY_UNIT_STATES = {"arrived", "on_scene", "unavailable"}
#: extraction-capable kinds, in order of preference, and substitutes when a kind is not free
SUBSTITUTES = {"boat": ["boat", "rescue", "fire_engine"], "rescue": ["rescue", "fire_engine"],
               "ambulance": ["ambulance"], "fire_engine": ["fire_engine"], "engineering": ["engineering"],
               "bus": ["bus"], "pump": ["pump"]}
EXTRACT_KINDS = {"boat", "rescue", "fire_engine"}
WATER_CATEGORIES = {"flood", "person_stranded", "rooftop", "stranded", "drowning"}
TRAPPED_CATEGORIES = {"trapped", "collapse", "building_collapse"}
LEVEL = {"none": 0, "low": 1, "moderate": 2, "high": 3, "severe": 4, "extreme": 5}


def _dist(a, b) -> float:
    return math.hypot(float(a[0]) - float(b[0]), float(a[1]) - float(b[1]))


def _num(v, default=0.0) -> float:
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------ pure reasoning --
def situation_signature(body: dict) -> dict:
    people = body.get("people") or {}
    injured = body.get("injured") or {}
    hazards = {h.get("kind"): LEVEL.get(str(h.get("level")), 0) for h in body.get("hazards") or [] if isinstance(h, dict)}
    access = body.get("access") or {}
    return {"people": round(_num(people.get("estimate"))), "injured": round(_num(injured.get("estimate")), 1),
            "hazards": hazards, "trend": body.get("trend") or "stable",
            "blocked": int(_num(access.get("roads_blocked"))) + int(_num(access.get("roads_flooded"))),
            "assigned": sorted(u.get("id") for u in body.get("nearby_units") or [] if u.get("assigned_here"))}


def material_change(old: dict | None, new: dict) -> list[str]:
    """Why a new report should make us re-think (empty: nothing an officer would act on)."""
    if old is None:
        return ["first report from the drones"]
    why = []
    dp = new["people"] - old["people"]
    if abs(dp) >= 2 or (old["people"] and abs(dp) / max(1, old["people"]) >= 0.25):
        why.append(f"people {old['people']} -> {new['people']}")
    if new["injured"] > old["injured"] + 0.4:
        why.append(f"injured {old['injured']} -> {new['injured']}")
    for kind, lvl in new["hazards"].items():
        if lvl >= 2 and lvl > old["hazards"].get(kind, 0):
            why.append(f"{kind} hazard up to {next(n for n, v in LEVEL.items() if v == lvl)}")
    if new["trend"] != old["trend"] and new["trend"] in ("worsening", "deteriorating", "rising"):
        why.append(f"trend {old['trend']} -> {new['trend']}")
    if set(old["assigned"]) - set(new["assigned"]):
        why.append("a unit left the scene: " + ", ".join(sorted(set(old["assigned"]) - set(new["assigned"]))))
    return why


def reason(inc: dict, body: dict, units: list[dict], drones: list[dict], *, exclude: set[str] = frozenset(),
           unavailable_kinds: set[str] = frozenset(), ground: str = "swarm") -> dict:
    """Situation -> plan. Pure, so it is unit-testable and explainable.

    inc: swarm incident (id, category, people, severity, position, evacuated_people, status)
    body: latest situation_report body (may be {} when acting on an alert or a manual replan)
    units: swarm ground resources (id, kind, state, incident_id, position, capacity, eta)
    drones: swarm drones (id, state, task_id, active, link, battery)
    ground: "swarm" assigns the swarm's free units; "disruptionops" never assigns a ground unit itself:
            each unit it wants becomes a ``dops_need`` action, which DisruptionOps' planner covers.
    """
    iid, pos = inc["id"], inc.get("position") or [0, 0]
    people_b = body.get("people") or {}
    people = round(_num(people_b.get("estimate"), _num(inc.get("people"), 1)))
    left = max(0, people - int(_num(inc.get("evacuated_people"))))
    injured_b = body.get("injured") or {}
    injured = _num(injured_b.get("estimate"))
    inj_sev = str(injured_b.get("severity") or "none")
    hazards = {h.get("kind"): (LEVEL.get(str(h.get("level")), 0), h.get("trend")) for h in body.get("hazards") or [] if isinstance(h, dict)}
    trend = body.get("trend") or "stable"
    conf = _num(body.get("confidence"), 0.7)
    access = body.get("access") or {}
    category = str(inc.get("category") or "")

    here = [u for u in units if u.get("incident_id") == iid]
    on_scene_drones = [d for d in drones if d.get("task_id") == iid and d.get("active")]
    free = [] if ground == "disruptionops" else sorted(
        (u for u in units if u.get("state") == "available" and not u.get("incident_id") and u["id"] not in exclude),
        key=lambda u: _dist(u.get("position") or [0, 0], pos))

    obs, why, actions = [], [], []
    obs.append(f"{people} people (≈{left} still to move), injured ≈{injured:g} ({inj_sev})")
    if hazards:
        obs.append("hazards: " + ", ".join(f"{k} {next(n for n, v in LEVEL.items() if v == lv)}" for k, (lv, _) in hazards.items()))
    obs.append(f"trend {trend}; report confidence {conf:.2f}")
    obs.append(f"on the way/here: {', '.join(u['id'] + ' ' + u['kind'] for u in here) or 'no ground unit'}; "
               f"drones on scene: {', '.join(d['id'] for d in on_scene_drones) or 'none'}")
    if access:
        obs.append(f"access: {access.get('best_approach') or 'unknown'}")

    picked: list[dict] = []

    def take(kind: str, reason_text: str) -> dict | None:
        if ground == "disruptionops":
            if any(u["kind"] == kind for u in picked):
                return None
            u = {"id": f"planner:{kind}", "kind": kind, "capacity": 4, "virtual": True}
            picked.append(u)
            actions.append({"action": "dops_need", "payload": {"incident_id": iid, "kind": kind, "reason": reason_text},
                            "why": f"{reason_text}: ask DisruptionOps' planner for a {kind.replace('_', ' ')}", "expect": {}})
            return u
        for k in SUBSTITUTES.get(kind, [kind]):
            if k in unavailable_kinds and k != kind:
                continue
            u = next((u for u in free if u["kind"] == k and u not in picked), None)
            if u:
                picked.append(u)
                eta = _dist(u.get("position") or [0, 0], pos) / 9
                sub = "" if k == kind else f" (no {kind} free; {k} substitutes)"
                actions.append({"action": "assign", "payload": {"incident_id": iid, "resources": [{"id": u["id"], "kind": u["kind"]}]},
                                "why": f"{reason_text}: {u['id']} {u['kind']}, nearest free, ~{eta:.0f} s{sub}", "expect": {"unit": u["id"]}})
                return u
        why.append(f"wanted a {kind} for '{reason_text}' but none is free; keeping the drone on scene and rechecking")
        return None

    # 1. people to extract (water -> boats / rescue teams; trapped -> rescue teams) vs. capacity on the way
    water = hazards.get("water", (0, None))[0] >= 2 or category in WATER_CATEGORIES
    crowding = hazards.get("crowding", (0,))[0] >= 2
    trapped = (category in TRAPPED_CATEGORIES and not crowding) or hazards.get("structural", (0,))[0] >= 3
    extract_kinds = {"boat", "rescue"} if water else {"rescue", "fire_engine"}
    cap_here = sum(int(_num(u.get("capacity"), 4)) for u in here if u["kind"] in extract_kinds)
    if left > 0 and (water or trapped):
        short = left - cap_here
        why.append(f"{left} people need {'water extraction' if water else 'extrication'}; "
                   f"capacity on the way {cap_here} -> shortfall {max(0, short)}")
        n = 0
        while short > 0 and n < 2:
            u = take("boat" if water else "rescue", f"{'extract' if water else 'free'} {short} more people")
            if not u:
                break
            short -= int(_num(u.get("capacity"), 4))
            n += 1
    # 1b. crowding (shelter full, people arriving) -> move people: a bus
    if crowding and left > 0 and not any(u["kind"] == "bus" for u in here):
        why.append(f"crowding with ≈{left} people and no bus -> move them to the next shelter")
        take("bus", "move people to another shelter")
    # 2. injured vs. ambulances
    amb_here = [u for u in here if u["kind"] == "ambulance"]
    need_amb = 0 if injured < 0.5 else (2 if (injured >= 4 or inj_sev in ("critical", "severe")) else 1)
    if need_amb > len(amb_here):
        why.append(f"≈{injured:g} injured ({inj_sev}) and {len(amb_here)} ambulance(s) assigned -> need {need_amb}")
        for _ in range(need_amb - len(amb_here)):
            if not take("ambulance", "carry the injured"):
                break
    # 3. fire
    if (hazards.get("fire", (0,))[0] >= 2 or category == "fire") and not any(u["kind"] == "fire_engine" for u in here):
        why.append("fire at moderate or worse and no fire engine assigned")
        take("fire_engine", "fire suppression")
    # 4. structural / blocked access
    blocked = int(_num(access.get("roads_blocked"))); open_ = int(_num(access.get("roads_open")))
    if category == "blocked_route":
        eng_why = "route blocked"
    elif blocked and open_ == 0:
        eng_why = f"no open road in ({blocked} blocked)"
    elif hazards.get("structural", (0,))[0] >= 3:
        eng_why = "structure unsafe; heavy lifting needed"
    else:
        eng_why = ""
    if eng_why and not any(u["kind"] == "engineering" for u in here):
        why.append(eng_why + " -> engineering (JCB)")
        take("engineering", "clear the approach")
    # 5. drones: eyes on scene, overwatch when it is getting worse, supplies while crews are far
    if inc.get("status") != "resolved":
        if not on_scene_drones:
            why.append("no drone watching this incident -> send one to assess")
            actions.append({"action": "assign", "payload": {"incident_id": iid, "drones": [{"count": 1, "role": "assess"}]},
                            "why": "keep eyes on the scene until the ground crew takes over", "expect": {"drones": 1}})
        elif trend in ("worsening", "deteriorating", "rising") and len(on_scene_drones) < 2:
            why.append("situation worsening with one drone -> add overwatch")
            actions.append({"action": "assign", "payload": {"incident_id": iid, "drones": [{"count": 1, "role": "overwatch"}]},
                            "why": "second aircraft for overwatch while it worsens", "expect": {"drones": len(on_scene_drones) + 1}})
        etas = [_num(u.get("eta"), 0) for u in here] + [_dist(u.get("position") or [0, 0], pos) / 9 for u in picked if not u.get("virtual")]
        if left > 0 and water and not body.get("supplies_dropped") and (not etas or min(etas) > 240):
            why.append("people waiting in water with no crew within 4 min -> supply drop")
            actions.append({"action": "assign", "payload": {"incident_id": iid, "drones": [{"count": 1, "role": "supply", "payload": ["water", "food", "first_aid"]}]},
                            "why": "drop water/food/first aid until the crew arrives", "expect": {}})
    # 6. severity follows the facts
    sev = 3 + (people >= 5 or injured >= 1) + (any(lv >= 3 for lv, _ in hazards.values()) or trend == "worsening" or bool(body.get("vulnerable")))
    if sev > int(_num(inc.get("severity"), 3)):
        actions.append({"action": "update_incident", "payload": {"incident_id": iid, "severity": min(5, sev), "people": people},
                        "why": f"severity {inc.get('severity')} -> {min(5, sev)} from what the drones see", "expect": {}})
    # 7. low confidence: ask again
    if conf < 0.5 or not body:
        why.append("report is thin or uncertain -> ask the drones for a fresh look")
        actions.append({"action": "request_situation", "payload": {"incident_id": iid}, "why": "recheck", "expect": {}})
    if not actions:
        why.append("what is on the way covers what the drones see; no change")
    summary = (f"{iid}: " + "; ".join(a["why"].split(":")[0] for a in actions if a["action"] != "request_situation")) if actions else f"{iid}: no change needed"
    return {"incident_id": iid, "observations": obs, "reasoning": why, "actions": actions, "summary": summary[:300]}


def _graph_drives() -> bool:
    """True when the LangGraph cycle (with its aerial node) is what tasks the swarm."""
    try:
        from app.agents import event_router, graph
        return graph.enabled() and event_router.running()
    except Exception:  # noqa: BLE001
        return False


# ----------------------------------------------------------------- the link --
class SwarmLink(SwarmAgency):
    def __init__(self, *a, auto: bool = True, take_command: bool = True, **k):
        super().__init__(*a, **k)
        self.auto, self.take_command = auto, take_command
        self.can_task: bool | None = None         # None: not known yet; False: the token is view-only on this swarm
        self.anchor_fixed = bool(env("SWARM_ANCHOR"))
        self.state: dict = {}
        self.bounds = [-600, -600, 600, 600]
        self.run_id: str | None = None
        self.last_msg_id = 0
        self.log: deque = deque(maxlen=LOG_MAX)
        self.plans: deque = deque(maxlen=PLAN_MAX)
        self.sig: dict[str, dict] = {}            # swarm incident id -> last situation signature
        self.reports: dict[str, dict] = {}        # swarm incident id -> last situation_report body
        self.report_text: dict[str, str] = {}     # swarm incident id -> last situation_report text
        self.expect: dict[str, dict] = {}         # swarm incident id -> {plan_id, units, drones, due, rechecked}
        self.exclude: dict[str, set] = {}         # swarm incident id -> unit ids that did not come
        self.unavailable: dict[str, float] = {}   # unit kind -> until (monotonic) after an agency decline
        self.last_plan_at: dict[str, float] = {}
        self.claimed: dict[str, tuple[str, float]] = {}   # unit id -> (incident it was planned for, until)
        self.work: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._send_lock = asyncio.Lock()
        self._log_seq = 0
        self.ground = env("SWARM_GROUND", "disruptionops").strip().lower()
        self.filed: dict[str, dict] = {}          # swarm incident id -> {incident_id, ward_id, kinds, at} filed into DisruptionOps
        self.ground_push = {"at": 0.0, "sig": None, "sent": 0, "units": 0, "last_ok": None, "error": None}
        self.link_stats = {"messages_seen": 0, "logged": 0, "plans": 0, "applied": 0, "rechecks": 0, "replans": 0, "commands": 0, "command_errors": 0}

    # --- commands (rate-limited: the swarm closes sockets above 10/s) ---------------
    async def command(self, action: str, payload: dict, timeout: float = 8.0) -> dict:
        async with self._send_lock:
            try:
                self.link_stats["commands"] += 1
                ack = await super().command(action, payload, timeout)
                if not ack.get("ok"):
                    self.link_stats["command_errors"] += 1
                    if "authorization" in str(ack.get("error", "")).lower():
                        if self.can_task is not False:
                            self._log("link", "The swarm treats our token as VIEW-ONLY: logging works, tasking does not. "
                                              "Set the swarm's OPS_INTEGRATION_TOKEN in the panel.", frm="disruptionops", level="alert")
                        self.can_task = False
                else:
                    self.can_task = True
                return ack
            finally:
                await asyncio.sleep(0.12)

    # --- log ---------------------------------------------------------------------
    def _log(self, kind: str, text: str, *, incident_id: str | None = None, frm: str = "swarm", level: str = "info",
             data: dict | None = None, persist: bool = True) -> dict:
        self._log_seq += 1
        item = {"seq": self._log_seq, "at": time.time(), "t": self.state.get("t"), "kind": kind, "from": frm,
                "incident_id": incident_id, "text": str(text)[:400], "level": level, "data": data or {}}
        self.log.append(item)
        self.link_stats["logged"] += 1
        if persist:
            with contextlib.suppress(Exception):
                self.work.put_nowait(("persist", item))
        return item

    def _dops_incident(self, swarm_incident_id: str | None) -> tuple[str | None, str | None]:
        """Swarm incident -> (DisruptionOps incident uuid, ward) when it came from a DisruptionOps request."""
        inc = self._inc(swarm_incident_id)
        rid = (inc or {}).get("request_id") or ""
        for r, info in self.sent.items():
            if r == rid or info.get("swarm_incident_id") == swarm_incident_id:
                return info["incident_id"], info.get("ward_id")
        if swarm_incident_id in self.filed:
            f = self.filed[swarm_incident_id]
            return f["incident_id"], f.get("ward_id")
        ext = (inc or {}).get("external_id")
        return (str(ext), None) if ext and len(str(ext)) == 36 else (None, None)

    def ground_mode(self) -> str:
        """Who owns ground units: DisruptionOps only when it has taken command of the swarm."""
        return "disruptionops" if self.ground == "disruptionops" and self.take_command else "swarm"

    # --- state ---------------------------------------------------------------------
    def _inc(self, iid: str | None) -> dict | None:
        return next((i for i in self.state.get("incidents") or [] if i.get("id") == iid), None) if iid else None

    async def on_snapshot(self, packet: dict) -> None:
        state = packet.get("state") or {}
        world = state.get("world")
        if world and world.get("bounds"):
            self.bounds = world["bounds"]
        if state.get("run_id") != self.run_id:
            if self.run_id is not None:
                self._log("run", f"Swarm exercise changed (run {str(state.get('run_id'))[:8]}, scenario "
                                 f"{(state.get('scenario') or {}).get('id')}).", level="warn")
            self.run_id, self.last_msg_id = state.get("run_id"), 0
            self.sig.clear(); self.reports.clear(); self.expect.clear(); self.exclude.clear(); self.filed.clear()
            self.ground_push.update(sig=None, at=0.0)
            self._primed_msgs = False
        state.pop("world", None)
        anc = state.get("anchor")
        if not self.anchor_fixed and isinstance(anc, list) and len(anc) == 2:
            self.anchor = (float(anc[0]), float(anc[1]))
        self.state = state
        await super().on_snapshot(packet)
        msgs = state.get("messages") or []
        if not getattr(self, "_primed_msgs", False):
            # first frame after (re)connect: remember situation, log only what arrives from now on
            for m in msgs:
                if m.get("kind") == "situation_report" and m.get("incident_id"):
                    self.sig[m["incident_id"]] = situation_signature(m.get("body") or {})
                    self.reports[m["incident_id"]] = m.get("body") or {}
                    self.report_text[m["incident_id"]] = m.get("text") or ""
            self.last_msg_id = max([m["id"] for m in msgs if isinstance(m.get("id"), int)] or [0])
            self._primed_msgs = True
            return
        for m in msgs:
            mid = m.get("id")
            if not isinstance(mid, int) or mid <= self.last_msg_id:
                continue
            self.last_msg_id = mid
            self.link_stats["messages_seen"] += 1
            self._filter(m)

    def _filter(self, m: dict) -> None:
        """Keep what an officer would act on; drop bids, heartbeats and the swarm's internal chatter."""
        kind, to, frm, iid, body = m.get("kind"), m.get("to"), m.get("from"), m.get("incident_id"), m.get("body") or {}
        if to not in ("command_center", "disruptionops", "orchestrator"):
            return
        if kind == "situation_report" and iid:
            new = situation_signature(body)
            old = self.sig.get(iid)
            changed = material_change(old, new)
            self.reports[iid] = body
            self.report_text[iid] = m.get("text") or ""
            if old == new:
                self._check_expectation(iid)
                return
            self.sig[iid] = new
            self._log("situation", m.get("text") or "situation report", incident_id=iid, frm=frm,
                      level="warn" if changed else "info",
                      data={"people": new["people"], "injured": new["injured"], "hazards": new["hazards"], "trend": new["trend"],
                            "confidence": body.get("confidence"), "changed": changed})
            if changed:
                self._queue("reason", iid, "; ".join(changed))
            self._check_expectation(iid)
        elif kind == "alert":
            sev = body.get("severity") or "high"
            self._log("alert", m.get("text") or body.get("alert"), incident_id=iid, frm=frm, level="alert", data=body)
            self._queue("alert", iid, body)
        elif kind == "decline":
            self._log("decline", m.get("text"), incident_id=iid, frm=frm, level="warn", data=body)
            if body.get("kind"):
                self.unavailable[str(body["kind"])] = time.monotonic() + 60
            if iid:
                self._queue("reason", iid, f"{frm} declined: {body.get('reason') or ''}")
        elif kind == "accept" and frm in ("swarm",):
            self._log("accept", m.get("text"), incident_id=iid, frm=frm, data=body)
        elif kind == "status" and str(body.get("status")) in KEY_STATUSES:
            st = str(body.get("status"))
            self._log(f"status.{st}", m.get("text"), incident_id=iid, frm=frm,
                      level="good" if st in ("on_scene", "handed_over", "completed", "supply_delivered") else "info", data=body)
        elif kind == "unit_status" and str(body.get("state")) in KEY_UNIT_STATES:
            self._log("unit", m.get("text"), incident_id=iid or body.get("incident_id"), frm=frm,
                      level="warn" if body.get("state") == "unavailable" else "good", data=body)
        elif kind == "handoff":
            self._log("handoff", m.get("text"), incident_id=iid, frm=frm, level="good", data=body)

    def _queue(self, what: str, iid: str | None, why: Any) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            self.work.put_nowait((what, iid, why))

    # --- recheck ---------------------------------------------------------------------
    def _check_expectation(self, iid: str) -> None:
        exp = self.expect.get(iid)
        if not exp:
            return
        res = self.state.get("resources") or []
        drones = self.state.get("drones") or []
        missing = [u for u in exp["units"] if not any(r.get("id") == u and r.get("incident_id") == iid for r in res)]
        have_drones = sum(1 for d in drones if d.get("task_id") == iid and d.get("active"))
        if not missing and have_drones >= exp["drones"]:
            plan = self._plan(exp["plan_id"])
            if plan is not None:
                plan["results"].append({"at": time.time(), "ok": True, "text": "verified: " + (
                    ", ".join(exp["units"]) + " assigned" if exp["units"] else f"{have_drones} drone(s) on scene")})
                plan["verified"] = True
            self._log("verified", f"{iid}: the drones confirm the plan took effect "
                                  f"({', '.join(exp['units']) or str(have_drones) + ' drone(s)'}).", incident_id=iid, frm="disruptionops", level="good")
            self.expect.pop(iid, None)

    async def _tick(self) -> None:
        now = time.monotonic()
        for iid, exp in list(self.expect.items()):
            self._check_expectation(iid)
            if iid not in self.expect or now < exp["due"]:
                continue
            inc = self._inc(iid)
            if inc is None or inc.get("status") == "resolved":
                self.expect.pop(iid, None)
                continue
            res = self.state.get("resources") or []
            missing = [u for u in exp["units"] if not any(r.get("id") == u and r.get("incident_id") == iid for r in res)]
            if not exp["rechecked"]:
                exp["rechecked"], exp["due"] = True, now + RECHECK_S
                self.link_stats["rechecks"] += 1
                self._log("recheck", f"{iid}: result not seen yet ({', '.join(missing) or 'drone'} pending); asking the drones for a fresh report.",
                          incident_id=iid, frm="disruptionops", level="warn")
                with contextlib.suppress(Exception):
                    await self.command("request_situation", {"incident_id": iid})
                continue
            # second miss: drop what did not come, exclude it, re-plan with the next best
            self.expect.pop(iid, None)
            if not missing:
                self._log("queued", f"{iid}: drone still queued in the swarm (launch cap or no free aircraft); keeping the request, not re-sending.",
                          incident_id=iid, frm="disruptionops", level="info")
                continue
            if missing:
                self.exclude.setdefault(iid, set()).update(missing)
                with contextlib.suppress(Exception):
                    await self.command("unassign", {"incident_id": iid, "resource_ids": missing})
            self.link_stats["replans"] += 1
            self._log("replan", f"{iid}: planned result did not happen ({', '.join(missing) or 'drone did not arrive'}); "
                                f"re-planning without {', '.join(missing) or 'it'}.", incident_id=iid, frm="disruptionops", level="warn")
            await self.make_plan(iid, "result check failed: " + (", ".join(missing) + " never moved" if missing else "no drone on scene"), force=True)

    # --- plans -----------------------------------------------------------------------
    def _plan(self, pid: str) -> dict | None:
        return next((p for p in self.plans if p["id"] == pid), None)

    async def make_plan(self, iid: str, trigger: str, *, force: bool = False, extra: list[dict] | None = None) -> dict | None:
        inc = self._inc(iid)
        if inc is None or inc.get("status") == "resolved":
            return None
        now = time.monotonic()
        if not force and now - self.last_plan_at.get(iid, 0) < REPLAN_COOLDOWN_S:
            return None
        self.last_plan_at[iid] = now
        unavailable = {k for k, until in self.unavailable.items() if until > now}
        claimed = {u for u, (other, until) in self.claimed.items() if other != iid and until > now}
        out = reason(inc, self.reports.get(iid) or {}, self.state.get("resources") or [], self.state.get("drones") or [],
                     exclude=self.exclude.get(iid, set()) | claimed, unavailable_kinds=unavailable, ground=self.ground_mode())
        for a in out["actions"]:                      # claim now so the next incident's plan does not take the same unit
            u = a.get("expect", {}).get("unit")
            if u:
                self.claimed[u] = (iid, now + RECHECK_S * 3)
        if extra:
            out["actions"] = extra + out["actions"]
        plan = {"id": f"P{uuid.uuid4().hex[:6]}", "at": time.time(), "t": self.state.get("t"), "incident_id": iid,
                "title": inc.get("title"), "trigger": trigger, **out, "status": "proposed", "results": [], "verified": False,
                "mode": "auto" if self.auto and self.can_task is not False and not self.ports.autonomy_paused() else "proposed"}
        if not plan["actions"]:
            plan["status"] = "no_change"
        self.plans.append(plan)
        self.link_stats["plans"] += 1
        if not plan["actions"]:
            return plan
        self._log("plan", f"Plan {plan['id']} for {iid} ({trigger}): {plan['summary']}", incident_id=iid, frm="disruptionops",
                  level="info", data={"plan_id": plan["id"], "actions": len(plan["actions"])})
        dops_id, ward = self._dops_incident(iid)
        with contextlib.suppress(Exception):
            await self.ports.record("field.drone_situation" if dops_id else "drone.plan", dops_id, ward,
                                    {"swarm_incident_id": iid, "plan_id": plan["id"], "trigger": trigger, "observations": plan["observations"],
                                     "reasoning": plan["reasoning"], "actions": [{"action": a["action"], "why": a["why"]} for a in plan["actions"]],
                                     "simulation_only": True})
        if plan["actions"] and plan["mode"] == "auto":
            await self.apply_plan(plan["id"], by="auto")
        return plan

    async def apply_plan(self, pid: str, *, by: str = "officer") -> dict:
        plan = self._plan(pid)
        if plan is None:
            raise KeyError("Unknown plan")
        if plan["status"] in ("applied", "partial", "rejected"):
            return plan
        units, drones_wanted = [], 0
        ok_n = 0
        needs = [a for a in plan["actions"] if a["action"] == "dops_need"]
        if needs:
            try:
                filed = await self.file_needs(plan["incident_id"], [a["payload"]["kind"] for a in needs], plan)
                ok, text = bool(filed.get("ok")), filed.get("text")
            except Exception as exc:  # noqa: BLE001
                ok, text = False, f"{type(exc).__name__}: {str(exc)[:120]}"
            for a in needs:
                plan["results"].append({"at": time.time(), "ok": ok, "action": "dops_need",
                                        "text": f"{a['why']} -> {'filed' if ok else 'not filed'}: {text}"})
            ok_n += len(needs) if ok else 0
        for a in plan["actions"]:
            if a["action"] == "dops_need":
                continue
            try:
                ack = await self.command(a["action"], a["payload"])
                ok = bool(ack.get("ok"))
                text = (ack.get("result") or {}).get("status") if ok else ack.get("error")
            except (ConnectionError, asyncio.TimeoutError) as exc:
                ok, text = False, type(exc).__name__
            plan["results"].append({"at": time.time(), "ok": ok, "action": a["action"], "text": f"{a['why']} -> {'ok' if ok else 'failed: ' + str(text)}"})
            if ok:
                ok_n += 1
                units += [e for e in [a.get("expect", {}).get("unit")] if e]
                drones_wanted = max(drones_wanted, a.get("expect", {}).get("drones", 0))
        plan["status"] = "applied" if ok_n == len(plan["actions"]) else "partial" if ok_n else "failed"
        plan["applied_by"] = by
        if any(not r["ok"] and ("TimeoutError" in r["text"] or "ConnectionError" in r["text"]) for r in plan["results"]):
            # The command may still have landed (an ack that came late): re-read the incident from the swarm's own
            # state in a moment instead of re-sending blindly; the reasoner skips what is already done.
            iid = plan["incident_id"]
            asyncio.get_running_loop().call_later(15, self._queue, "reason", iid, "retry after a command timed out")
        self.link_stats["applied"] += 1
        if units or drones_wanted:
            self.expect[plan["incident_id"]] = {"plan_id": pid, "units": units, "drones": drones_wanted,
                                                "due": time.monotonic() + RECHECK_S, "rechecked": False}
        self._log("applied", f"Plan {pid} {plan['status']} by {by}: {ok_n}/{len(plan['actions'])} actions accepted by the swarm.",
                  incident_id=plan["incident_id"], frm="disruptionops", level="good" if plan["status"] == "applied" else "warn")
        return plan

    def reject_plan(self, pid: str, by: str = "officer") -> dict:
        plan = self._plan(pid)
        if plan is None:
            raise KeyError("Unknown plan")
        if plan["status"] == "proposed":
            plan["status"], plan["applied_by"] = "rejected", by
            self._log("rejected", f"Plan {pid} rejected by {by}.", incident_id=plan["incident_id"], frm="disruptionops", level="warn")
        return plan

    # --- DisruptionOps owns the ground ------------------------------------------------------
    async def file_needs(self, iid: str, kinds: list[str], plan: dict | None = None) -> dict:
        """A drone on scene wants ground units: file it into DisruptionOps (intake + needs), so CP-SAT picks the
        real unit and the ground push shows it in the swarm. Never assigns a unit from here."""
        inc = self._inc(iid)
        if inc is None:
            return {"ok": False, "text": "swarm incident gone"}
        kinds = sorted(set(kinds))
        prev = self.filed.get(iid)
        if prev and set(kinds) <= set(prev.get("kinds") or []) and time.time() - prev.get("at", 0) < NEED_REFILE_S:
            return {"ok": True, "text": f"already with the planner as incident {str(prev['incident_id'])[:8]}"}
        if self.ports.autonomy_paused():
            return {"ok": False, "text": "emergency stop: need logged, not filed"}
        lng, lat = to_geo(*(inc.get("position") or [0, 0])[:2], self.anchor)
        dops_id, ward = self._dops_incident(iid)
        if not ward:
            from app.mesh import service as mesh
            ward = await mesh.ward_at(lng, lat, getattr(self.ports, "city_id", "pune"))
        if not ward:
            return {"ok": False, "text": "no ward at that point"}
        hazards = {h.get("kind") for h in (self.reports.get(iid) or {}).get("hazards") or [] if isinstance(h, dict)}
        cat = str(inc.get("category") or "")
        category = "structural_damage" if cat == "trapped" and "structural" in hazards else SWARM_TO_DOPS_CATEGORY.get(cat, "person_stranded")
        with contextlib.suppress(Exception):
            from app.taxonomy import cache as taxonomy
            if taxonomy.categories and category not in taxonomy.categories:
                category = "person_stranded"
        words = "; ".join(w for w in (KIND_WORDS.get(k) for k in kinds) if w)
        why = "; ".join((plan or {}).get("reasoning") or [])[:240]
        parts = [f"Drone swarm on scene ({iid}, {inc.get('people') or 1} people): needs "
                 f"{', '.join(k.replace('_', ' ') for k in kinds)}", words, why, "(simulated drones)"]
        note = ". ".join(p.strip().rstrip(".") for p in parts if p and p.strip())[:480]
        out = await self.ports.file_report(ward_id=ward, category=category, lng=lng, lat=lat, note=note)
        target = out.get("incident_id") or dops_id
        caps = {SWARM_KIND_CAPABILITY[k]: 1 for k in kinds if k in SWARM_KIND_CAPABILITY}
        if target and caps and hasattr(self.ports, "add_needs"):
            caps = await self.ports.add_needs(target, caps)
        if target:
            self.filed[iid] = {"incident_id": target, "ward_id": ward, "kinds": kinds, "at": time.time()}
            await self.ports.record("field.drone_need", target, ward,
                                    {"swarm_incident_id": iid, "kinds": kinds, "capabilities": caps, "category": category,
                                     "intake": out, "reasoning": (plan or {}).get("reasoning"), "simulation_only": True})
        self._log("need", f"{iid}: asked DisruptionOps' planner for {', '.join(kinds)} "
                          f"(incident {str(target)[:8] if target else '?'}, {'new' if out.get('created') else 'linked'}).",
                  incident_id=iid, frm="disruptionops", level="info", data={"dops_incident_id": target, "kinds": kinds})
        return {"ok": bool(target), "text": f"incident {str(target)[:8]}" if target else "intake held the report"}

    def _incident_map(self) -> dict[str, str]:
        """DisruptionOps incident id -> swarm incident id, from every way the two got linked."""
        out: dict[str, str] = {}
        for info in self.sent.values():
            if info.get("swarm_incident_id"):
                out[str(info["incident_id"])] = info["swarm_incident_id"]
        for sid, f in self.filed.items():
            out.setdefault(str(f["incident_id"]), sid)
        for i in self.state.get("incidents") or []:
            ext = i.get("external_id")
            if ext and len(str(ext)) == 36:
                out.setdefault(str(ext), i["id"])
        return out

    def ground_payload(self, rows: list[dict]) -> dict:
        """DisruptionOps' units -> the swarm's external_resource_update. Units outside the map are shown at its
        edge, still carrying DisruptionOps' ETA, so nobody appears to teleport."""
        b = self.bounds
        inset = 8.0

        def clamp(p):
            return [round(min(max(p[0], b[0] + inset), b[2] - inset), 1), round(min(max(p[1], b[1] + inset), b[3] - inset), 1)]

        mapping = self._incident_map()
        resources, incidents = [], {}
        for r in rows:
            if r.get("lng") is None:
                continue
            x, y = to_local(r["lng"], r["lat"], self.anchor)
            off = not (b[0] <= x <= b[2] and b[1] <= y <= b[3])
            a_status = r.get("assignment_status")
            state = ("on_site" if a_status == "on_site" else "enroute" if a_status in ("proposed", "approved", "en_route")
                     else "available" if r.get("status") == "available" else "unavailable")
            eta = None
            if r.get("eta_minutes") is not None and a_status != "on_site":
                eta = round(max(0.0, float(r["eta_minutes"]) * (1 - float(r.get("progress") or 0))) * 60, 1)
            route = []
            for lng, lat in (r.get("route") or [])[int(len(r.get("route") or []) * float(r.get("progress") or 0)):]:
                q = to_local(lng, lat, self.anchor)
                if b[0] <= q[0] <= b[2] and b[1] <= q[1] <= b[3]:
                    route.append([q[0], q[1], 0.0])
            entry = {"id": r["id"], "kind": DOPS_TO_SWARM_KIND.get(r["kind"], r["kind"]), "state": state,
                     "position": [*(clamp([x, y]) if off else [x, y]), 0.0], "eta": eta,
                     "agency": r.get("agency_id") or "DisruptionOps", "assignment_id": r.get("assignment_id"),
                     "incident_id": r.get("incident_id"), "location": [r["lng"], r["lat"]], "off_map": off,
                     "label": r.get("label"), "route": route[:60]}
            resources.append(entry)
            iid = r.get("incident_id")
            if iid and iid in mapping:
                incidents[iid] = {"local_id": mapping[iid], "external_id": iid, "status": r.get("incident_status"),
                                  "resolved": r.get("incident_status") == "resolved"}
        return {"source": "disruptionops", "mode": "live", "run_id": self.run_id, "replace": True,
                "incidents": list(incidents.values()), "resources": resources, "handoffs": []}

    async def push_ground(self, *, force: bool = False) -> dict | None:
        """Send DisruptionOps' ground picture to the swarm when it changed (or every 30 s)."""
        if self.ground_mode() != "disruptionops" or not self.connected or self.can_task is False:
            return None
        gp = self.ground_push
        now = time.monotonic()
        if not force and now - gp["at"] < GROUND_PUSH_S:
            return None
        if not hasattr(self.ports, "ground_state"):
            return None
        rows = await self.ports.ground_state(self.anchor, self.bounds)
        payload = self.ground_payload(rows)
        sig = json.dumps([(r["id"], r["state"], [round(v / 5) for v in r["position"][:2]], r["incident_id"]) for r in payload["resources"]]
                         + [(i["external_id"], i["status"]) for i in payload["incidents"]])
        if not force and sig == gp["sig"] and now - gp["at"] < GROUND_KEEPALIVE_S:
            return None
        gp["at"] = now
        ack = await self.command("external_resource_update", payload)
        if ack.get("ok"):
            first = gp["sent"] == 0
            gp.update(sig=sig, sent=gp["sent"] + 1, units=len(payload["resources"]), last_ok=time.time(), error=None)
            if first:
                self._log("ground", f"DisruptionOps now owns the ground picture in the swarm: {len(payload['resources'])} real unit(s) "
                                    "pushed; the swarm's simulated units are off.", frm="disruptionops", level="good")
                real = {r["id"] for r in payload["resources"]}
                for i in self.state.get("incidents") or []:
                    stale = [u for u in i.get("resource_ids") or [] if u not in real]
                    if stale and i.get("status") != "resolved":
                        with contextlib.suppress(Exception):
                            await self.command("unassign", {"incident_id": i["id"], "resource_ids": stale})
        else:
            gp["error"] = str(ack.get("error"))[:200]
            self._log("ground", f"Swarm refused the ground update: {gp['error']}", frm="disruptionops", level="warn", persist=False)
        return ack

    # --- panel tasking -----------------------------------------------------------------
    async def task(self, body: dict, by: str = "officer") -> dict:
        action = body.get("action")
        iid = body.get("incident_id")
        if action == "assign_drone":
            spec = {"count": max(1, min(6, int(body.get("count") or 1))), "role": str(body.get("role") or "assess")[:20]}
            if body.get("drone_id"):
                spec = {"id": body["drone_id"], "count": 1, "role": spec["role"]}
            if body.get("payload"):
                spec["payload"] = body["payload"]
            cmd, payload = "assign", {"incident_id": iid, "drones": [spec]}
        elif action == "assign_unit":
            cmd, payload = "assign", {"incident_id": iid, "resources": [{"id": body["resource_id"], "kind": body.get("kind") or "rescue"}]}
        elif action == "unassign":
            cmd, payload = "unassign", {"incident_id": iid, "drone_ids": body.get("drone_ids") or [], "resource_ids": body.get("resource_ids") or []}
        elif action == "recall":
            cmd, payload = "recall_drone", {"drone_id": body["drone_id"], "reason": str(body.get("reason") or f"recalled by {by}")[:120]}
        elif action == "recheck":
            cmd, payload = "request_situation", {"incident_id": iid}
        elif action == "update":
            payload = {"incident_id": iid}
            for k in ("people", "severity"):
                if body.get(k) is not None:
                    payload[k] = int(body[k])
            cmd = "update_incident"
        elif action in ("close", "cancel"):
            cmd, payload = f"{action}_incident", {"incident_id": iid, "reason": str(body.get("reason") or f"{action}d by {by}")[:120]}
        elif action == "new_task":
            lng, lat = float(body["lng"]), float(body["lat"])
            x, y = to_local(lng, lat, self.anchor)
            b = self.bounds
            if not (b[0] <= x <= b[2] and b[1] <= y <= b[3]):
                raise ValueError(f"That point is outside the swarm's map ({abs(b[0]):.0f} m around its base); pick a point inside the drone area")
            kind = body.get("kind") or "assess"
            cmd, payload = "agency_request", {
                "request_id": f"dops-panel-{uuid.uuid4().hex[:10]}", "kind": kind, "position": to_local(lng, lat, self.anchor),
                "category": body.get("category") or "flood", "people": max(1, min(200, int(body.get("people") or 1))),
                **({"needs": body["needs"]} if body.get("needs") else
                   {"needs": ["food", "first_aid", "medical"]} if kind == "supply" else
                   {"needs": ["assessment"]} if kind == "assess" else
                   {"needs": ["rescue", "first_aid"]} if (body.get("category") or "") in ("trapped", "flood") else {}),
                "priority": max(1, min(5, int(body.get("priority") or 4))), "requested_by": f"disruptionops:panel:{by}"[:60],
                "title": (body.get("note") or "")[:120] or None}
        elif action == "replan":
            plan = await self.make_plan(iid, f"officer {by} asked for a re-plan", force=True)
            return {"ok": True, "plan": plan}
        elif action == "orchestrator":
            mode = "external" if body.get("value", True) else "internal"
            self.take_command = mode == "external"
            cmd, payload = "control", {"orchestrator": mode}
        elif action == "autonomy":
            cmd, payload = "control", {"autonomy": "paused" if body.get("paused") else "running", "reason": body.get("reason") or f"by {by}", "by": by[:40]}
        else:
            raise ValueError("Unknown task action")
        ack = await self.command(cmd, payload)
        ok = bool(ack.get("ok"))
        res = ack.get("result") or {}
        self._log("task", f"{by}: {action} {iid or body.get('drone_id') or ''} -> "
                          f"{'accepted' if ok else 'refused: ' + str(ack.get('error'))}"
                          + (f" ({res.get('reason')})" if res.get("reason") else ""),
                  incident_id=iid or res.get("incident_id"), frm="disruptionops", level="good" if ok else "warn",
                  data={"command": cmd, "payload": payload, "result": res})
        if ok and cmd == "assign" and iid:
            units = [r["id"] for r in payload.get("resources") or []]
            drones = len([d for d in payload.get("drones") or []])
            have = sum(1 for d in self.state.get("drones") or [] if d.get("task_id") == iid)
            self.expect[iid] = {"plan_id": "", "units": units, "drones": have + drones if drones else 0,
                                "due": time.monotonic() + RECHECK_S, "rechecked": False}
        return {"ok": ok, "error": ack.get("error"), "result": res}

    # --- the LangGraph agent's aerial step -----------------------------------------------
    async def agent_cycle(self, *, city_id: str = "pune", trigger: str = "") -> dict:
        """Called by the agent graph's `aerial` node on every cycle. Decides drone work for DisruptionOps incidents:
        new requests (assess unverified reports, eyes on severe / life-safety incidents, supply drops), keeps the
        swarm in step (severity / people changes) and stands it down on resolved incidents. Returns what it did and why."""
        out: dict = {"connected": self.connected, "can_task": self.can_task, "decisions": [], "skipped": None}
        if not self.connected:
            out["skipped"] = "swarm not connected"
            return out
        if self.can_task is False:
            out["skipped"] = "swarm token is view-only"
            return out
        if self.ports.autonomy_paused():
            out["skipped"] = "emergency stop: no drone tasking"
            return out
        before = set(self.sent)
        try:
            await self.scan_once()                       # bounded to the swarm map; dedups per incident+kind
        except Exception as exc:  # noqa: BLE001
            out["decisions"].append({"action": "scan_failed", "why": f"{type(exc).__name__}: {str(exc)[:120]}"})
        for rid in [r for r in self.sent if r not in before]:
            info = self.sent[rid]
            why = {"assess": "report not verified yet: send a drone to look before ground units commit",
                   "sos": "severe / life-safety incident: eyes on scene for the planner",
                   "supply": "supplies short: drone drop until a truck can get through"}.get(info.get("kind"), "aerial help")
            out["decisions"].append({"action": f"request_{info.get('kind')}", "incident_id": info["incident_id"],
                                     "swarm_incident_id": info.get("swarm_incident_id"), "why": why,
                                     "answer": info.get("reply")})
            self._log("agent", f"Agent graph tasked the swarm ({info.get('kind')}) for incident {str(info['incident_id'])[:8]}: {why}.",
                      incident_id=info.get("swarm_incident_id"), frm="agent:langgraph", level="info")
        # keep the swarm in step with DisruptionOps: resolved -> close; severity up -> update
        try:
            from app.db import session as db
            ids = [i["incident_id"] for i in self.sent.values() if i.get("swarm_incident_id")]
            rows = await db.fetch("select id::text id, status::text status, severity, report_count from incidents where id = any($1::uuid[])",
                                  ids) if ids else []
        except Exception:  # noqa: BLE001
            rows = []
        by = {r["id"]: r for r in rows}
        for rid, info in list(self.sent.items()):
            row, sid = by.get(info["incident_id"]), info.get("swarm_incident_id")
            sw = self._inc(sid)
            if not row or not sw or sw.get("status") == "resolved":
                continue
            if row["status"] == "resolved":
                ack = await self.command("close_incident", {"incident_id": sid, "reason": "resolved in DisruptionOps"})
                out["decisions"].append({"action": "close", "swarm_incident_id": sid, "why": "resolved in DisruptionOps: stand the drones down",
                                         "ok": ack.get("ok")})
                self._log("agent", f"Agent graph stood the swarm down on {sid}: resolved in DisruptionOps.", incident_id=sid,
                          frm="agent:langgraph", level="good")
            elif int(row["severity"] or 0) > int(sw.get("severity") or 0):
                ack = await self.command("update_incident", {"incident_id": sid, "severity": int(row["severity"]),
                                                             "people": max(int(sw.get("people") or 1), int(row["report_count"] or 1))})
                out["decisions"].append({"action": "update", "swarm_incident_id": sid,
                                         "why": f"severity rose to {row['severity']} in DisruptionOps", "ok": ack.get("ok")})
        out["open_swarm_incidents"] = sum(1 for i in self.state.get("incidents") or [] if i.get("status") != "resolved")
        out["drones_busy"] = sum(1 for d in self.state.get("drones") or [] if d.get("task_id"))
        return out

    # --- worker: reasoning and persistence (never on the socket reader) ------------------
    async def _worker(self) -> None:
        while True:
            item = await self.work.get()
            try:
                what = item[0]
                if what == "persist":
                    it = item[1]
                    if it["kind"] in ("plan",):         # plans are recorded with their reasoning in make_plan
                        continue
                    dops_id, ward = self._dops_incident(it.get("incident_id"))
                    await self.ports.record(f"drone.{it['kind']}", dops_id, ward,
                                            {"swarm_incident_id": it.get("incident_id"), "from": it["from"], "text": it["text"],
                                             "level": it["level"], "data": it["data"], "simulation_only": True})
                elif what == "reason":
                    await self.make_plan(item[1], item[2])
                elif what == "alert":
                    iid, body = item[1], item[2] or {}
                    extra = []
                    if body.get("alert") == "drone_fault" and iid:
                        extra = [{"action": "assign", "payload": {"incident_id": iid, "drones": [{"count": 1, "role": "assess"}]},
                                  "why": f"replace {body.get('drone_id')} ({body.get('fault')})", "expect": {}}]
                    if iid:
                        await self.make_plan(iid, f"alert: {body.get('alert')}", force=True, extra=extra)
                    if body.get("alert") in ("life_threat", "situation_worsening"):
                        from app.agents import event_router
                        event_router.request_replan(f"drone alert {body.get('alert')} at {iid}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"worker: {type(exc).__name__}: {str(exc)[:160]}"

    # --- only ask for help inside the swarm's map ---------------------------------------
    async def scan_once(self) -> int:
        real = self.ports.candidates
        b = self.bounds

        async def inside():
            out = []
            for c in await real():
                x, y = to_local(c["lng"], c["lat"], self.anchor)
                if b[0] <= x <= b[2] and b[1] <= y <= b[3]:
                    out.append(c)
            return out
        self.ports.candidates = inside
        try:
            return await super().scan_once()
        finally:
            self.ports.candidates = real

    # --- connection ---------------------------------------------------------------------
    async def run(self) -> None:
        from urllib.parse import quote
        from websockets.asyncio.client import connect
        backoff = 1.0
        worker = asyncio.create_task(self._worker())
        try:
            while True:
                try:
                    sep = "&" if "?" in self.url else "?"
                    async with connect(f"{self.url}{sep}token={quote(self.token)}", max_size=8_000_000, open_timeout=20,
                                       additional_headers={"ngrok-skip-browser-warning": "1", "User-Agent": "DisruptionOps-swarm-link"}) as ws:
                        self._ws, self.connected, self._primed, self._primed_msgs, backoff = ws, True, False, False, 1.0
                        self.last_error = ""
                        reader = asyncio.create_task(self._reader(ws))
                        self._log("link", f"Connected to the drone swarm ({self.url.split('/')[2]}).", frm="disruptionops", level="good")
                        try:
                            await asyncio.sleep(1.0)
                            if self.take_command:
                                ack = await self.command("control", {"orchestrator": "external"})
                                if ack.get("ok"):
                                    self._log("link", "DisruptionOps took command: the swarm's stand-in orchestrator is off; we dispatch.",
                                              frm="disruptionops", level="good")
                            await asyncio.sleep(1.5)
                            open_ids = [i["id"] for i in self.state.get("incidents") or [] if i.get("status") != "resolved"]
                            if open_ids:
                                self._log("review", f"Reviewing {len(open_ids)} open swarm incident(s) now that we are connected.",
                                          frm="disruptionops", level="info")
                            try:
                                await self.push_ground(force=True)
                            except Exception as exc:  # noqa: BLE001
                                self.last_error = f"ground: {type(exc).__name__}: {str(exc)[:120]}"
                            for iid in open_ids:
                                self._queue("reason", iid, "initial review on connect")
                            # Wake the agent graph: its aerial node decides drone work for DisruptionOps' incidents.
                            with contextlib.suppress(Exception):
                                from app.agents import event_router
                                event_router.request_replan("drone swarm linked")
                            last_scan = 0.0
                            while not reader.done():
                                # The agent graph's aerial node tasks the swarm on every cycle; this timer is only the
                                # fallback when the graph is off (or between cycles, for a request still unsent).
                                period = float(env("SWARM_SCAN_S", "10")) * (6 if _graph_drives() else 1)
                                if time.monotonic() - last_scan >= period:
                                    last_scan = time.monotonic()
                                    try:
                                        await self.scan_once()
                                        if self.orchestrate:
                                            await self.orchestrate_once()
                                    except Exception as exc:  # noqa: BLE001
                                        self.last_error = f"scan: {type(exc).__name__}: {str(exc)[:120]}"
                                try:
                                    await self.push_ground()
                                except Exception as exc:  # noqa: BLE001
                                    self.ground_push["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
                                try:
                                    await self._tick()
                                except Exception as exc:  # noqa: BLE001
                                    self.last_error = f"tick: {type(exc).__name__}: {str(exc)[:120]}"
                                await asyncio.sleep(1.0)
                            with contextlib.suppress(Exception):
                                reader.result()
                        finally:
                            reader.cancel()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                finally:
                    if self.connected:
                        self._log("link", f"Disconnected from the swarm ({self.last_error or 'closed'}); retrying.", frm="disruptionops",
                                  level="warn", persist=False)
                    self._ws, self.connected = None, False
                    for f in self._pending.values():
                        if not f.done():
                            f.set_exception(ConnectionError("connection lost"))
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2)
        finally:
            worker.cancel()

    # --- view ---------------------------------------------------------------------------
    def view(self, log_after: int = 0) -> dict:
        s = self.state
        geo = lambda p: to_geo(p[0], p[1], self.anchor) if p else (None, None)  # noqa: E731
        drones = []
        for d in s.get("drones") or []:
            lng, lat = geo(d.get("position"))
            drones.append({"id": d.get("id"), "state": d.get("state"), "battery": d.get("battery"), "lng": lng, "lat": lat,
                           "task_id": d.get("task_id"), "activity": d.get("activity") or d.get("behavior"), "active": d.get("active"),
                           "link": d.get("link"), "payload": d.get("payload")})
        units = []
        for r in s.get("resources") or []:
            lng, lat = geo(r.get("position"))
            units.append({"id": r.get("id"), "kind": r.get("kind"), "state": r.get("state"), "incident_id": r.get("incident_id"),
                          "eta": r.get("eta"), "agency": r.get("agency"), "lng": lng, "lat": lat, "capacity": r.get("capacity")})
        incidents = []
        for i in s.get("incidents") or []:
            lng, lat = geo(i.get("position"))
            rep = self.reports.get(i.get("id")) or {}
            dops_id, _ = self._dops_incident(i.get("id"))
            incidents.append({
                "id": i.get("id"), "title": i.get("title"), "category": i.get("category"), "status": i.get("status"),
                "people": i.get("people"), "severity": i.get("severity"), "lng": lng, "lat": lat,
                "evacuated": i.get("evacuated_people"), "control_mode": i.get("control_mode"),
                "drones": [d["id"] for d in drones if d["task_id"] == i.get("id")],
                "units": [u["id"] for u in units if u["incident_id"] == i.get("id")],
                "dops_incident_id": dops_id, "wait_reason": i.get("wait_reason"),
                "report": {"text": self.report_text.get(i.get("id")),
                           "people": (rep.get("people") or {}).get("estimate"), "injured": (rep.get("injured") or {}).get("estimate"),
                           "hazards": rep.get("hazards"), "trend": rep.get("trend"), "confidence": rep.get("confidence"),
                           "observed_at": rep.get("observed_at"), "best_approach": (rep.get("access") or {}).get("best_approach")} if rep else None,
                "expecting": ({k: v for k, v in self.expect[i["id"]].items() if k != "due"} | {"due_in_s": round(self.expect[i["id"]]["due"] - time.monotonic())})
                if i.get("id") in self.expect else None})
        return {
            "enabled": True, "connected": self.connected, "url": self.url, "protocol": PROTOCOL, "last_error": self.last_error,
            "run_id": self.run_id, "t": s.get("t"), "scenario": (s.get("scenario") or {}).get("id"),
            "coordination": (s.get("coordination") or {}).get("mode"), "control": s.get("control"),
            "ground": {"owner": self.ground_mode(), **{k: v for k, v in self.ground_push.items() if k != "sig"}},
            "filed": {k: {**v, "incident_id": str(v["incident_id"])} for k, v in list(self.filed.items())[-20:]},
            "auto": self.auto, "take_command": self.take_command, "can_task": self.can_task, "anchor": {"lat": self.anchor[0], "lon": self.anchor[1]},
            "bounds": self.bounds, "drones": drones, "units": units, "incidents": incidents,
            "log": [x for x in self.log if x["seq"] > log_after][-200:][::-1],
            "plans": list(self.plans)[-40:][::-1], "stats": self.link_stats, "agency": SwarmAgency.status(self),
        }


# ------------------------------------------------------------------ app wiring --
_link: SwarmLink | None = None
_task: asyncio.Task | None = None


def enabled() -> bool:
    return _on("SWARM_AGENCY", "0") and bool(env("SWARM_WS_URL"))


async def start(url: str | None = None, token: str | None = None) -> None:
    """Called from app.main lifespan. Safe to call again with a new URL/token (restarts the link)."""
    global _link, _task
    await stop()
    url = url or env("SWARM_WS_URL")
    if not url:
        return
    token = token if token is not None else env("SWARM_INTEGRATION_TOKEN")
    prev = _link
    _link = SwarmLink(url=url.rstrip("/"), token=token, anchor=parse_anchor(env("SWARM_ANCHOR")) if env("SWARM_ANCHOR") else ANCHOR,
                      orchestrate=_on("SWARM_ORCHESTRATE", "0"), auto=_on("SWARM_LINK_AUTO", "1") if prev is None else prev.auto,
                      take_command=_on("SWARM_TAKE_COMMAND", "1") if prev is None else prev.take_command)
    if prev is not None:                    # keep history across a URL change
        _link.log, _link.plans, _link.sent, _link.seen = prev.log, prev.plans, prev.sent, prev.seen
    _task = asyncio.create_task(_link.run())


async def stop() -> None:
    global _task
    if _task:
        _task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await _task
        _task = None


def link() -> SwarmLink | None:
    return _link


def status(log_after: int = 0) -> dict:
    if _link is None:
        return {"enabled": False, "connected": False, "url": env("SWARM_WS_URL") or None,
                "hint": "Set SWARM_AGENCY=1, SWARM_WS_URL and SWARM_INTEGRATION_TOKEN, or paste the URL in the panel."}
    return _link.view(log_after)
