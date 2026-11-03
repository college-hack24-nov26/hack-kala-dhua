"""Named end-to-end scenarios, replayed on held-out storms, with every log the
MISC-04 brief asks for.

    python -m ml.scenarios                      # all scenarios -> data/scenarios/<name>/
    python -m ml.scenarios --only confirmed_closure

What one scenario is
--------------------
A held-out (test-split, 2023-2026) simulated run of the PCMC Pawana corridor:
the weather and river drivers are the real historical ones (ERA5 rain, GloFAS
river discharge for that window); the roads, hazards, people and reports are
simulated (ml.sim). The command centre only ever sees the *evidence* (reports,
patrols, sensors, gauges, traffic, official notices) - never the truth.

Over the scenario window, every 30 minutes the planning loop does what the live
system does:

  1. ingest the evidence that has arrived; group reports into incidents;
     corroborate, dispute or expire them (the trust rules below);
  2. turn confirmed incidents into needs per capability (a fire with people
     trapped needs a fire tender, an ambulance and police);
  3. route every unit on what the policy believes about the roads;
  4. assign units to needs with the production solver
     (app.solver.allocation.allocate: CP-SAT, capability-aware, one job per
     unit, a 45-minute reach limit, a switching cost that preserves committed
     actions);
  5. check every en-route unit's remaining route against new information and
     reroute it if a road it will use is now closed (or predicted closed).

Units then *drive through the simulated truth*. A route that reaches a road that
is really blocked at that moment is an invalid route: the crew turns back (60 s),
reports the road (it becomes a binding closure for everyone in that policy) and
is rerouted. On scene they work for a service time, buses carry people to a
shelter (capacity is reserved at dispatch, so a shelter can never overflow),
ambulances carry patients to the nearest hospital, JCBs clear the debris they
were sent to (the world changes).

Policies (same storm, same evidence, same fleet, same shelters, same injected
events - matched inputs):

  current_status  roads closed only where the evidence says so now (B0, the
                  baseline the brief asks for)
  predicted       current_status's closures stay binding, plus the passability
                  model's P(blocked at arrival) and its uncertainty:
                  cost x (1 + 4p) + 900 s x p; p >= 0.6, or p >= 0.4 with
                  p + 2 sd >= 0.6, is avoided (as live, app/nav/live.py)
  oracle          the truth at the decision time (best possible, for scale only)
  naive_trust     (conflicting_stale_reports only) predicted routing, but every
                  report is believed at once: no corroboration, no disputes,
                  no expiry - the ablation of the trust rules

Trust rules (identical in every policy except naive_trust)
----------------------------------------------------------
  confirmed   2+ independent reports within 250 m in 60 min; or a sensor
              within 300 m >= 0.15 m in the last 30 min; or a patrol within
              60 m in the last 60 min saw it; or one report of a fire, crash,
              collapse or gas with reliability >= 0.85
  disputed    a sensor within 150 m reads < 0.05 m (last 30 min), or a patrol
              within 60 m in the last 30 min saw the road open and dry
  unverified  otherwise. Unverified and disputed incidents get one
              "go and assess" need (police, rescue team or ambulance); the
              assessor's arrival reveals the truth: confirmed or dismissed
  expired     unverified for 2 h with nothing new; or a "clear"/"cleared"
              report within 150 m after the last supporting report

Timestamps
----------
Every logged row carries `t_replay` (the historical timestamp of that moment
in the storm's real weather window, UTC), `t_min` (minutes from the window
start) and `time_label` - always "REPLAY", never presented as live.

Outputs per scenario (data/scenarios/<name>/)
---------------------------------------------
  events.jsonl         the replay log: every decision, dispatch, reroute,
                       arrival, closure, dispute, aid request, shelter opening
  dispatch_log.csv     every assignment: unit, need, capability, planned ETA,
                       solver engine, why
  route_checks.csv     every leg driven: planned vs actual minutes, invalid
                       (hit a really blocked road), reroutes, max p on route
  resource_ledger.csv  every unit and shelter state change, with checks
  unmet.csv            needs left without a unit at each decision, with reason
  predictions.parquet  P(blocked) the predicted policy used on its routes, with
                       the truth at the time the unit got there (label)
  summary.json         matched outcomes per policy

Assumptions (documented, not tuned on the test storms): service times, people
per flooded place (40 + 260 x density for waist-deep, 20 + 120 x density for
knee-deep, density being the segment's normalised building density), the
reach radius at which a unit counts as arrived (150 m; 400 m bus assembly
point; 600 m boat/foot launch), boats and rescue teams travel by road on a
truck (supply-truck and pump-truck rules) and cover the last stretch at 8 and
3 km/h, units based outside the modelled road graph are excluded.
"""
from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

from app.nav.profiles import PROFILES
from ml.district import PROCESSED, SIM
from ml.features import PROFILE_CODES, build
from ml.labels import seconds, static_arrays
from ml.route_eval import AVOID_P, DETOUR_S, TURNBACK_S, Run, graph, known_state, path, shortest
from ml.sim.static import load_static
from ml.sim.storms import STEP_MIN
from ml.sim.world import F_BRIDGE, F_COLLAPSE, F_DEBRIS, F_LANDSLIDE, F_WIRE

OUT = PROCESSED.parent / "scenarios"
SNAPSHOT = PROCESSED.parent / "scenarios" / "district_snapshot.json"
PLAN_MIN = 10          # the planner runs every 10 minutes (live: on every event)
BELIEF_MIN = 30        # evidence features / model scores are refreshed every 30 minutes (live: 5)
TIME_LABEL = "REPLAY"
UNCERTAIN_P, UNCERTAIN_SD = 0.4, 2.0

TRAVEL = {"ambulance": "ambulance", "police": "police", "fire_engine": "fire_engine", "bus": "bus",
          "jcb": "jcb", "pump": "pump", "supply_truck": "supply_truck", "water_tanker": "water_tanker",
          "boat": "supply_truck", "rescue_team": "pump"}
LAST_LEG_KMH = {"boat": 8.0, "rescue_team": 3.0}
REACH_M = {"mass_transport": 400, "water_rescue": 600, "search_rescue": 600}
SERVICE_MIN = {"water_rescue": 45, "search_rescue": 60, "mass_transport": 15, "medical_transport": 10,
               "dewatering": 60, "debris_clearance": 40, "traffic_control": 45, "fire_suppression": 60,
               "field_assessment": 5, "medical_care": 20}
LIFE_SAFETY = {"search_rescue", "water_rescue", "medical_transport", "medical_care", "fire_suppression"}
HAZARD_NEEDS = {
    "fire": (5, {"fire_suppression": 1, "medical_transport": 1, "traffic_control": 1}),
    "gas": (4, {"fire_suppression": 1, "traffic_control": 1}),
    "collapse": (5, {"search_rescue": 1, "debris_clearance": 1, "medical_transport": 1}),
    "crash": (4, {"medical_transport": 1, "traffic_control": 1}),
    "tree": (3, {"debris_clearance": 1}),
    "landslide": (4, {"debris_clearance": 1, "traffic_control": 1}),
    "wire": (3, {"traffic_control": 1}),
}
SELF_CONFIRMING = {"fire", "crash", "collapse", "gas"}
INCIDENT_KINDS = {"flood"} | set(HAZARD_NEEDS)
TRUTH_KIND = {"tree": ("tree",), "wire": ("wire",), "collapse": ("collapse",), "landslide": ("landslide",),
              "fire": ("fire",), "gas": ("gas",), "crash": ("crash", "breakdown")}


def _hash01(*parts) -> float:
    h = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF


