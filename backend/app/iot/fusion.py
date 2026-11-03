"""What a LoRa node's raw numbers mean. Pure functions, no I/O.

Every channel is judged against the node's own quiet baseline rather than a
fixed number: a cheap MQ-2 in a kitchen and one in a basement read very
different "normal", and a node bolted to a wall at 4 degrees is not leaning.
The baseline is learned from the node's first readings (gas only once the MQ
heaters have warmed up) and then follows slow drift while things are calm.

These are transparent, hand-set evidence weights, not a trained model, and
they say so on the console. Each score keeps its parts in `evidence` so an
officer can see that "human 82%" was mostly voices plus tapping.

Scores (all 0..1):
    human          someone is near the node: sound above the room, repeated
                   tapping on the piezo disc (the classic trapped-person
                   signal), warmth, CO2 rise without smoke, PIR if fitted
    structural     the thing the node is fixed to is moving: lean since
                   install, tilt switch flipped, shocks, sustained shaking
    environmental  gas or heat: MQ-2 / MQ-135 rise, high or rising temperature
    overall        max of the three hazards, lifted when a person is inside a
                   dangerous zone; what the heatmap shows by default
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

WARMUP_S = 120           # MQ heaters: ignore gas readings before this uptime
LEARN_N = 15             # readings that seed each baseline
ALPHA = 0.05             # baseline drift while calm
TILT_ALPHA = 0.01        # tilt baseline moves far slower: a slow lean is the point
KNOCK_MEMORY = 15        # readings of knock history kept (~30 s at 2 s/reading)

CHANNELS = ("mq2", "mq135", "temp_c", "tilt_deg", "gyro_dps", "vib_g",
            "mic", "piezo", "knocks", "tilt_sw", "pir")


def _c(x: float) -> float:
    return 0.0 if x != x else max(0.0, min(1.0, x))   # x != x: NaN


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _or(parts: list[tuple[float, float]]) -> float:
    """Noisy-OR of (weight, evidence): independent clues that each raise the odds."""
    miss = 1.0
    for w, e in parts:
        miss *= 1.0 - _c(w) * _c(e)
    return _c(1.0 - miss)


@dataclass
class Scored:
    human: float
    structural: float
    environmental: float
    overall: float
    evidence: dict[str, float]
    flags: list[str]
    baseline: dict[str, Any]
    state: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"human": round(self.human, 3), "structural": round(self.structural, 3),
                "environmental": round(self.environmental, 3), "overall": round(self.overall, 3),
                "evidence": self.evidence, "flags": self.flags}


def _learn(base: dict, key: str, value: float | None, calm: bool, alpha: float = ALPHA) -> None:
    """Seed with a running mean for LEARN_N readings, then EWMA while calm."""
    if value is None:
        return
    n = int(base.get(f"n_{key}", 0))
    cur = _num(base.get(key))
    if cur is None or n < LEARN_N:
        base[key] = value if cur is None else cur + (value - cur) / (n + 1)
        base[f"n_{key}"] = n + 1
    elif calm:
        base[key] = cur + alpha * (value - cur)


def score(obs: dict, baseline: dict | None, state: dict | None) -> Scored:
    """Score one reading against the node's baseline; returns the updated
    baseline and state too (the caller stores them)."""
    base = dict(baseline or {})
    st = dict(state or {})
    v = {k: _num(obs.get(k)) for k in CHANNELS}
    up = _num(obs.get("up")) or _num(obs.get("uptime_s"))
    gas_ready = up is None or up >= WARMUP_S
    learning = int(base.get("n_mic", 0)) < LEARN_N

    def b(key: str) -> float | None:
        return _num(base.get(key)) if int(base.get(f"n_{key}", 0)) >= 3 else None

    flags: list[str] = []
    ev: dict[str, float] = {}

    # ---- environmental ----------------------------------------------------
    mq2, mq135, temp = v["mq2"], v["mq135"], v["temp_c"]
    b2, b135, bt = b("mq2"), b("mq135"), b("temp_c")
    gas2 = _c((mq2 - b2) / max(60.0, 0.35 * b2)) if gas_ready and mq2 is not None and b2 else 0.0
    gas135 = (_c((mq135 - b135) / max(80.0, 0.4 * b135))
              if gas_ready and mq135 is not None and b135 else 0.0)
    heat = _c((temp - 45.0) / 20.0) if temp is not None else 0.0
    heat_rise = _c((temp - bt - 3.0) / 12.0) if temp is not None and bt is not None else 0.0
    ev.update(gas_mq2=gas2, gas_mq135=gas135, heat=max(heat, heat_rise))
    environmental = max(gas2, 0.85 * gas135, heat, 0.7 * heat_rise)

    # ---- human --------------------------------------------------------------
    mic, bmic = v["mic"], b("mic")
    audio = _c((mic - bmic - 15.0) / max(40.0, 2.0 * bmic)) if mic is not None and bmic is not None else 0.0
    knocks = int(v["knocks"] or 0)
    hist = (list(st.get("knock_hist", [])) + [knocks])[-KNOCK_MEMORY:]
    st["knock_hist"] = hist
    rhythm = _c(sum(1 for k in hist if k >= 2) / 3.0)        # tapping that keeps coming back
    tapping = max(0.5 * _c(knocks / 4.0), rhythm)
    body = (_c((temp - bt - 1.0) / 4.0)
            if temp is not None and bt is not None and 20.0 <= temp <= 40.0 else 0.0)
    breath = 0.5 * gas135 if gas2 < 0.3 else 0.0               # CO2 up without smoke
    pir = 1.0 if v["pir"] == 1 else 0.0
    ev.update(audio=audio, tapping=tapping, warmth=body, breath=breath, pir=pir)
    human = _or([(0.55, audio), (0.75, tapping), (0.25, body), (0.2, breath), (0.6, pir)])

    # ---- structural ---------------------------------------------------------
    tilt, btilt = v["tilt_deg"], b("tilt_deg")
    lean = _c((abs(tilt - btilt) - 1.5) / 8.0) if tilt is not None and btilt is not None else 0.0
    tsw, btsw = v["tilt_sw"], b("tilt_sw")
    switched = 1.0 if tsw is not None and btsw is not None and abs(tsw - round(btsw)) >= 1 else 0.0
    shock = _c(((v["gyro_dps"] or 0.0) - 15.0) / 60.0)
    shaking = _c(((v["vib_g"] or 0.0) - 0.02) / 0.15)
    bpz = b("piezo")
    rumble = (_c((v["piezo"] - bpz - 40.0) / 300.0)
              if v["piezo"] is not None and bpz is not None and knocks == 0 else 0.0)
    ev.update(lean=lean, tilt_switch=switched, shock=shock, shaking=max(shaking, rumble))
    structural = _or([(1.0, lean), (0.6, switched), (0.5, shock), (0.6, shaking), (0.4, rumble)])

    hazard = max(structural, environmental)
    overall = _c(max(hazard, 0.6 * human) + 0.3 * human * hazard)

    # ---- flags ----------------------------------------------------------------
    if not gas_ready:
        flags.append("warming_up")
    if learning:
        flags.append("learning")
    for name, val, th in (("gas_mq2", gas2, 0.5), ("gas_mq135", gas135, 0.5),
                          ("heat", max(heat, heat_rise), 0.5), ("sound", audio, 0.5),
                          ("tapping", tapping, 0.5), ("tilt_shift", lean, 0.4),
                          ("tilt_switch", switched, 1.0), ("shock", shock, 0.4),
                          ("shaking", max(shaking, rumble), 0.4)):
        if val >= th:
            flags.append(name)

    # ---- learn ------------------------------------------------------------------
    calm = overall < 0.3
    if gas_ready:
        _learn(base, "mq2", mq2, calm)
        _learn(base, "mq135", mq135, calm)
    _learn(base, "temp_c", temp, calm)
    _learn(base, "mic", mic, calm)
    _learn(base, "piezo", v["piezo"] if knocks == 0 else None, calm)
    _learn(base, "tilt_sw", tsw, calm and switched == 0)
    _learn(base, "tilt_deg", tilt, calm and lean == 0, alpha=TILT_ALPHA)

    ev = {k: round(x, 3) for k, x in ev.items()}
    return Scored(human, structural, environmental, overall, ev, flags, base, st)


# ---- escalation -----------------------------------------------------------------
ESCALATE_AFTER = 2          # consecutive readings over the line (~4 s), not one spike
COOLDOWN_S = 600


def escalations(s: Scored, state: dict, now_s: float, last: dict) -> list[dict]:
    """Which hazards this reading should file as a sensor report.

    Returns [{kind, confidence, why}] and updates `state` streak counters.
    `last` is kind -> epoch seconds of the last escalation, for the cooldown.
    """
    rules = (
        # kind      condition                                                  confidence
        ("fire",     s.environmental >= 0.7,                                    s.environmental),
        ("collapse", s.structural >= 0.7,                                       s.structural),
        ("trapped",  s.human >= 0.65 and max(s.structural, s.environmental) >= 0.4,
                     min(1.0, 0.5 * s.human + 0.5 * max(s.structural, s.environmental) + 0.1)),
    )
    out = []
    streaks = dict(state.get("streak", {}))
    for kind, hit, conf in rules:
        streaks[kind] = streaks.get(kind, 0) + 1 if hit else 0
        if streaks[kind] >= ESCALATE_AFTER and now_s - float(last.get(kind, 0)) >= COOLDOWN_S:
            out.append({"kind": kind, "confidence": round(conf, 3), "why": why(s, kind)})
    state["streak"] = streaks
    return out


def why(s: Scored, kind: str) -> str:
    e = s.evidence
    top = sorted(((k, x) for k, x in e.items() if x >= 0.3), key=lambda kv: -kv[1])[:4]
    parts = ", ".join(f"{k.replace('_', ' ')} {x:.0%}" for k, x in top) or "several weak signals"
    head = {"fire": "Gas/heat hazard", "collapse": "Structural movement",
            "trapped": "Possible person in a hazardous spot"}[kind]
    return (f"{head}: human {s.human:.0%}, structural {s.structural:.0%}, "
            f"environment {s.environmental:.0%} ({parts}).")
