"""Evidence fusion: how sure are we that an incident is real, from every node that can see it.

The old rubric scored each report on its own, like a single phone call, and
punished a source for reporting often. That is wrong twice in a disaster:
frightened people call again and again, and a sensor network (a drone swarm, a
camera grid, LoRa nodes) legitimately reports many places within minutes. What
should make the system sure is *independent agreement*: a citizen, a camera
and a drone all saying the same thing about the same place.

So belief is computed per incident, from evidence of every kind, in log-odds
(each item adds or subtracts; the order does not matter; it is explainable line
by line):

    belief = prior(category) + sum over modalities [ diminishing sum over witnesses ]

* **Evidence item**: one observation (a report, a camera frame, a drone
  situation report, a sensor reading, a crew update, an official notice), with
  a modality, a witness (who/what observed it), a reliability r (how often this
  witness is right, learned from outcomes, class prior until then), its own
  confidence c (VLM / detector / drone confidence, classification confidence),
  a polarity (+ supports, - contradicts: "water has receded", a drone over an
  empty dry street), an age and a distance.
  Weight = polarity x c x logit(r) x decay(age) x near(distance).
* **Witness**: repeats from the same witness count once, plus a little for
  persistence (capped). A scared person calling ten times is one witness, and
  the repeats are reported as a *distress signal*, never as spam.
* **Modality**: several witnesses of the same kind add with diminishing
  weight (1, 1/1.45, 1/1.9, ...). Human channels alone are capped: a crowd
  can make an incident *likely* (enough to dispatch), but *confirmed* needs a
  second, independent kind of evidence or a trusted one (crew, official).
  That is the defence against a coordinated burst of fake accounts, without
  punishing honest repeat callers.
* **Contradiction** works the same way with a negative sign, so the system can
  also become sure that something is *not* there.
* **Decay**: evidence fades (half-life by category), so an old report does not
  keep a stale incident "confirmed".

What belief controls (`gate`): at or above the dispatch floor (lower for
life-safety categories, because not sending a boat drowns somebody) the planner
covers every need; below it the incident gets *verification first*: one
field-assessment need, which the agent graph's aerial node turns into a drone
"assess" request and the planner into the nearest assessor. The drone's report
is evidence too, so the loop closes by itself: report -> unsure -> drone looks
-> belief moves -> dispatch (or stand down).

Deterministic and model-free on purpose: an officer can read every line of why.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

# ------------------------------------------------------------------ constants --
#: How often a witness of this kind is right when it says something is there,
#: before we have its own track record. Chain of custody, not honesty.
CLASS_RELIABILITY = {
    "field_crew": 0.95,   # a responder on scene
    "official": 0.95,     # agency / authority notice
    "drone": 0.88,        # aircraft looking at the place (scaled by its own report confidence)
    "sensor": 0.85,       # LoRa field node, water-level sensor
    "camera": 0.80,       # edge-AI camera + VLM (scaled by detector / VLM confidence)
    "satellite": 0.80,    # SAR / optical product (scaled by product confidence)
    "citizen": 0.68,      # a person, app or phone
    "mesh": 0.66,         # a person over the offline mesh, signed
    "mesh_unsigned": 0.52,
}
HUMAN = {"citizen", "mesh", "mesh_unsigned"}
#: Prior belief that a newly reported incident of a category is real.
DEFAULT_PRIOR = 0.35
#: Repeats from one witness: a little more each time, never more than this (logit units).
REPEAT_STEP, REPEAT_CAP = 0.12, 0.30
#: k-th independent witness of the same kind weighs 1 / (1 + DIMINISH * (k-1)).
DIMINISH = 0.45
#: Human-only evidence can reach about 0.83 from the default prior: likely (dispatch), never confirmed.
HUMAN_CAP = 2.2
#: Evidence older than this many half-lives is ignored.
MAX_HALF_LIVES = 4.0
HALF_LIFE_MIN = {"fire": 45.0, "smoke": 45.0, "person_stranded": 90.0, "flooded_road": 120.0,
                 "waterlogging": 120.0, "structural_damage": 240.0}
DEFAULT_HALF_LIFE_MIN = 90.0
#: Distance fall-off for nearby (not merged) evidence, metres.
NEAR_M = 200.0
#: Belief thresholds.
CONFIRMED, LIKELY, DOUBTFUL = 0.85, 0.60, 0.25
GATE_LIFE_SAFETY, GATE_OTHER = 0.30, 0.55
WEIGHT_CAP = 3.5          # one item can never decide alone beyond this (logit)


def logit(p: float) -> float:
    p = min(0.995, max(0.005, p))
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


@dataclass(slots=True)
class Evidence:
    modality: str                 # key of CLASS_RELIABILITY
    witness: str                  # who observed it (reporter key, device, node, drone fleet)
    polarity: int = 1             # +1 supports, -1 contradicts
    confidence: float = 1.0       # the source's own confidence 0..1
    reliability: float | None = None   # learned reliability of this witness (None: class prior)
    age_min: float = 0.0
    distance_m: float = 0.0       # 0 for evidence attached to the incident
    note: str = ""
    ref: str | None = None        # report id / event id / node id, for the trail
    half_life_min: float | None = None   # own decay (a satellite pass is days apart, not minutes)

    def weight(self, half_life_min: float) -> float:
        half_life_min = self.half_life_min or half_life_min
        prior_r = CLASS_RELIABILITY.get(self.modality, 0.6)
        r = prior_r if self.reliability is None else 0.5 * prior_r + 0.5 * float(self.reliability)
        c = min(1.0, max(0.0, float(self.confidence)))
        decay = 0.5 ** (max(0.0, self.age_min) / half_life_min)
        if self.age_min > MAX_HALF_LIVES * half_life_min:
            decay = 0.0
        near = math.exp(-max(0.0, self.distance_m) / NEAR_M) if self.distance_m else 1.0
        w = self.polarity * c * max(0.0, logit(r)) * decay * near
        return max(-WEIGHT_CAP, min(WEIGHT_CAP, w))


@dataclass
class Belief:
    p: float
    status: str                   # confirmed | likely | uncertain | doubtful
    dispatch: bool                # may the planner commit units to every need
    prior: float
    by_modality: dict[str, float] = field(default_factory=dict)
    witnesses: list[dict] = field(default_factory=list)
    distress: list[dict] = field(default_factory=list)   # witnesses repeating themselves
    reasons: list[str] = field(default_factory=list)
    next_step: str | None = None
    modalities_seen: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"p": round(self.p, 4), "status": self.status, "dispatch": self.dispatch, "prior": self.prior,
                "byModality": {k: round(v, 3) for k, v in self.by_modality.items()},
                "witnesses": self.witnesses, "distress": self.distress, "reasons": self.reasons,
                "nextStep": self.next_step, "modalities": self.modalities_seen}


def status_of(p: float) -> str:
    return "confirmed" if p >= CONFIRMED else "likely" if p >= LIKELY else "doubtful" if p < DOUBTFUL else "uncertain"


def gate(p: float, life_safety: bool) -> bool:
    return p >= (GATE_LIFE_SAFETY if life_safety else GATE_OTHER)


def fuse(items: Iterable[Evidence], *, category: str = "", life_safety: bool = False,
         prior: float = DEFAULT_PRIOR, can_fly: bool = False, cameras_near: bool = False) -> Belief:
    """Pure: evidence in, belief out. Same inputs always give the same answer."""
    hl = HALF_LIFE_MIN.get(category, DEFAULT_HALF_LIFE_MIN)
    # 1. witness: strongest observation + capped persistence, separately for support and contradiction
    by_w: dict[tuple[str, str, int], list[tuple[float, Evidence]]] = {}
    for e in items:
        w = e.weight(hl)
        if w == 0:
            continue
        by_w.setdefault((e.modality, e.witness, 1 if w > 0 else -1), []).append((w, e))
    witnesses = []
    distress = []
    for (mod, wit, sign), obs in by_w.items():
        obs.sort(key=lambda t: -abs(t[0]))
        top = abs(obs[0][0])
        extra = min(REPEAT_CAP, REPEAT_STEP * (len(obs) - 1))
        witnesses.append({"modality": mod, "witness": wit, "sign": sign, "weight": sign * (top + extra), "count": len(obs),
                          "note": obs[0][1].note[:160], "ref": obs[0][1].ref})
        if sign > 0 and len(obs) >= 3 and mod in HUMAN:
            distress.append({"witness": wit, "reports": len(obs),
                             "meaning": "the same person keeps reporting: urgency, counted once as evidence"})
    # 2. modality: diminishing sum of independent witnesses; human-only cap
    by_mod: dict[str, float] = {}
    for sign in (1, -1):
        for mod in {w["modality"] for w in witnesses if w["sign"] == sign}:
            ws = sorted((abs(w["weight"]) for w in witnesses if w["modality"] == mod and w["sign"] == sign), reverse=True)
            total = sum(v / (1 + DIMINISH * k) for k, v in enumerate(ws))
            by_mod[mod] = by_mod.get(mod, 0.0) + sign * total
    support_human = sum(v for m, v in by_mod.items() if m in HUMAN and v > 0)
    capped = False
    if support_human > HUMAN_CAP:
        scale = HUMAN_CAP / support_human
        for m in list(by_mod):
            if m in HUMAN and by_mod[m] > 0:
                by_mod[m] *= scale
        capped = True
    x = logit(prior) + sum(by_mod.values())
    p = sigmoid(x)
    st = status_of(p)
    reasons = []
    pos = sorted(((m, v) for m, v in by_mod.items() if v > 0), key=lambda t: -t[1])
    neg = sorted(((m, v) for m, v in by_mod.items() if v < 0), key=lambda t: t[1])
    seen = [m for m, _ in pos]
    if pos:
        reasons.append("Supported by " + ", ".join(f"{m.replace('_', ' ')} ({sum(1 for w in witnesses if w['modality'] == m and w['sign'] > 0)})"
                                                  for m, _ in pos) + ".")
    if neg:
        reasons.append("Contradicted by " + ", ".join(m.replace("_", " ") for m, _ in neg) + ".")
    if len({m for m, _ in pos} - HUMAN) >= 1 and any(m in HUMAN for m, _ in pos):
        reasons.append("People and an independent sensor agree.")
    if capped:
        reasons.append("Many people report it, but no independent sensor has seen it yet: likely, not confirmed.")
    if distress:
        reasons.append(f"{len(distress)} reporter(s) keep calling about it: treated as urgency, not as more proof.")
    dispatch = gate(p, life_safety)
    nxt = None
    if st != "confirmed":
        if can_fly and "drone" not in seen:
            nxt = "send a drone to look"
        elif cameras_near and "camera" not in seen:
            nxt = "check the nearest edge camera"
        elif "field_crew" not in seen:
            nxt = "ask the nearest crew or patrol to confirm"
    if not dispatch:
        reasons.append(f"Belief {p:.0%} is below the dispatch floor ({GATE_LIFE_SAFETY if life_safety else GATE_OTHER:.0%}): "
                       "verify first" + (f" ({nxt})" if nxt else "") + ".")
    witnesses.sort(key=lambda w: -abs(w["weight"]))
    return Belief(p=p, status=st, dispatch=dispatch, prior=prior, by_modality=by_mod, witnesses=witnesses[:30],
                  distress=distress, reasons=reasons, next_step=nxt, modalities_seen=seen)


# --------------------------------------------------------------- classifying --
CLEAR_WORDS = ("all clear", "cleared", "receded", "no longer", "false alarm", "nothing here", "water gone",
               "road open", "fire out", "fire is out", "रास्ता खुला", "पाणी ओसरले")


def modality_of(source: str | None, reporter_key: str | None, *, lora_nodes: set[str] | None = None) -> str:
    s, k = (source or "").lower(), reporter_key or ""
    if s == "field":
        return "field_crew"
    if s == "agency":
        return "official"
    if k.startswith("device:sensor:"):
        node = k.split(":", 2)[-1]
        if "drone" in node:
            return "drone"
        if lora_nodes and node in lora_nodes:
            return "sensor"
        return "camera"
    if s == "mesh":
        return "mesh"
    if s == "mesh_unsigned":
        return "mesh_unsigned"
    if s == "sensor":
        return "sensor"
    return "citizen"


def polarity_of(note: str | None) -> int:
    t = (note or "").lower()
    return -1 if any(w in t for w in CLEAR_WORDS) else 1


# ------------------------------------------------------------------ gathering --
async def gather(conn: Any, incident_id: str) -> tuple[dict | None, list[Evidence], dict]:
    """Every observation that bears on one incident, from the database and the live drone link."""
    inc = await conn.fetchrow(
        """
        select i.id::text id, i.category, i.status::text status, i.created_at,
               extensions.ST_X(i.location::extensions.geometry) lng, extensions.ST_Y(i.location::extensions.geometry) lat
          from incidents i where i.id = $1::uuid
        """, incident_id)
    if inc is None:
        return None, [], {}
    inc = dict(inc)
    now = time.time()
    ev: list[Evidence] = []
    ctx: dict = {"can_fly": False, "cameras_near": False}
    lora = {r["id"] for r in await conn.fetch("select id from sensor_nodes")} if await _has(conn, "sensor_nodes") else set()

    # 1. reports on the incident (people, mesh, cameras, drones, LoRa escalations: all arrive as reports)
    rows = await conn.fetch(
        """
        select r.id::text id, r.source, r.reporter_key, r.device_id, r.note, r.created_at,
               r.classification_confidence, r.verification_status
          from citizen_reports r
         where r.incident_id = $1::uuid and r.verification_status <> 'rejected'
        """, incident_id)
    learned = await _learned_reliability(conn, {r["reporter_key"] for r in rows if r["reporter_key"]})
    for r in rows:
        mod = modality_of(r["source"], r["reporter_key"], lora_nodes=lora)
        conf = r["classification_confidence"]
        ev.append(Evidence(
            modality=mod, witness=r["reporter_key"] or r["device_id"] or r["id"], polarity=polarity_of(r["note"]),
            confidence=float(conf) if conf is not None and mod in ("camera", "sensor") else 1.0,
            reliability=learned.get(r["reporter_key"]),
            age_min=(now - r["created_at"].timestamp()) / 60.0, note=r["note"] or "", ref=r["id"]))

    if inc.get("lng") is not None:
        # 2. edge cameras near it: a hazard frame supports, an ordinary frame of the same spot mildly contradicts
        frames = await conn.fetch(
            """
            select id, subject_id, occurred_at, payload from events
             where kind = 'camera.frame' and occurred_at > now() - interval '45 minutes'
             order by id desc limit 200
            """)
        for f in frames:
            p = f["payload"] if isinstance(f["payload"], dict) else {}
            if p.get("lat") is None or p.get("lon") is None:
                continue
            d = _m(inc["lng"], inc["lat"], float(p["lon"]), float(p["lat"]))
            if d > 400:
                continue
            ctx["cameras_near"] = True
            if p.get("incidentId") == incident_id:
                continue                      # already counted as a report on this incident
            hazard = bool(p.get("kind"))
            ev.append(Evidence(modality="camera", witness=f"camera:{f['subject_id']}", polarity=1 if hazard else -1,
                               confidence=0.75 if hazard else 0.35, age_min=(now - f["occurred_at"].timestamp()) / 60.0,
                               distance_m=d, note=str(p.get("caption") or "")[:200], ref=f"event:{f['id']}"))
        # 3. crews and officials on this incident
        crew = await conn.fetch(
            """
            select id, kind, actor, occurred_at, payload from events
             where subject_type = 'incident' and subject_id = $1
               and (kind like 'field.%' or kind like 'agency.%' or kind = 'incident.resolved')
               and kind not in ('field.drone_need', 'field.drone_situation')
             order by id desc limit 40
            """, incident_id)
        for c in crew:
            p = c["payload"] if isinstance(c["payload"], dict) else {}
            k = c["kind"]
            text = " ".join(str(p.get(x) or "") for x in ("note", "reason", "outcome", "text"))
            if k == "incident.resolved" and str(p.get("outcome") or "").lower() in ("false", "false_alarm", "dismissed"):
                pol = -1
            elif k in ("field.on_site", "field.confirmed", "field.situation", "field.report") or k.startswith("agency.replied"):
                pol = polarity_of(text)
            else:
                continue
            ev.append(Evidence(modality="field_crew" if k.startswith("field.") else "official", witness=c["actor"] or k,
                               polarity=pol, age_min=(now - c["occurred_at"].timestamp()) / 60.0, note=text[:160],
                               ref=f"event:{c['id']}"))
        # 4. drones: the swarm's latest situation report on the same place
        ev += _drone_evidence(inc, incident_id, now, ctx)
    return inc, ev, ctx


#: A witness's own track record counts once this many of its reports have been ruled on.
MIN_JUDGED = 3


async def _learned_reliability(conn: Any, keys: set[str]) -> dict[str, float]:
    """How often each witness was right, from outcomes a person or crew ruled on (confirmed / false) only.

    Not from automatic statuses: counting every quarantined report as "wrong" taught the old view that the
    drone swarm was almost always wrong, purely because the old rubric quarantined it. Laplace-smoothed.
    """
    if not keys:
        return {}
    rows = await conn.fetch(
        """
        select reporter_key, count(*) filter (where outcome = 'confirmed') ok, count(*) n
          from citizen_reports
         where reporter_key = any($1::text[]) and outcome in ('confirmed', 'false')
         group by reporter_key
        """, list(keys))
    return {r["reporter_key"]: (r["ok"] + 1) / (r["n"] + 2) for r in rows if r["n"] >= MIN_JUDGED}


def _drone_evidence(inc: dict, incident_id: str, now: float, ctx: dict) -> list[Evidence]:
    try:
        from app.drone import swarm_link
        from app.drone.swarm_agency import to_local
    except Exception:  # noqa: BLE001
        return []
    link = swarm_link.link()
    if link is None or not link.connected:
        return []
    x, y = to_local(inc["lng"], inc["lat"], link.anchor)
    b = link.bounds
    ctx["can_fly"] = b[0] <= x <= b[2] and b[1] <= y <= b[3]
    out = []
    for si in link.state.get("incidents") or []:
        pos = si.get("position") or [0, 0]
        d = math.hypot(pos[0] - x, pos[1] - y)
        dops, _ = link._dops_incident(si.get("id"))
        if d > 400 and dops != incident_id:
            continue
        body = link.reports.get(si.get("id")) or {}
        if not body:
            continue
        people = float(((body.get("people") or {}).get("estimate")) or 0)
        hazards = [h for h in body.get("hazards") or [] if isinstance(h, dict) and str(h.get("level")) not in ("none", "low")]
        conf = float(body.get("confidence") or 0.6)
        pol = 1 if (people > 0 or hazards) else -1
        out.append(Evidence(modality="drone", witness="drone-swarm", polarity=pol, confidence=conf,
                            age_min=max(0.0, (now - float(body.get("observed_at") or now)) / 60.0) if isinstance(body.get("observed_at"), (int, float)) else 1.0,
                            distance_m=0.0 if dops == incident_id else d,
                            note=(link.report_text.get(si.get("id")) or "")[:200], ref=f"swarm:{si.get('id')}"))
    return out


def _m(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    k = 111_320.0
    return math.hypot((lng2 - lng1) * k * math.cos(math.radians((lat1 + lat2) / 2)), (lat2 - lat1) * k)


_TABLES: dict[str, bool] = {}


async def _has(conn: Any, table: str) -> bool:
    if table not in _TABLES:
        _TABLES[table] = bool(await conn.fetchval("select to_regclass($1) is not null", f"public.{table}"))
    return _TABLES[table]


# ------------------------------------------------------------------- updating --
async def assess(incident_id: str) -> tuple[dict | None, Belief | None]:
    """Gather and fuse, read only."""
    from app.db import session as db
    from app.taxonomy import cache as taxonomy
    async with db.acquire() as conn:
        inc, items, ctx = await gather(conn, incident_id)
    if inc is None:
        return None, None
    from app.eo import gfm
    obs = gfm.cached(float(inc["lng"]), float(inc["lat"])) if inc.get("lng") is not None else None
    e = gfm.evidence_for(obs, inc["category"]) if obs else None
    if e:
        items.append(Evidence(modality="satellite", witness="copernicus-gfm", polarity=e["polarity"], confidence=e["confidence"],
                              age_min=e["age_min"], note=e["note"], ref=e["ref"], half_life_min=SATELLITE_HALF_LIFE_MIN))
    ref = taxonomy.categories.get(inc["category"])
    b = fuse(items, category=inc["category"], life_safety=bool(ref and ref.life_safety),
             can_fly=ctx.get("can_fly", False), cameras_near=ctx.get("cameras_near", False))
    return inc, b


SATELLITE_HALF_LIFE_MIN = 24 * 60.0


async def satellite(inc: dict, incident_id: str, *, caused_by: int | None = None, city_id: str = "pune") -> Evidence | None:
    """GFM / Sentinel-1 reading at the incident (water categories only), logged once per pass with its images."""
    from app.eo import gfm
    if inc.get("category") not in gfm.WATER_CATEGORIES or inc.get("lng") is None:
        return None
    obs = await gfm.observe(float(inc["lng"]), float(inc["lat"]))
    await _log_satellite(incident_id, obs, caused_by=caused_by, city_id=city_id)
    e = gfm.evidence_for(obs, inc["category"])
    if e is None:
        return None
    return Evidence(modality="satellite", witness="copernicus-gfm", polarity=e["polarity"], confidence=e["confidence"],
                    age_min=e["age_min"], note=e["note"], ref=e["ref"], half_life_min=SATELLITE_HALF_LIFE_MIN)


_sat_logged: set[tuple[str, str]] = set()


async def _log_satellite(incident_id: str, obs: dict, *, caused_by: int | None, city_id: str) -> None:
    key = (incident_id, str(obs.get("item_id") or obs.get("status")))
    if key in _sat_logged:
        return
    _sat_logged.add(key)
    from app.eo import gfm
    from app.world import events
    from app.world.clock import WALL
    images = []
    for im in obs.get("images") or []:
        mid = None
        png = await gfm.fetch_png(im["url"]) if im.get("url") else None
        if png:
            mid = await _store_media(png, caption=f"{im['label']} · {obs.get('acquired_at', '')[:16]}",
                                     event_ref=f"sat:{obs.get('item_id')}:{im['key']}", city_id=city_id)
        images.append({**im, "mediaId": mid})
    await events.append(clock=WALL, kind="satellite.observed", actor="sensor:copernicus-gfm", subject_type="incident",
                        subject_id=incident_id, city_id=city_id, caused_by=caused_by,
                        payload={**{k: v for k, v in obs.items() if k != "images"}, "images": images})


async def _store_media(data: bytes, *, caption: str, event_ref: str, city_id: str, mime: str = "image/png",
                       source: str = "satellite") -> int | None:
    """Keep the actual image for the audit trail (needs migration 041 for satellite sources; URL kept otherwise)."""
    from app.db import session as db
    try:
        return await db.fetchval(
            "insert into feed_media (city_id, source, mime, bytes, caption, event_ref) values ($1,$2,$3,$4,$5,$6) returning id",
            city_id, source, mime, data, caption[:500], event_ref)
    except Exception as exc:  # noqa: BLE001 - before 041 the media table only takes camera/citizen sources
        from app.core.logging import get_logger
        get_logger(__name__).info("media_not_stored", source=source, error=str(exc)[:120])
        return None


async def update(incident_id: str, *, trigger: str, caused_by: int | None = None, city_id: str = "pune") -> Belief | None:
    """Re-fuse one incident and act on it: store belief, set verification need, wake the planner on a gate change."""
    from app.db import session as db
    from app.taxonomy import cache as taxonomy
    from app.world import events
    from app.world.clock import WALL
    async with db.acquire() as conn:
        inc, items, ctx = await gather(conn, incident_id)
    if inc is None or inc["status"] == "resolved":
        return None
    try:
        sat = await satellite(inc, incident_id, caused_by=caused_by, city_id=city_id)
    except Exception:  # noqa: BLE001
        sat = None
    if sat is not None:
        items.append(sat)
    ref0 = taxonomy.categories.get(inc["category"])
    b = fuse(items, category=inc["category"], life_safety=bool(ref0 and ref0.life_safety),
             can_fly=ctx.get("can_fly", False), cameras_near=ctx.get("cameras_near", False))
    row = await db.fetchrow("select confidence, ward_id from incidents where id = $1::uuid", incident_id)
    before = float(row["confidence"] or 0) if row else 0.0
    async with db.transaction() as conn:
        await conn.execute("update incidents set confidence = $2, trust_score = $2 where id = $1::uuid",
                           incident_id, round(b.p, 4))
        # verification first: one assessor / drone look while belief is below the floor
        has_cap = await conn.fetchval("select 1 from capabilities where id = 'field_assessment'")
        if has_cap:
            if not b.dispatch:
                await conn.execute(
                    """
                    insert into incident_needs (incident_id, capability_id, required, updated_at)
                    values ($1::uuid, 'field_assessment', 1, now())
                    on conflict (incident_id, capability_id) do update set required = greatest(incident_needs.required, 1), updated_at = now()
                    """, incident_id)
            elif inc["category"] != "unknown_report":
                await conn.execute(
                    "update incident_needs set required = 0, updated_at = now() where incident_id = $1::uuid and capability_id = 'field_assessment'",
                    incident_id)
    from app.taxonomy import cache as taxonomy
    ref = taxonomy.categories.get(inc["category"])
    ls = bool(ref and ref.life_safety)
    flipped = gate(before, ls) != gate(b.p, ls)
    if flipped or abs(b.p - before) >= 0.05:
        await events.append(clock=WALL, kind="incident.belief_changed", actor="agent:fusion", subject_type="incident",
                            subject_id=incident_id, city_id=city_id, ward_id=row["ward_id"] if row else None, caused_by=caused_by,
                            payload={"trigger": trigger, "before": round(before, 4), **b.as_dict(), "witnesses": b.witnesses[:10]})
        if flipped or not b.dispatch:
            from app.agents import event_router
            event_router.request_replan(f"belief {before:.0%} -> {b.p:.0%} on {incident_id[:8]} ({b.status})", city_id)
    return b


# ---------------------------------------------------------------- scheduling --
#: Incidents waiting to be re-fused. One worker drains them, one at a time, so a burst of evidence
#: re-fuses each incident once and never holds more than one pooled connection.
_dirty: dict[str, tuple[str, int | None, str]] = {}
_worker_task = None


def schedule(incident_id: str, *, trigger: str, caused_by: int | None = None, city_id: str = "pune") -> None:
    import asyncio
    global _worker_task
    _dirty[incident_id] = (trigger, caused_by, city_id)
    if len(_dirty) > 500:                      # bounded: drop the oldest
        _dirty.pop(next(iter(_dirty)))
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _worker_task is None or _worker_task.done():
        _worker_task = loop.create_task(_worker())


async def _worker() -> None:
    import asyncio
    from app.core.logging import get_logger
    log = get_logger(__name__)
    while _dirty:
        iid = next(iter(_dirty))
        trigger, caused_by, city = _dirty.pop(iid)
        try:
            await update(iid, trigger=trigger, caused_by=caused_by, city_id=city)
        except Exception as exc:  # noqa: BLE001
            log.warning("fusion_update_failed", incident=iid, error=f"{type(exc).__name__}: {str(exc)[:160]}")
        await asyncio.sleep(0.2)