# ----------------------------------------------------------------- scenarios --
SCENARIOS: dict[str, dict] = {
    "normal_operation": dict(
        storm="20230516-06", k=0, hours=12, anchor="events",
        title="Normal operation: an ordinary day",
        story="A dry pre-monsoon day: crashes, breakdowns and fallen trees, no flooding. The planner "
              "dispatches the right mix per incident and nothing should go wrong.",
        policies=("current_status", "predicted", "oracle")),
    "deteriorating_access": dict(
        storm="20240724-08", k=0, hours=12, anchor="rise",
        title="Deteriorating access: the Pawana rises during the response",
        story="July 2024 monsoon window. The river climbs through the window; roads that are open when a "
              "unit leaves are under water when it gets there. Current-status routing finds out on the road; "
              "predicted routing avoids roads likely to close before arrival.",
        policies=("current_status", "predicted", "oracle")),
    "confirmed_closure": dict(
        storm="20250927-18", k=0, hours=10, anchor="flood",
        title="Confirmed closure: a bridge is closed mid-response",
        story="September 2025 storm. Three hours in, the authority closes the most-used river bridge "
              "(official notice 10 minutes later). The closure is binding in every policy; units whose "
              "remaining route crosses it are rerouted, everyone else keeps their commitment.",
        inject_closure_after_min=180, policies=("current_status", "predicted", "oracle")),
    "insufficient_capacity": dict(
        storm="20260704-23", k=1, hours=12, anchor="flood",
        title="Insufficient shelter and transport: the surge ladder",
        story="July 2026, the largest held-out flood. Only 40% of the fleet is on shift and shelters have 25% "
              "of their space (the rest already full from the night before). Needs go unmet with reasons; the "
              "surge ladder triages, asks for mutual aid (which arrives as real units, or is declined), opens "
              "schools as shelters and requests a declaration that an officer approves.",
        fleet_keep=0.4, shelter_scale=0.25, surge=True, policies=("current_status", "predicted")),
    "conflicting_stale_reports": dict(
        storm="20240925-00", k=0, hours=12, anchor="flood",
        title="Conflicting and stale reports",
        story="September 2024: many rumours, hoaxes and stale forwards in the feed. Sensors and patrols "
              "contradict some reports; old reports are re-shared as new. Unverified and disputed incidents "
              "get an assessor first; the ablation that believes everything sends crews to places that are fine.",
        policies=("predicted", "naive_trust", "current_status")),
}


# ------------------------------------------------------------------- world ---
@dataclass
class UnitState:
    id: str
    kind: str
    label: str
    capacity: int
    node: int
    status: str = "available"           # available | en_route | on_site | offline
    task: str | None = None              # demand id
    incident: str | None = None
    target: int | None = None
    last_leg_s: float = 0.0
    route: list = field(default_factory=list)      # [(seg, frm, to)] remaining plan
    timeline: list = field(default_factory=list)   # [(seg, frm, to, t_in, t_out)]
    depart_t: float = 0.0
    planned_s: float = 0.0
    reroutes: int = 0
    invalid: int = 0
    version: int = 0
    leg_id: int = 0
    shelter: str | None = None
    shelter_n: int = 0
    aid: bool = False


@dataclass
class Incident:
    id: str
    kind: str                # flood | hazard kind
    seg: int
    x: float
    y: float
    first_t: float
    last_support_t: float
    reports: list = field(default_factory=list)   # (t, reliability, source)
    depth_class: int = 0
    status: str = "unverified"   # unverified | disputed | confirmed | dismissed | expired | done
    needs: dict = field(default_factory=dict)      # capability -> required
    served: dict = field(default_factory=dict)     # capability -> completed
    severity: int = 3
    people: int = 0
    ward: str = ""
    first_on_scene: float | None = None
    first_life_safety: float | None = None
    truly_real: bool | None = None


