"""How much to believe a report.

A disaster platform that accepts public reports and then moves real vehicles has
an adversarial input problem whether or not it admits one. Rumour spreads faster
than verified information, and a flood of identical reports from newly created
accounts is enough to pull scarce rescue units away from the place that needs
them. That is not an exotic attack; it is what a bad afternoon on social media
does by accident.

So every report gets a score, from six components, all deterministic. The model
is not consulted. A language model deciding whether to believe a rescue call is
exactly the wrong division of labour: it would be confident, unaccountable, and
impossible to explain to an officer afterwards.

What the score controls is not truth, it is *cost*. A low-trust report is not
deleted and not called a lie. It is prevented from spending an ambulance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping

from app.core.logging import get_logger

log = get_logger(__name__)

#: Above this a report enters the operational pipeline by itself.
AUTO_CONFIRM = 0.72
#: Below this it never causes a dispatch on its own.
QUARANTINE = 0.35

#: Base credibility by channel. A field operator on shift is not the same kind
#: of source as an anonymous web form, and the gap is about chain of custody
#: rather than about honesty.
SOURCE_CREDIBILITY: Mapping[str, float] = {
    "field": 0.95,
    "agency": 0.92,
    "sensor": 0.88,
    # A person's report that reached us over the offline mesh. The words and
    # the GPS fix are theirs; the device cannot be looked up and the packet
    # passed through strangers' phones. Signed packets only — unsigned ones are
    # scored as "mesh_unsigned".
    "mesh": 0.66,
    "mesh_unsigned": 0.45,
    "phone": 0.70,
    "app": 0.62,
    "sim": 0.62,
}

#: How much a fully anomalous source discounts an otherwise good score.
#: At 1.0 anomaly this leaves 30% of the score, which puts any realistic report
#: below the quarantine floor.
ANOMALY_DISCOUNT = 0.70

WEIGHTS: Mapping[str, float] = {
    "source": 0.22,
    "reporter": 0.18,
    "location": 0.18,
    "corroboration": 0.22,
    "evidence": 0.10,
}


@dataclass(slots=True)
class TrustInputs:
    source: str
    reporter_id: str | None
    reporter_reliability: float | None
    #: Current hazard score for the ward, 0-1, or None if nothing is running.
    ward_risk: float | None
    #: Is the reported category plausible for the hazard that is actually active?
    category_plausible: bool
    #: Independent reports already clustered here, excluding this reporter and
    #: this device.
    corroborations: int
    has_photo: bool
    #: Reports from this device or reporter in the last fifteen minutes.
    recent_from_source: int
    #: Reports from this device that were near-identical in text.
    near_duplicate_text: int
    #: Implied speed between this reporter's last report and this one, km/h.
    implied_speed_kmh: float | None
    #: Agreement between an attached photo and what was reported, -1..1, from
    #: `vision.assess`. None when there was no photo or no analysis of it.
    photo_agreement: float | None = None


@dataclass(slots=True)
class Trust:
    score: float
    status: str
    components: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    @property
    def may_dispatch(self) -> bool:
        """Whether this report alone may commit a unit."""
        return self.status == "auto_confirmed"


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def score(inputs: TrustInputs, *, life_safety: bool = False) -> Trust:
    """Six components in, one score and a routing decision out."""
    c: dict[str, float] = {}
    reasons: list[str] = []

    # 1. Which channel it arrived on.
    c["source"] = SOURCE_CREDIBILITY.get(inputs.source, 0.5)

    # 2. How this reporter has done before.
    if inputs.reporter_reliability is None:
        c["reporter"] = 0.5
        if inputs.reporter_id is None:
            reasons.append("Anonymous reporter, so no history to weigh.")
    else:
        c["reporter"] = _clamp(inputs.reporter_reliability)

    # 3. Is this plausible where they say they are?
    if inputs.ward_risk is None:
        c["location"] = 0.5
    else:
        base = 0.35 + 0.65 * inputs.ward_risk
        c["location"] = _clamp(base if inputs.category_plausible else base * 0.6)
        if not inputs.category_plausible:
            reasons.append(
                "The reported category does not match the hazard currently "
                "active in this ward."
            )

    # 4. Did anyone else, independently, say the same thing?
    #    Saturating: the second independent report is worth far more than the
    #    ninth, and counting them linearly is what lets a burst look like truth.
    n = max(0, inputs.corroborations)
    c["corroboration"] = _clamp(1.0 - 1.0 / (1.0 + 0.9 * n)) if n else 0.25
    if n >= 2:
        reasons.append(f"{n} independent reports describe the same thing.")

    # 5. Evidence attached, and whether it agrees.
    #
    # A photo used to be worth the same whatever it showed, which is the wrong
    # shape: attaching *a* photo is easy and attaching one that matches the claim
    # is not. When a vision model has looked at it, the agreement it found moves
    # this component in both directions — a picture of a dry street against a
    # "road is flooded" report should cost the report, not flatter it.
    #
    # Bounded deliberately. At full agreement this reaches 0.95, which is high
    # but not enough on its own to clear the auto-confirm floor: a photo still
    # cannot carry a report past the gate by itself.
    c["evidence"] = 0.85 if inputs.has_photo else 0.45
    if inputs.photo_agreement is not None:
        c["evidence"] = _clamp(0.65 + 0.30 * inputs.photo_agreement, 0.15, 0.95)
        if inputs.photo_agreement > 0.15:
            reasons.append("The attached photo shows what was reported.")
        elif inputs.photo_agreement < -0.15:
            reasons.append(
                "The attached photo does not show what was reported, which is "
                "also what a fabricated report looks like."
            )

    # 6. Anomaly. This one discounts everything above it.
    anomaly = 0.0
    # Repeats are not anomalies. A frightened person reports again and again, and
    # a sensor network reports many places within minutes. Whether the thing is
    # real is decided per incident by evidence fusion (app/incidents/fusion.py):
    # the same witness counts once there, its repeats are read as urgency, and
    # independent sources around it decide. Only physically impossible movement
    # (a spoofed location) still discounts a report.
    if inputs.recent_from_source > 3 or inputs.near_duplicate_text > 0:
        reasons.append("Repeated report from the same source: counted once as evidence, read as urgency.")
    if inputs.implied_speed_kmh is not None and inputs.implied_speed_kmh > 180:
        anomaly += 0.35
        reasons.append(
            f"Implied travel of {inputs.implied_speed_kmh:.0f} km/h since this "
            "reporter's previous report."
        )
    c["anomaly"] = _clamp(anomaly)

    kept = ("source", "reporter", "location", "corroboration", "evidence")
    raw = sum(WEIGHTS[k] * c[k] for k in kept) / sum(WEIGHTS[k] for k in kept)

    # Anomaly discounts the score rather than subtracting from it. Subtracting a
    # fixed amount meant a nine-report burst from one device, with duplicated
    # text, still landed in "needs corroboration" instead of being held: the
    # most obvious fabrication pattern there is, waved through. A discount
    # scales with how strong the rest of the evidence looked, which is the right
    # shape, because a confident-looking report from a suspicious source is
    # exactly the one that should lose the most.
    final = _clamp(raw * (1.0 - ANOMALY_DISCOUNT * c["anomaly"]))

    status = _route(final, life_safety=life_safety, corroborations=n, reasons=reasons)
    return Trust(score=round(final, 4), status=status, components={k: round(v, 4) for k, v in c.items()}, reasons=reasons)


def _route(value: float, *, life_safety: bool, corroborations: int, reasons: list[str]) -> str:
    """Trust decides what a report is allowed to cost, not whether it is true.

    The life-safety asymmetry is deliberate and is the most important rule here.
    A report of a person stranded in water is acted on at a lower bar than a
    report of a blocked drain, because the two mistakes are not comparable:
    sending a boat to nobody wastes a boat, and not sending one drowns somebody.
    """
    if life_safety and value >= QUARANTINE:
        if value < AUTO_CONFIRM:
            reasons.append(
                "Below the usual confidence bar, but this is a life-safety "
                "category, so it is confirmed rather than held."
            )
        return "auto_confirmed"
    if value >= AUTO_CONFIRM:
        return "auto_confirmed"
    if value >= QUARANTINE:
        if corroborations == 0:
            reasons.append("Uncorroborated. A second independent report would confirm it.")
        return "needs_corroboration"
    reasons.append("Too low to commit a unit. Held for a human to look at.")
    return "quarantined"


def explain(trust: Trust) -> str:
    """One sentence for the console, next to the report."""
    top = sorted(
        ((k, v) for k, v in trust.components.items() if k != "anomaly"),
        key=lambda kv: kv[1],
    )
    weakest = top[0][0] if top else "source"
    label = {
        "source": "the channel it came from",
        "reporter": "this reporter's history",
        "location": "whether it is plausible here",
        "corroboration": "independent corroboration",
        "evidence": "attached evidence",
    }.get(weakest, weakest)
    return (
        f"Trust {trust.score:.0%} ({trust.status.replace('_', ' ')}). "
        f"Weakest component is {label}."
    )