class Replay:
    """One scenario under one policy."""

    def __init__(self, name: str, cfg: dict, policy: str, shared: "Shared"):
        self.name, self.cfg, self.policy, self.sh = name, cfg, policy, shared
        self.run = Run(cfg["storm"], cfg["k"])               # own truth copy (crews change the world)
        self._inject_truth()
        self.t0, self.t1 = shared.t0, shared.t1
        self.known_closed: dict[int, str] = {}             # seg -> why (crew / official)
        self.known_open: set[int] = set()                  # crew-cleared
        self.units: dict[str, UnitState] = {}
        self.incidents: dict[str, Incident] = {}
        self.inc_seq = 0
        self.shelters = {s["id"]: dict(s, occ=0, reserved=0) for s in shared.shelters}
        self.events: list[dict] = []
        self.dispatch: list[dict] = []
        self.routes: list[dict] = []
        self.ledger: list[dict] = []
        self.unmet: list[dict] = []
        self.preds: list[dict] = []
        self.heap: list = []
        self.seq = 0
        self.level, self.up, self.calm, self.decl = 0, 0, 0, False
        self.aid_asked: set[str] = set()
        self.schools_open: set[str] = set()
        self.seen_reports = self.t0 / 60 - 60          # the last hour before the window is the backlog
        self.violations = {"unit_double_assignment": 0, "shelter_overflow": 0}
        for u in shared.units:
            self.units[u["id"]] = UnitState(u["id"], u["kind"], u["label"], int(u["capacity"]), u["node"],
                                            status=u.get("status", "available"))
            self._led("unit", u["id"], self.units[u["id"]].status, self.t0, note="start of window")
        for s in self.shelters.values():
            self._led("shelter", s["id"], "open", self.t0, capacity=s["capacity"], occupancy=0, reserved=0)

    # ------------------------------------------------------------- helpers --
    def _ts(self, t_s: float) -> dict:
        return {"t_min": round(t_s / 60 - self.t0 / 60, 1),
                "t_replay": (self.sh.start + pd.Timedelta(seconds=float(t_s))).isoformat() + "Z",
                "time_label": TIME_LABEL}

    def _log(self, t: float, kind: str, **kw) -> None:
        self.events.append({**self._ts(t), "policy": self.policy, "kind": kind, **kw})

    def _led(self, subject: str, sid: str, state: str, t: float, **kw) -> None:
        self.ledger.append({**self._ts(t), "policy": self.policy, "subject": subject, "id": sid, "state": state, **kw})

    def _push(self, t: float, kind: str, payload) -> None:
        self.seq += 1
        heapq.heappush(self.heap, (t, self.seq, kind, payload))

    def _inject_truth(self) -> None:
        inj = self.sh.injection
        if inj:
            step = int(inj["t_s"] // (STEP_MIN * 60))
            self.run.tr.flags[step:, inj["seg"]] |= F_BRIDGE
            self.run._true.clear()

    # ------------------------------------------------------------- beliefs --
    def _belief(self, t: float, prof: str) -> np.ndarray:
        """Seconds per segment as this policy believes now (inf = avoid), before horizons."""
        b = self.sh.base_cost(t, prof)
        c = b.copy()
        if self.known_closed:
            c[list(self.known_closed)] = np.inf
        if self.known_open:
            ko = list(self.known_open - set(self.known_closed))
            c[ko] = self.sh.free_cost(prof)[ko]
        return c

    def _cost_for_unit(self, t: float, u: UnitState) -> tuple[np.ndarray, np.ndarray | None]:
        prof = TRAVEL[u.kind]
        if self.policy == "oracle":
            c = self.run.true_secs(int(t // (STEP_MIN * 60)), prof).copy()
            if self.known_closed:
                c[list(self.known_closed)] = np.inf
            return c, None
        c = self._belief(t, prof)
        if self.policy in ("predicted", "naive_trust"):
            p, sd = self.sh.p_for(t, prof, self.sh.nxy[u.node])
            if self.known_open:
                p = p.copy()
                p[list(self.known_open)] = 0.0
            fin = np.isfinite(c)
            c = np.where(fin, c * (1 + 4 * p) + DETOUR_S * p, c)
            avoid = (p >= AVOID_P) | ((p >= UNCERTAIN_P) & (p + UNCERTAIN_SD * sd >= AVOID_P))
            c[avoid & ~np.isin(np.arange(len(c)), list(self.known_open))] = np.inf
            return c, p
        return c, None

    # ---------------------------------------------------------- evidence ---
    def _ingest(self, t: float) -> None:
        R = self.sh.reports
        tmin = t / 60
        new = R[(R["t_obs_min"] <= tmin) & (R["t_obs_min"] > self.seen_reports)]
        self.seen_reports = tmin
        naive = self.policy == "naive_trust"
        for r in new.itertuples():
            if r.source == "official" and r.kind == "closure":
                self.known_closed[int(r.seg_rep)] = "official closure notice (binding)"
                self._log(r.t_obs_min * 60, "closure.official", seg=int(r.seg_rep),
                          note="binding: no route may use this road until a reopen notice")
                continue
            if r.source == "official" and r.kind == "reopen":
                self.known_closed.pop(int(r.seg_rep), None)
                continue
            if r.source in ("official", "citizen") and r.kind in ("cleared", "clear"):
                for inc in self._near(r.x, r.y, 150):
                    if inc.status in ("unverified", "disputed", "confirmed") and r.t_obs_min * 60 > inc.last_support_t \
                            and inc.kind in ("flood", "tree", "wire", "crash") and not naive:
                        self._close(inc, t, "expired", f"'{r.kind}' report after the last supporting report")
                continue
            if r.source != "citizen" or r.kind not in INCIDENT_KINDS:
                continue
            fam = r.kind
            hit = [i for i in self._near(r.x, r.y, 250) if i.kind == fam and i.status not in ("dismissed", "expired", "done")]
            if hit:
                inc = hit[0]
            else:
                self.inc_seq += 1
                inc = Incident(f"I{self.inc_seq:03d}", fam, int(r.seg_rep), float(r.x), float(r.y),
                               r.t_obs_min * 60, r.t_obs_min * 60, ward=str(self.sh.st["ward_id"].iloc[int(r.seg_rep)]))
                inc.truly_real = self.sh.truth_real(fam, int(r.seg_rep), r.t_obs_min)
                self.incidents[inc.id] = inc
                self._log(r.t_obs_min * 60, "incident.reported", incident=inc.id, category=fam, seg=inc.seg,
                          ward=inc.ward, reliability=round(float(r.reliability or 0.5), 2))
            inc.reports.append((r.t_obs_min * 60, float(r.reliability or 0.5), r.source))
            inc.last_support_t = r.t_obs_min * 60
            if fam == "flood":
                inc.depth_class = max(inc.depth_class, int(r.depth_class if r.depth_class >= 0 else 1))
        for inc in list(self.incidents.values()):
            if inc.status in ("dismissed", "expired", "done"):
                continue
            self._judge(inc, t, naive)

    def _near(self, x: float, y: float, r: float) -> list[Incident]:
        return [i for i in self.incidents.values() if math.hypot(i.x - x, i.y - y) <= r]

    def _judge(self, inc: Incident, t: float, naive: bool) -> None:
        if inc.status == "confirmed":
            return
        if naive:
            self._confirm(inc, t, "believed at once (naive_trust ablation)")
            return
        recent = [rr for rr in inc.reports if rr[0] >= t - 3600]
        why = None
        if len(recent) >= 2:
            why = f"{len(recent)} independent reports within 250 m in 60 min"
        elif inc.kind in SELF_CONFIRMING and max(rr[1] for rr in inc.reports) >= 0.85:
            why = "high-reliability report of a fire/crash/collapse/gas"
        sv = self.sh.sensor_near(inc.x, inc.y, t)
        pt = self.sh.patrol_near(inc.x, inc.y, t)
        if inc.kind == "flood":
            if sv is not None and sv[0] >= 0.15 and sv[1] <= 30:
                why = f"water sensor within 300 m reads {sv[0]:.2f} m"
            elif pt is not None and pt["age"] <= 60 and (pt["blocked"] == 1 or (pt["depth"] or 0) >= 0.15):
                why = "patrol saw water on the road"
            disputed = ((sv is not None and sv[2] <= 150 and sv[0] < 0.05 and sv[1] <= 30)
                        or (pt is not None and pt["age"] <= 30 and pt["blocked"] == 0 and (pt["depth"] or 0) < 0.05))
            if disputed and why is None and inc.status != "disputed":
                inc.status = "disputed"
                self._log(t, "incident.disputed", incident=inc.id,
                          reason="sensor reads dry" if (sv is not None and sv[0] < 0.05) else "patrol saw the road open and dry",
                          action="send one assessor before any crew")
                self._set_needs(inc, t)
                return
        else:
            if pt is not None and pt["age"] <= 60 and pt["blocked"] == 1:
                why = why or "patrol confirmed the blockage"
        if why:
            self._confirm(inc, t, why)
            return
        if t - inc.last_support_t >= 7200 and inc.status in ("unverified", "disputed"):
            self._close(inc, t, "expired", "unverified for 2 h with nothing new (likely stale or false)")
            return
        if not inc.needs:
            self._set_needs(inc, t)

    def _confirm(self, inc: Incident, t: float, why: str) -> None:
        inc.status = "confirmed"
        self._log(t, "incident.confirmed", incident=inc.id, category=inc.kind, reason=why)
        self._set_needs(inc, t)

    def _set_needs(self, inc: Incident, t: float) -> None:
        old = dict(inc.needs)
        if inc.status in ("unverified", "disputed"):
            inc.needs, inc.severity = {"field_assessment": 1}, 2
        elif inc.kind == "flood":
            dens = float(self.sh.st["density"].iloc[inc.seg])
            if inc.depth_class >= 3:
                inc.people = int(40 + 260 * dens)
                inc.severity = 5
                inc.needs = {"water_rescue": 1, "mass_transport": min(3, math.ceil(inc.people / 45))}
            elif inc.depth_class == 2:
                inc.people = int(20 + 120 * dens)
                inc.severity = 4
                inc.needs = {"mass_transport": min(2, math.ceil(inc.people / 45)), "dewatering": 1}
            else:
                inc.severity, inc.needs = 2, {"dewatering": 1}
        else:
            inc.severity, needs = HAZARD_NEEDS[inc.kind]
            inc.needs = dict(needs)
        for c in list(inc.served):
            if c not in inc.needs:
                inc.served.pop(c)
        if inc.needs != old:
            self._log(t, "incident.needs", incident=inc.id, status=inc.status, needs=inc.needs,
                      people=inc.people, severity=inc.severity)

    def _close(self, inc: Incident, t: float, status: str, why: str) -> None:
        inc.status = status
        self._log(t, f"incident.{status}", incident=inc.id, reason=why, truly_real=inc.truly_real)
        for u in self.units.values():
            if u.incident == inc.id and u.status == "en_route":
                self._free(u, t, f"incident {status}: {why}")

    # ----------------------------------------------------------- planning ---
    def _demands(self, t: float):
        from app.solver.allocation import Demand
        out, meta = [], {}
        active: dict[tuple[str, str], int] = {}
        for u in self.units.values():
            if u.task and u.status in ("en_route", "on_site"):
                inc, cap, _ = u.task.split(":")
                active[(inc, cap)] = active.get((inc, cap), 0) + 1
        enroute: dict[tuple[str, str], list[UnitState]] = {}
        onsite: dict[tuple[str, str], int] = {}
        for u in self.units.values():
            if u.task and u.status == "en_route":
                inc_id, cap, _ = u.task.split(":")
                enroute.setdefault((inc_id, cap), []).append(u)
            elif u.task and u.status == "on_site":
                inc_id, cap, _ = u.task.split(":")
                onsite[(inc_id, cap)] = onsite.get((inc_id, cap), 0) + 1
        for inc in self.incidents.values():
            if inc.status not in ("unverified", "disputed", "confirmed"):
                continue
            for cap, req in inc.needs.items():
                left = req - inc.served.get(cap, 0) - onsite.get((inc.id, cap), 0)
                # en-route units keep a slot each (their task id is re-keyed onto
                # the open slots), so a finished unit never leaves a ghost slot
                for j, u in enumerate(enroute.get((inc.id, cap), [])):
                    if j < left:
                        u.task = f"{inc.id}:{cap}:{j}"
                    else:
                        self._log(t, "unit.released", unit=u.id, demand=u.task, reason="need already met")
                        self._free(u, t, "need already met")
                for i in range(left):
                    did = f"{inc.id}:{cap}:{i}"
                    pr = 1.0
                    if self.level >= 1:
                        pr = (2.0 if cap in LIFE_SAFETY else 1.0) * (1.3 if self.sh.elderly.get(inc.ward, 0) >= 0.10 else 1.0)
                    out.append(Demand(id=did, ward_id=inc.ward, incident_id=inc.id, capability=cap,
                                      purpose=f"{cap} at {inc.kind} {inc.id}",
                                      location=tuple(self.sh.seg_ll(inc.seg)), severity=inc.severity,
                                      population_at_risk=max(inc.people, 10), priority=pr))
                    meta[did] = (inc, cap)
        return out, meta, active

    def plan(self, t: float) -> None:
        from app.solver.allocation import Unit, allocate
        from app.solver.routing import TravelMatrix
        self._ingest(t)
        self._aid_arrivals(t)
        self._reroute_check(t)
        demands, meta, _ = self._demands(t)
        waiting = []
        pool = [u for u in self.units.values() if u.status in ("available", "en_route")]
        if not demands:
            return
        # current commitments: a unit's task id maps onto the same demand id slot
        current, progress = {}, {}
        for u in pool:
            if u.status == "en_route" and u.task:
                current[u.id] = u.task
                progress[u.id] = min(1.0, (t - u.depart_t) / max(u.planned_s, 1))
        # shelter capacity for mass transport: a bus needs a shelter place to go to
        free_places = sum(max(0, s["capacity"] - s["occ"] - s["reserved"]) for s in self.shelters.values())
        bus_slots = free_places // 45
        if bus_slots <= 0:
            keep = []
            for d in demands:
                if d.capability == "mass_transport" and current_holder(current, d.id) is None:
                    self._unmet(t, d, "no shelter place left for the people this bus would carry")
                    waiting.append(d)
                else:
                    keep.append(d)
            demands = keep
        if not demands:
            if self.cfg.get("surge"):
                self._surge(t, waiting)
            return
        dur = np.full((len(pool), len(demands)), 1e6)
        dist = np.zeros((len(pool), len(demands)))
        routes = {}
        for ui, u in enumerate(pool):
            if u.status == "en_route":
                node, t_free = self._position(u, t)
            else:
                node, t_free = u.node, t
            cost, p = self._cost_for_unit(t, u)
            dists, pred, key = shortest(cost, "road", node)
            for di, d in enumerate(demands):
                inc, cap = meta[d.id]
                tgt, total, last = self._target(dists, inc, cap, u.kind)
                if tgt is None:
                    continue
                mob = 120.0 if u.status == "available" else 0.0
                dur[ui, di] = (total + mob + (t_free - t)) / 60
                dist[ui, di] = math.hypot(*(self.sh.nxy_m[node] - self.sh.nxy_m[tgt])) / 1000
                routes[(u.id, d.id)] = (node, tgt, last, pred, key, t_free, p, total)
        units = [Unit(u.id, u.kind, u.label, "", tuple(self.sh.nxy[u.node]), u.capacity) for u in pool]
        res = allocate(demands, units, TravelMatrix(dur.tolist(), dist.tolist(), "graph"),
                       current=current, progress=progress, time_budget_s=3.0)
        taken: set[str] = set()
        for a in res.allocations:
            u = self.units[a.unit.id]
            if u.id in taken:
                self.violations["unit_double_assignment"] += 1
                continue
            taken.add(u.id)
            if u.task == a.demand.id and u.status == "en_route":
                continue                                      # committed action preserved
            inc, cap = meta[a.demand.id]
            if cap == "mass_transport" and not self._reserve(u, inc, t):
                self._unmet(t, a.demand, "no shelter place left for the people this bus would carry")
                waiting.append(a.demand)
                continue
            prev = u.task
            if u.status == "en_route" and prev:
                self._log(t, "unit.reassigned", unit=u.id, frm=prev, to=a.demand.id,
                          reason="higher-weight need; switching cost paid")
                self._release_shelter(u, t)
            node, tgt, last, pred, key, t_free, p, total = routes[(u.id, a.demand.id)]
            rt = path(pred, key, node, tgt) or []
            u.node = node
            self._start(u, inc, cap, a.demand.id, rt, tgt, last, t_free, p, a.eta_minutes, res.engine, t, total)
        # a committed unit the solver did not keep: if its need went to another
        # unit, it is released; otherwise it carries on (committed action kept)
        given = {a.demand.id: a.unit.id for a in res.allocations}
        for u in pool:
            if u.status == "en_route" and u.id not in taken and u.task in given and given[u.task] != u.id:
                self._log(t, "unit.released", unit=u.id, demand=u.task, reason="need covered by a closer unit")
                self._free(u, t, "need covered by a closer unit")
        for m in res.unmet:
            self._unmet(t, m.demand, m.reason)
            waiting.append(m.demand)
        if self.cfg.get("surge"):
            self._surge(t, waiting)
        self._log(t, "plan", engine=res.engine, demands=len(demands), allocated=len(res.allocations),
                  unmet=len(res.unmet), runtime_ms=res.runtime_ms,
                  fleet_busy=sum(u.status in ("en_route", "on_site") for u in self.units.values()),
                  fleet=sum(u.status != "offline" for u in self.units.values()))

    def _target(self, dists: np.ndarray, inc: Incident, cap: str, kind: str):
        r = REACH_M.get(cap, 150)
        if kind in LAST_LEG_KMH:
            r = max(r, 600)
        cand = self.sh.node_tree.query_ball_point(self.sh.seg_xy[inc.seg], r)
        if not cand:
            cand = [int(self.sh.node_tree.query(self.sh.seg_xy[inc.seg])[1])]
        cand = np.asarray(cand)
        d = dists[cand]
        ok = np.isfinite(d)
        if not ok.any():
            return None, None, 0.0
        walk = np.hypot(*(self.sh.nxy_m[cand] - self.sh.seg_xy[inc.seg]).T)
        spd = LAST_LEG_KMH.get(kind, 4.0) / 3.6
        tot = np.where(ok, d + walk / spd, np.inf)
        i = int(np.argmin(tot))
        return int(cand[i]), float(tot[i]), float(walk[i] / spd)

    def _unmet(self, t: float, d, reason: str) -> None:
        self.unmet.append({**self._ts(t), "policy": self.policy, "demand": d.id, "incident": d.incident_id,
                           "capability": d.capability, "ward": d.ward_id, "severity": d.severity, "reason": reason})

    # ---------------------------------------------------------- movement ---
    def _start(self, u, inc, cap, did, rt, tgt, last, t_free, p, eta_min, engine, t, total=None) -> None:
        u.version += 1
        u.leg_id += 1
        was = u.status
        u.status, u.task, u.incident, u.target, u.last_leg_s = "en_route", did, inc.id, tgt, last
        u.route = [(s, f, self._to(rt, k, tgt)) for k, (s, f) in enumerate(rt)]
        u.depart_t = t_free + (120.0 if was == "available" else 0.0)
        u.planned_s = float(total) if total is not None else eta_min * 60.0
        u.reroutes = 0
        u.invalid = 0
        pmax = float(np.max(p[[s for s, _, _ in u.route]])) if (p is not None and u.route) else None
        if p is not None and u.route:
            for s, _, _ in u.route:
                if p[s] >= 0.05:
                    self.preds.append({**self._ts(t), "policy": self.policy, "unit": u.id, "leg": u.leg_id,
                                       "seg": int(s), "p_blocked": round(float(p[s]), 4), "used": True})
        self.dispatch.append({**self._ts(t), "policy": self.policy, "unit": u.id, "kind": u.kind,
                              "incident": inc.id, "category": inc.kind, "demand": did, "capability": cap,
                              "eta_planned_min": eta_min, "engine": engine, "incident_status": inc.status,
                              "severity": inc.severity, "max_p_on_route": pmax,
                              "why": f"{cap.replace('_', ' ')} for {inc.kind} {inc.id} ({inc.status})"})
        self._led("unit", u.id, "en_route", t, task=did, incident=inc.id)
        self._log(t, "dispatch", unit=u.id, demand=did, eta_min=eta_min, max_p_on_route=pmax)
        self._drive(u, u.depart_t)

    @staticmethod
    def _to(rt, k, tgt) -> int:
        return rt[k + 1][1] if k + 1 < len(rt) else tgt

    def _drive(self, u: UnitState, t_start: float) -> None:
        """Follow u.route through the truth from t_start; schedule what happens next."""
        prof = TRAVEL[u.kind]
        t = t_start
        u.timeline = []
        for s, f, to in u.route:
            step = int(t // (STEP_MIN * 60))
            secs = self.run.true_secs(min(step, self.run.tr.steps - 1), prof)[s]
            if not np.isfinite(secs):
                self._push(t, "blocked", (u.id, u.version, s, f))
                return
            u.timeline.append((s, f, to, t, t + secs))
            t += secs
        self._push(t + u.last_leg_s, "arrive", (u.id, u.version))

    def _position(self, u: UnitState, t: float) -> tuple[int, float]:
        """Where an en-route unit will next be at a junction, and when."""
        for s, f, to, ti, to_t in u.timeline:
            if to_t >= t:
                return (to, to_t) if ti < t else (f, t)
        return (u.timeline[-1][2], t) if u.timeline else (u.node, t)

    def _reroute_check(self, t: float) -> None:
        for u in self.units.values():
            if u.status != "en_route" or not u.route:
                continue
            node, t_free = self._position(u, t)
            remaining = []
            hit = False
            for s, f, to, ti, to_t in u.timeline:
                if ti >= t_free:
                    remaining.append(s)
            if not remaining:
                continue
            cost, p = self._cost_for_unit(t, u)
            bad = [s for s in remaining if not np.isfinite(cost[s])]
            if not bad:
                continue
            s0 = bad[0]
            if s0 in self.known_closed:
                why = self.known_closed[s0]
            elif p is not None and p[s0] >= UNCERTAIN_P:
                why = f"predicted blocked at arrival (p={p[s0]:.2f})"
            else:
                why = "reported closed now (report, patrol or sensor evidence)"
            self._reroute(u, node, t_free, t, why)

    def _reroute(self, u: UnitState, node: int, t_free: float, t: float, why: str) -> None:
        inc = self.incidents.get(u.incident)
        if inc is None:
            return
        cost, p = self._cost_for_unit(t, u)
        dists, pred, key = shortest(cost, "road", node)
        cap = u.task.split(":")[1]
        tgt, total, last = self._target(dists, inc, cap, u.kind)
        u.reroutes += 1
        if tgt is None or u.reroutes > 6:
            self._log(t, "unit.no_route", unit=u.id, demand=u.task, reason=why)
            u.node = node
            self._free(u, t_free, "no passable road to the need (need returns to the pool)")
            return
        rt = path(pred, key, node, tgt) or []
        u.version += 1
        u.route = [(s, f, self._to(rt, k, tgt)) for k, (s, f) in enumerate(rt)]
        u.target, u.last_leg_s = tgt, last
        self._log(t, "unit.rerouted", unit=u.id, demand=u.task, reason=why,
                  new_eta_min=round((t_free - t + total) / 60, 1))
        self._led("unit", u.id, "en_route", t, task=u.task, note=f"rerouted: {why}")
        self._drive(u, t_free)

    def _free(self, u: UnitState, t: float, why: str) -> None:
        self._release_shelter(u, t)
        u.status, u.task, u.incident, u.route, u.timeline = "available", None, None, [], []
        u.version += 1
        self._led("unit", u.id, "available", t, note=why)

    # ------------------------------------------------------------ shelters --
    def _reserve(self, u: UnitState, inc: Incident, t: float) -> bool:
        best, bd = None, 1e18
        for s in self.shelters.values():
            if s["capacity"] - s["occ"] - s["reserved"] >= min(45, max(inc.people, 1)):
                d = math.hypot(*(self.sh.ll_m(s["lng"], s["lat"]) - self.sh.seg_xy[inc.seg]))
                if d < bd:
                    best, bd = s, d
        if best is None:
            return False
        n = min(u.capacity, 45, max(inc.people, 1))
        best["reserved"] += n
        u.shelter = best["id"]
        u.shelter_n = n
        self._led("shelter", best["id"], "reserved", t, capacity=best["capacity"], occupancy=best["occ"],
                  reserved=best["reserved"], unit=u.id, people=n)
        return True

    def _release_shelter(self, u: UnitState, t: float) -> None:
        if u.shelter and u.task and u.task.split(":")[1] == "mass_transport":
            s = self.shelters[u.shelter]
            s["reserved"] = max(0, s["reserved"] - u.shelter_n)
            self._led("shelter", s["id"], "released", t, capacity=s["capacity"], occupancy=s["occ"],
                      reserved=s["reserved"], unit=u.id)
        u.shelter = None

    # -------------------------------------------------------------- events --
    def on_blocked(self, t: float, uid: str, ver: int, seg: int, frm: int) -> None:
        u = self.units[uid]
        if ver != u.version:
            return
        u.invalid += 1
        self.known_closed[seg] = "crew found it blocked (binding)"
        self._log(t, "route.invalid", unit=u.id, demand=u.task, seg=int(seg),
                  reason="road really blocked when the unit got there; crew turns back and reports it")
        self.routes.append(self._route_row(u, t, invalid=True, arrived=False))
        self._reroute(u, frm, t + TURNBACK_S, t, "crew found the road blocked")

    def on_arrive(self, t: float, uid: str, ver: int) -> None:
        u = self.units[uid]
        if ver != u.version:
            return
        inc = self.incidents[u.incident]
        cap = u.task.split(":")[1]
        u.node = u.target
        u.status = "on_site"
        u.version += 1
        if inc.first_on_scene is None:
            inc.first_on_scene = t
        if cap in LIFE_SAFETY and inc.first_life_safety is None:
            inc.first_life_safety = t
        self.routes.append(self._route_row(u, t, invalid=u.invalid > 0, arrived=True))
        self._led("unit", u.id, "on_site", t, task=u.task, incident=inc.id)
        self._log(t, "unit.on_scene", unit=u.id, demand=u.task,
                  response_min=round((t - inc.first_t) / 60, 1))
        service = SERVICE_MIN.get(cap, 30) * 60
        if cap == "field_assessment":
            real = self.sh.truth_real(inc.kind, inc.seg, t / 60, self.run.tr)
            if real:
                self._confirm(inc, t + service, "assessor on scene confirmed it")
            else:
                self._close(inc, t + service, "dismissed", "assessor found nothing there (false or stale report)")
        extra = 0.0
        if cap in ("medical_transport", "mass_transport"):
            dst = self.sh.hospital_node(u.node) if cap == "medical_transport" else self.sh.shelter_node(self.shelters, u.shelter)
            extra = self._secondary_leg(u, t + service, dst, "to hospital" if cap == "medical_transport" else "to shelter")
            service += 600
        self._push(t + service + extra, "done", (u.id, u.version, inc.id, cap))

    def _secondary_leg(self, u: UnitState, t: float, dst: int | None, what: str) -> float:
        if dst is None:
            return 0.0
        from ml.route_eval import drive
        prof = TRAVEL[u.kind]
        cost_now = (lambda known: self._cost_with(t, u, known))
        r = drive(self.run, prof, int(t // 60), u.node, dst, cost_now)
        self.routes.append({**self._ts(t), "policy": self.policy, "unit": u.id, "leg": f"{u.leg_id}b",
                            "what": what, "planned_min": None, "actual_min": round(r["travel_s"] / 60, 1),
                            "invalid": bool(r["invalid"]), "reroutes": int(r["replans"]), "arrived": bool(r["arrived"])})
        if r["arrived"]:
            u.node = dst
        return float(r["travel_s"]) if np.isfinite(r["travel_s"]) else 1800.0

    def _cost_with(self, t, u, known):
        c, _ = self._cost_for_unit(t, u)
        if known:
            c = c.copy()
            c[list(known)] = np.inf
        return c

    def on_done(self, t: float, uid: str, ver: int, inc_id: str, cap: str) -> None:
        u = self.units[uid]
        if ver != u.version:
            return
        inc = self.incidents[inc_id]
        if inc.status == "confirmed" or cap == "field_assessment":
            inc.served[cap] = inc.served.get(cap, 0) + (1 if cap != "field_assessment" else 0)
        if cap == "mass_transport" and u.shelter:
            s = self.shelters[u.shelter]
            n = u.shelter_n
            s["reserved"] = max(0, s["reserved"] - n)
            s["occ"] += n
            if s["occ"] > s["capacity"]:
                self.violations["shelter_overflow"] += 1
            self._led("shelter", s["id"], "arrived", t, capacity=s["capacity"], occupancy=s["occ"],
                      reserved=s["reserved"], unit=u.id, people=n)
            u.shelter = None
        if cap == "debris_clearance":
            self._clear_debris(inc, t)
        if inc.status == "confirmed" and all(inc.served.get(c, 0) >= r for c, r in inc.needs.items()):
            inc.status = "done"
            self._log(t, "incident.done", incident=inc.id, response_min=round(((inc.first_on_scene or t) - inc.first_t) / 60, 1))
        u.status, u.task, u.incident, u.route, u.timeline = "available", None, None, [], []
        u.version += 1
        self._led("unit", u.id, "available", t, note=f"{cap} finished at {inc_id}")

    def _clear_debris(self, inc: Incident, t: float) -> None:
        step = int(t // (STEP_MIN * 60))
        segs = self.sh.seg_tree.query_ball_point(self.sh.seg_xy[inc.seg], 60)
        f = self.run.tr.flags
        f[step:, segs] &= np.uint8(~(F_DEBRIS | F_COLLAPSE | F_LANDSLIDE) & 0xFF)
        self.run._true.clear()
        for s in segs:
            self.known_closed.pop(int(s), None)
            self.known_open.add(int(s))
        self._log(t, "road.cleared_by_crew", incident=inc.id, segments=len(segs))

    def _route_row(self, u: UnitState, t: float, invalid: bool, arrived: bool) -> dict:
        return {**self._ts(t), "policy": self.policy, "unit": u.id, "leg": u.leg_id, "what": u.task,
                "planned_min": round(u.planned_s / 60, 1), "actual_min": round((t - u.depart_t) / 60, 1),
                "invalid": invalid, "reroutes": u.reroutes, "arrived": arrived}

    # --------------------------------------------------------------- surge --
    def _surge(self, t: float, demands) -> None:
        from app.surge.service import CLIMB_AFTER, DESCEND_AFTER, LEVELS, STRAIN_UTIL, SURGE_SHELTER
        live = [u for u in self.units.values() if u.status != "offline"]
        busy = sum(u.status in ("en_route", "on_site") for u in live)
        util = busy / max(len(live), 1)
        cap = sum(s["capacity"] for s in self.shelters.values())
        occ = sum(s["occ"] + s["reserved"] for s in self.shelters.values())
        occ_r = occ / cap if cap else 1.0
        unmet = len(demands)
        ls = sum(1 for d in demands if d.capability in LIFE_SAFETY)
        nxt = min(4, self.level + 1)
        reasons = []
        if nxt == 1 and (util >= STRAIN_UTIL or occ_r >= 0.8 or unmet):
            reasons.append(f"fleet {util:.0%} busy, shelters {occ_r:.0%} full, {unmet} need(s) waiting")
        elif nxt == 2 and unmet:
            reasons.append(f"{unmet} need(s) still waiting after triage")
        elif nxt == 3 and (occ_r >= SURGE_SHELTER or unmet):
            reasons.append(f"shelters {occ_r:.0%} full; {unmet} need(s) waiting after mutual aid was asked")
        elif nxt == 4 and ls:
            reasons.append(f"{ls} life-safety need(s) waiting with mutual aid and surge shelters in play")
        self.up = self.up + 1 if reasons else 0
        if reasons and self.level < 4 and self.up >= CLIMB_AFTER.get(nxt, 2):
            if nxt == 4:
                if not self.decl:
                    self.decl = True
                    self._log(t, "surge.declaration_requested", reason="; ".join(reasons),
                              asks="state declaration; NDRF and Army", note="needs an officer's approval")
                    self._push(t + 20 * 60, "approve", None)
                return
            self.level, self.up = nxt, 0
            self._log(t, "surge.level_changed", level=nxt, name=LEVELS[nxt], reason="; ".join(reasons),
                      authority=AUTHORITY[nxt])
            self._surge_enter(t, nxt, demands)

    def _surge_enter(self, t: float, lvl: int, demands) -> None:
        if lvl == 1:
            self._log(t, "surge.triage", rule="life-safety needs x2, wards with >=10% elderly x1.3")
        if lvl >= 2:
            self._ask_aid(t, lvl, demands)
        if lvl == 3:
            self._open_schools(t)

    def _ask_aid(self, t: float, lvl: int, demands) -> None:
        from app import taxonomy as tx
        short: dict[str, int] = {}
        for d in demands:
            short[d.capability] = short.get(d.capability, 0) + 1
        if lvl >= 3:
            short.setdefault("mass_transport", 1)
        for o in self.sh.offers:
            if o["min_level"] > lvl or o["id"] in self.aid_asked:
                continue
            caps = [c for c in short if tx.cache.kind_can(o["kind"], c)]
            if not caps:
                continue
            self.aid_asked.add(o["id"])
            ok = _hash01(self.name, o["id"]) < o["accept_p"]
            due = t + o["response_minutes"] * 60
            self._log(t, "surge.aid_requested", offer=o["id"], label=o["label"], capability=caps[0],
                      quantity=o["quantity"], eta_min=o["response_minutes"])
            self._push(due, "aid", (o, ok))

    def _aid_arrivals(self, t: float) -> None:
        return

    def on_aid(self, t: float, o: dict, ok: bool) -> None:
        if not ok:
            self._log(t, "surge.aid_declined", offer=o["id"], label=o["label"],
                      reason="no capacity to spare (the next agency is asked)")
            return
        node = int(self.sh.node_tree.query(self.sh.ll_m(o["lng"], o["lat"]))[1])
        made = []
        for k in range(o["quantity"]):
            uid = f"AID-{o['id']}-{k + 1}"
            self.units[uid] = UnitState(uid, o["kind"], f"{o['label']} #{k + 1}",
                                        45 if o["kind"] == "bus" else 6, node, aid=True)
            self._led("unit", uid, "available", t, note=f"mutual aid arrived ({o['label']})")
            made.append(uid)
        self._log(t, "surge.aid_arrived", offer=o["id"], label=o["label"], units=made)

    def on_approve(self, t: float) -> None:
        from app.surge.service import LEVELS
        self.level = 4
        self._log(t, "surge.level_changed", level=4, name=LEVELS[4], authority=AUTHORITY[4],
                  reason="declaration approved by the duty officer (simulated approval, 20 min)")
        self._ask_aid(t, 4, self._demands(t)[0])

    def _open_schools(self, t: float, n: int = 3) -> None:
        full = sorted(self.shelters.values(), key=lambda s: -(s["occ"] + s["reserved"]) / max(s["capacity"], 1))
        anchor = self.sh.ll_m(full[0]["lng"], full[0]["lat"]) if full else np.zeros(2)
        flooded_wards = {i.ward for i in self.incidents.values() if i.kind == "flood" and i.status == "confirmed"}
        cands = [s for s in self.sh.schools if s["id"] not in self.schools_open
                 and self.sh.ward_of_ll(s["lng"], s["lat"]) not in flooded_wards]
        cands.sort(key=lambda s: np.hypot(*(self.sh.ll_m(s["lng"], s["lat"]) - anchor)))
        for s in cands[:n]:
            self.schools_open.add(s["id"])
            sid = f"surge-{s['id']}"
            self.shelters[sid] = dict(s, id=sid, kind="shelter", occ=0, reserved=0)
            self._led("shelter", sid, "opened", t, capacity=s["capacity"], occupancy=0, reserved=0,
                      note=f"school opened as surge shelter: {s['name']}")
            self._log(t, "surge.shelter_opened", shelter=sid, name=s["name"], capacity=s["capacity"],
                      reason="shelters near capacity; this school's ward has no confirmed flooding")

    # ----------------------------------------------------------------- run --
    def go(self) -> None:
        t = self.t0
        while t <= self.t1:
            self._push(t, "plan", None)
            t += PLAN_MIN * 60
        while self.heap:
            t, _, kind, pl = heapq.heappop(self.heap)
            if t > self.t1:
                break
            if kind == "plan":
                self.plan(t)
            elif kind == "blocked":
                self.on_blocked(t, *pl)
            elif kind == "arrive":
                self.on_arrive(t, *pl)
            elif kind == "done":
                self.on_done(t, *pl)
            elif kind == "aid":
                self.on_aid(t, *pl)
            elif kind == "approve":
                self.on_approve(t)

    def summary(self) -> dict:
        inc = list(self.incidents.values())
        real = [i for i in inc if i.truly_real]
        false = [i for i in inc if i.truly_real is False]
        resp = [(i.first_on_scene - i.first_t) / 60 for i in real if i.first_on_scene is not None]
        ls = [(i.first_life_safety - i.first_t) / 60 for i in real
              if i.first_life_safety is not None and any(c in LIFE_SAFETY for c in i.needs)]
        crews_to_false = sum(1 for d in self.dispatch
                             if d["capability"] != "field_assessment"
                             and self.incidents[d["incident"]].truly_real is False)
        legs = pd.DataFrame(self.routes)
        prim = legs[legs["planned_min"].notna()] if len(legs) else legs
        unserved = [i for i in real if i.status not in ("done",) and i.first_on_scene is None]
        und = pd.DataFrame(self.unmet)
        sh_peak = max(((s["occ"]) / s["capacity"] for s in self.shelters.values() if s["capacity"]), default=0)
        return {
            "policy": self.policy,
            "incidents": len(inc), "incidents_real": len(real), "incidents_false_or_stale": len(false),
            "confirmed": sum(i.status in ("confirmed", "done") for i in inc),
            "dismissed_by_assessor": sum(i.status == "dismissed" for i in inc),
            "expired": sum(i.status == "expired" for i in inc),
            "dispatches": len(self.dispatch), "crews_sent_to_false_reports": crews_to_false,
            "legs_driven": int(len(legs)),
            "invalid_routes": int(legs["invalid"].sum()) if len(legs) else 0,
            "invalid_route_rate": round(float(prim["invalid"].mean()), 4) if len(prim) else None,
            "reroutes_before_reaching_a_closure": sum(1 for e in self.events if e["kind"] == "unit.rerouted"
                                                      and "crew found" not in e.get("reason", "")),
            "response_p50_min": round(float(np.median(resp)), 1) if resp else None,
            "response_p90_min": round(float(np.quantile(resp, 0.9)), 1) if resp else None,
            "life_safety_response_p50_min": round(float(np.median(ls)), 1) if ls else None,
            "real_incidents_reached": len(resp),
            "real_incidents_unreached_at_end": len(unserved),
            "unmet_need_decisions": int(len(und)),
            "unmet_by_reason": und["reason"].str.slice(0, 70).value_counts().to_dict() if len(und) else {},
            "people_sheltered": int(sum(s["occ"] for s in self.shelters.values())),
            "shelter_peak_occupancy": round(float(sh_peak), 3),
            "surge_max_level": self.level,
            "aid_units_arrived": sum(1 for u in self.units.values() if u.aid),
            "checks": dict(self.violations),
        }

    def write(self, d: Path) -> None:
        d.mkdir(parents=True, exist_ok=True)
        with open(d / f"events_{self.policy}.jsonl", "w") as f:
            for e in self.events:
                f.write(json.dumps(e, default=_jd) + "\n")
        for name, rows in (("dispatch_log", self.dispatch), ("route_checks", self.routes),
                           ("resource_ledger", self.ledger), ("unmet", self.unmet)):
            pd.DataFrame(rows).to_csv(d / f"{name}_{self.policy}.csv", index=False)
        if self.preds:
            P = pd.DataFrame(self.preds)
            P["truth_blocked_at_arrival"] = self._label_preds(P)
            P.to_parquet(d / f"predictions_{self.policy}.parquet", index=False)


    def _label_preds(self, P: pd.DataFrame) -> np.ndarray:
        """Truth for each prediction used: was that road blocked for the unit when it
        would have been there? (Arrival approximated by the decision time + the
        segment's position on the leg, using the unit class's own rules.)"""
        out = np.zeros(len(P), dtype=np.int8)
        units = {u.id: u for u in self.units.values()}
        for i, r in enumerate(P.itertuples()):
            u = units.get(r.unit)
            prof = TRAVEL[u.kind] if u else "ambulance"
            t = (r.t_min * 60) + self.t0
            step = min(int(t // (STEP_MIN * 60)) + 1, self.run.tr.steps - 1)
            out[i] = int(not np.isfinite(self.run.true_secs(step, prof)[r.seg]))
        return out


def current_holder(current: dict, did: str):
    for k, v in current.items():
        if v == did:
            return k
    return None


def _jd(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    return str(o)


AUTHORITY = {0: "local (ward office / PCMC control room)",
             1: "municipal (PCMC Disaster Management Cell)",
             2: "district (Collector, DDMA Pune: mutual aid)",
             3: "state (SDMA / SDRF, Relief & Rehabilitation)",
             4: "national (NDMA / NDRF, Armed Forces via MHA)"}


# ------------------------------------------------------------------ shared --
class Shared:
    """Everything identical across policies: window, evidence, beliefs, fleet."""

    def __init__(self, name: str, cfg: dict, model_dir: Path | None):
        from app import taxonomy as tx
        from app.nav.passability import LightEnsemble
        snap = json.load(open(SNAPSHOT))
        tx.seed_for_tests(kind_capabilities=snap["kind_capabilities"])
        self.cfg = cfg
        self.st = load_static()
        self.g = static_arrays()
        u, v, ow, N, nxy = graph()
        self.nxy = nxy
        self.lat0 = float(np.mean(nxy[:, 1]))
        self.nxy_m = self._m(nxy[:, 0], nxy[:, 1])
        self.node_tree = cKDTree(self.nxy_m)
        self.seg_xy = self._m(self.st["lon"].to_numpy(), self.st["lat"].to_numpy())
        self.seg_tree = cKDTree(self.seg_xy)
        self.cand = np.flatnonzero(self.g["drivable"])
        self.cand_xy = self.seg_xy[self.cand]
        self.run0 = Run(cfg["storm"], cfg["k"])
        self.start = pd.Timestamp(self.run0.ev.start)
        if self.start.tzinfo is not None:
            self.start = self.start.tz_convert("UTC").tz_localize(None)
        self.ens = LightEnsemble(model_dir) if model_dir else None
        self.model = model_dir.name if model_dir else None
        self.t0, self.t1 = self._window()
        self.injection = None
        R = self.run0.ev.reports.copy()
        if cfg.get("inject_closure_after_min"):
            self.injection = self._pick_bridge()
        if self.injection:
            seg = self.injection["seg"]
            note = pd.DataFrame([{"source": "official", "kind": "closure",
                                  "t_obs_min": self.injection["t_s"] / 60 + 10, "x": self.st["x"].iloc[seg],
                                  "y": self.st["y"].iloc[seg], "seg_rep": seg, "depth_class": -1,
                                  "depth_est_m": np.nan, "blocked_est": 1.0, "reliability": 0.99}])
            R = pd.concat([R, note], ignore_index=True).sort_values("t_obs_min", kind="stable")
            self.run0.ev.reports = R
        self.reports = R.reset_index(drop=True)
        self.rep_xy = np.c_[self.reports["x"].to_numpy(), self.reports["y"].to_numpy()]
        self.sens = self.run0.ev.sensors
        self.offers = snap["aid_offers"]
        self.schools = snap["schools"]
        self.elderly = snap.get("ward_elderly_share", {})
        keep = cfg.get("fleet_keep", 1.0)
        units = []
        for k, uu in enumerate(snap["units"]):
            st_ = "available"
            if keep < 1.0 and _hash01(name, uu["id"]) > keep:
                st_ = "offline"
            units.append(dict(uu, node=int(self.node_tree.query(self.ll_m(uu["lng"], uu["lat"]))[1]), status=st_))
        self.units = [x for x in units if x["status"] != "offline"]
        self.offline_units = [x["id"] for x in units if x["status"] == "offline"]
        scale = cfg.get("shelter_scale", 1.0)
        self.shelters = [dict(s, capacity=int(s["capacity"] * scale)) for s in snap["shelters"]]
        self.hospitals = [dict(h, node=int(self.node_tree.query(self.ll_m(h["lng"], h["lat"]))[1]))
                          for h in snap["hospitals"]]
        self._F: dict = {}
        self._P: dict = {}
        self._B: dict = {}
        self._free: dict = {}

    def _m(self, lon, lat) -> np.ndarray:
        return np.c_[(np.asarray(lon) - 73.77) * 111_320 * math.cos(math.radians(18.63)),
                     (np.asarray(lat) - 18.63) * 110_574]

    def ll_m(self, lon, lat) -> np.ndarray:
        return self._m([lon], [lat])[0]

    def seg_ll(self, seg: int) -> tuple[float, float]:
        return float(self.st["lon"].iloc[seg]), float(self.st["lat"].iloc[seg])

    def ward_of_ll(self, lon, lat) -> str:
        return str(self.st["ward_id"].iloc[int(self.seg_tree.query(self.ll_m(lon, lat))[1])])

    def hospital_node(self, node: int) -> int:
        xy = self.nxy_m[node]
        h = min(self.hospitals, key=lambda h: np.hypot(*(self.nxy_m[h["node"]] - xy)))
        return h["node"]

    def shelter_node(self, shelters: dict, sid: str | None) -> int | None:
        if not sid:
            return None
        s = shelters[sid]
        return int(self.node_tree.query(self.ll_m(s["lng"], s["lat"]))[1])

    def _window(self) -> tuple[float, float]:
        tr = self.run0.tr
        hours = self.cfg["hours"]
        steps = int(hours * 60 // STEP_MIN)
        a = self.cfg["anchor"]
        if a == "events":
            R = self.run0.ev.reports
            c = R[(R["source"] == "citizen") & R["kind"].isin(list(INCIDENT_KINDS))]
            counts = np.bincount((c["t_obs_min"] // STEP_MIN).astype(int), minlength=tr.steps)
            score = np.convolve(counts, np.ones(steps), "valid")
            s0 = int(np.argmax(score))
        elif a == "rise":
            stage = tr.stage.max(1)
            rise = np.r_[0, np.diff(stage)]
            peak = int(np.argmax(np.convolve(rise, np.ones(8), "same")))
            s0 = max(0, peak - steps // 2)
        else:
            wet = ((tr.depth >= 0.3) & self.g["drivable"][None, :]).sum(1)
            peak = int(np.argmax(wet))
            s0 = max(0, peak - int(steps * 0.6))
        s0 = min(s0, tr.steps - steps - 1)
        return float(s0 * STEP_MIN * 60), float((s0 + steps) * STEP_MIN * 60)

    def _pick_bridge(self) -> dict | None:
        """The river bridge most likely to be on dispatch routes: the drivable
        river bridge closest to the district's centre of demand that is still
        open in the truth at injection time (closing an already-closed bridge
        tests nothing)."""
        t = self.t0 + self.cfg["inject_closure_after_min"] * 60
        step = int(t // (STEP_MIN * 60))
        st = self.st
        br = np.flatnonzero(st["river_bridge"].to_numpy() & self.g["drivable"] & (st["cls"].isin(["main", "minor"]).to_numpy()))
        open_ = [s for s in br if not (self.run0.tr.flags[step, s] & F_BRIDGE) and self.run0.tr.depth[step, s] < 0.3]
        if not open_:
            return None
        centre = self.ll_m(73.785, 18.63)
        seg = int(min(open_, key=lambda s: np.hypot(*(self.seg_xy[s] - centre))))
        return {"seg": seg, "t_s": t, "lon": self.seg_ll(seg)[0], "lat": self.seg_ll(seg)[1]}

    # beliefs, cached per decision time (identical across policies)
    def _F_for(self, t: float, code: str) -> pd.DataFrame:
        key = (int(t // (BELIEF_MIN * 60)), code)
        if key not in self._F:
            lim = PROFILES[code].depth_limit_m
            reps = np.tile(self.cand, 3)
            hz = np.repeat([30, 60, 90], len(self.cand))
            self.run0.ev.fc_noise = float(np.exp(np.random.default_rng(int(t)).normal(0, 0.4)))
            F = build(self.run0.ev, int(t // 60), reps, hz, np.full(len(reps), PROFILE_CODES[code]),
                      np.full(len(reps), lim))
            self._F = {k: v for k, v in self._F.items() if k[0] >= key[0] - 1}
            self._F[key] = F
        return self._F[key]

    @staticmethod
    def code_of(prof: str) -> str:
        return "ambulance" if PROFILES[prof].depth_limit_m <= 0.3 else "fire_engine"

    def base_cost(self, t: float, prof: str) -> np.ndarray:
        key = (int(t // (BELIEF_MIN * 60)), prof)
        if key not in self._B:
            F = self._F_for(t, self.code_of(prof))
            Fn = F.iloc[: len(self.cand)]
            S = known_state(self.run0, int(t // (STEP_MIN * 60)), Fn, self.cand)
            self._B = {k: v for k, v in self._B.items() if k[0] >= key[0] - 1}
            self._B[key] = seconds(PROFILES[prof], S)
        return self._B[key]

    def free_cost(self, prof: str) -> np.ndarray:
        if prof not in self._free:
            n = len(self.st)
            z = np.zeros(n, bool)
            S = {"depth": np.zeros(n, np.float32), "flowing": z, "debris": z, "wire": z, "collapse": z,
                 "landslide": z, "bridge_closed": z, "fire_d": np.full(n, np.inf, np.float32),
                 "gas_d": np.full(n, np.inf, np.float32), "jam": np.zeros(n, np.float32),
                 "crowd": np.zeros(n, np.float32), "rain": np.zeros(n, np.float32), "confirmed_closed": z}
            self._free[prof] = seconds(PROFILES[prof], S)
        return self._free[prof]

    def p_for(self, t: float, prof: str, origin_ll) -> tuple[np.ndarray, np.ndarray]:
        """P(blocked) per segment at the horizon a unit starting at origin would reach it."""
        code = self.code_of(prof)
        key = (int(t // (BELIEF_MIN * 60)), code)
        if key not in self._P:
            F = self._F_for(t, code)
            p, sd = self.ens.predict(F)
            self._P = {k: v for k, v in self._P.items() if k[0] >= key[0] - 1}
            self._P[key] = (p.reshape(3, -1), sd.reshape(3, -1))
        pm, sm = self._P[key]
        o = self.ll_m(*origin_ll)
        off_min = np.hypot(*(self.cand_xy - o).T) / 1000 / 25 * 60 * 1.4
        h = np.clip(np.digitize(off_min, [45, 75]), 0, 2)
        n = len(self.st)
        p = np.zeros(n)
        sd = np.zeros(n)
        idx = np.arange(len(self.cand))
        p[self.cand] = pm[h, idx]
        sd[self.cand] = sm[h, idx]
        return p, sd

    # evidence lookups for the trust rules
    def sensor_near(self, x: float, y: float, t: float):
        S = self.sens[(self.sens["source"] == "sensor") & (self.sens["t_min"] <= t / 60)]
        if S.empty:
            return None
        last = S.groupby("seg").tail(1)
        d = np.hypot(self.st["x"].to_numpy()[last["seg"]] - x, self.st["y"].to_numpy()[last["seg"]] - y)
        i = int(np.argmin(d))
        if d[i] > 300:
            return None
        return float(last["value_m"].iloc[i]), float(t / 60 - last["t_min"].iloc[i]), float(d[i])

    def patrol_near(self, x: float, y: float, t: float):
        R = self.reports
        m = (R["source"] == "patrol") & (R["t_obs_min"] <= t / 60) & (R["t_obs_min"] > t / 60 - 60)
        P = R[m]
        if P.empty:
            return None
        d = np.hypot(P["x"].to_numpy() - x, P["y"].to_numpy() - y)
        near = P[d <= 60]
        if near.empty:
            return None
        r = near.iloc[-1]
        return {"blocked": int(r["blocked_est"]) if pd.notna(r["blocked_est"]) else None,
                "depth": float(r["depth_est_m"]) if pd.notna(r["depth_est_m"]) else None,
                "age": float(t / 60 - r["t_obs_min"])}

    def truth_real(self, kind: str, seg: int, t_min: float, tr=None) -> bool:
        """Scoring and assessor reveal only: was there really something at this place then?"""
        tr = tr or self.run0.tr
        step = min(int(t_min // STEP_MIN), tr.steps - 1)
        near = self.seg_tree.query_ball_point(self.seg_xy[seg], 150)
        if kind == "flood":
            lo = max(0, step - 1)
            return bool((tr.depth[lo:step + 1, near] >= 0.15).any())
        a = tr.active(step, TRUTH_KIND.get(kind, (kind,)))
        if a.empty:
            a = tr.active(max(0, step - 2), TRUTH_KIND.get(kind, (kind,)))
        if a.empty:
            return False
        d = np.hypot(a["x"].to_numpy() - self.st["x"].iloc[seg], a["y"].to_numpy() - self.st["y"].iloc[seg])
        return bool((d <= 200).any())


# --------------------------------------------------------------------- main --
def run_scenario(name: str, model_dir: Path | None, only_policies: list[str] | None = None) -> dict:
    cfg = SCENARIOS[name]
    sh = Shared(name, cfg, model_dir)
    d = OUT / name
    prev = json.load(open(d / "summary.json")) if (d / "summary.json").exists() else {}
    out = {"scenario": name, "title": cfg["title"], "story": cfg["story"],
           "data": {"storm_id": cfg["storm"], "realization": cfg["k"], "split": "test",
                    "inputs": "REPLAY: historical ERA5 rain and GloFAS river discharge for this window; roads, "
                              "hazards, people and reports simulated (ml.sim). Not live data.",
                    "window_start_replay": (sh.start + pd.Timedelta(seconds=sh.t0)).isoformat() + "Z",
                    "window_end_replay": (sh.start + pd.Timedelta(seconds=sh.t1)).isoformat() + "Z",
                    "hours": cfg["hours"], "plan_every_min": PLAN_MIN},
           "model": sh.model, "fleet_on_shift": len(sh.units), "fleet_off_shift": sh.offline_units,
           "shelter_places": sum(s["capacity"] for s in sh.shelters),
           "injected": ({"bridge_closure": {**sh.injection,
                                             "t_replay": (sh.start + pd.Timedelta(seconds=sh.injection["t_s"])).isoformat() + "Z",
                                             "notice_after_min": 10}} if sh.injection else None),
           "policies": dict(prev.get("policies", {}))}
    for pol in cfg["policies"]:
        if only_policies and pol not in only_policies:
            continue
        if pol in ("predicted", "naive_trust") and sh.ens is None:
            continue
        rp = Replay(name, cfg, pol, sh)
        rp.go()
        rp.write(d)
        s = rp.summary()
        if rp.preds:
            P = pd.read_parquet(d / f"predictions_{pol}.parquet")
            if P["truth_blocked_at_arrival"].nunique() > 1:
                from sklearn.metrics import roc_auc_score
                s["route_predictions_auc"] = round(float(roc_auc_score(P["truth_blocked_at_arrival"], P["p_blocked"])), 3)
            s["route_predictions"] = int(len(P))
            s["route_predictions_positive"] = int(P["truth_blocked_at_arrival"].sum())
        out["policies"][pol] = s
        print(name, pol, json.dumps({k: s[k] for k in ("dispatches", "invalid_routes", "response_p50_min",
                                                        "real_incidents_unreached_at_end", "checks")}), flush=True)
    d.mkdir(parents=True, exist_ok=True)
    json.dump(out, open(d / "summary.json", "w"), indent=1, default=_jd)
    return out


def main() -> None:
    from ml.route_eval import latest_model
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--policies", nargs="*", default=None, help="run only these policies (results merge)")
    a = ap.parse_args()
    reg = json.load(open(PROCESSED.parent.parent / "models" / "registry.json"))
    champ = reg.get("passability", {}).get("champion")
    md = Path(a.model) if a.model else (PROCESSED.parent.parent / "models" / "passability" / champ if champ else
                                        Path(latest_model()))
    names = a.only or list(SCENARIOS)
    res = {}
    for n in names:
        res[n] = run_scenario(n, md, a.policies)
    allp = OUT / "summary.json"
    old = json.load(open(allp)) if allp.exists() else {}
    old.update(res)
    json.dump(old, open(allp, "w"), indent=1, default=_jd)


if __name__ == "__main__":
    main()
