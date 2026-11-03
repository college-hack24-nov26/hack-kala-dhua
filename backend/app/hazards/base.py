"""The hazard adapter contract.

This is the abstraction the whole platform rests on. Every hazard — flood,
heatwave, wildfire, air quality, seismic — implements the same five stages:

    signal(t) -> score(cell) -> exposure_join(cell) -> impact(cell) -> action_policy(impact)

Nothing downstream of this file knows which hazard it is handling. The
orchestrator, the solver, the decision gate, the alert composer and all three
interfaces operate on `ScoredWard` and `ProposedAction` alone. Adding cyclone
means writing one adapter, not touching any of them.

Adapters declare their own `maturity`. `LIVE` means the scorer is trained and
runs against real feeds; `REGISTERED` means it implements the contract but its
scorer is a placeholder. We surface that distinction in the API rather than
letting a stub masquerade as a model.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Mapping, Sequence

from app.schemas.domain import HazardType, RiskDriver


class Maturity(StrEnum):
    LIVE = "live"
    REGISTERED = "registered"


@dataclass(frozen=True, slots=True)
class WardContext:
    """Everything an adapter is allowed to know about a ward.

    Deliberately narrow: an adapter that needs something not in here should
    fetch it in `fetch_signal`, not reach into the database mid-score.
    """

    ward_id: str
    number: str
    name: str
    centroid: tuple[float, float]
    population: int
    elderly_share: float
    elevation_m: float
    area_sq_km: float
    #: Minimum elevation across all wards, so terrain can be scored relatively.
    city_min_elevation_m: float
    #: Counts by lifeline kind inside the ward.
    lifelines: Mapping[str, int] = field(default_factory=dict)
    #: Recorded events of this hazard type here, for the history driver.
    past_events: int = 0


@dataclass(frozen=True, slots=True)
class HazardSignal:
    """One upstream reading for one ward, before any scoring."""

    ward_id: str
    #: Raw named measurements, e.g. {"rainfall_mm_6h": 142.0}.
    values: Mapping[str, float]
    observed_at: datetime
    #: True when this came from the live API rather than the cache.
    live: bool = True


@dataclass(frozen=True, slots=True)
class ScoredWard:
    ward_id: str
    hazard: HazardType
    score: float
    confidence: float
    lead_time_hours: float
    drivers: list[RiskDriver]
    #: Twelve hourly scores from now.
    projection: list[float]

    @property
    def severity(self) -> int:
        return severity_of(self.score)


@dataclass(frozen=True, slots=True)
class HazardImpact:
    ward_id: str
    population_at_risk: int
    lifelines_at_risk: Mapping[str, int]
    #: Free-text summary the guidance agent can quote.
    summary: str


@dataclass(frozen=True, slots=True)
class ProposedAction:
    """A candidate action, before the policy gate has seen it.

    `action_key` is what the policy corpus is searched on; keep it stable, it
    is effectively a foreign key into the delegation matrix.
    """

    action_key: str
    action: str
    target: str
    ward_id: str | None
    rationale: str
    confidence: float
    severity: int
    #: What the action needs DONE, as {capability_id: count}, e.g.
    #: {"dewatering": 2, "water_rescue": 1}. Adapters never name a vehicle:
    #: which kind of unit answers a capability is city-specific fleet data, and
    #: the solver resolves it. This is what lets one adapter work in a city with
    #: an entirely different set of equipment.
    resource_need: Mapping[str, int] = field(default_factory=dict)


def severity_of(score: float) -> int:
    """Score to severity bucket. One definition, shared with the frontend."""
    if score >= 0.8:
        return 5
    if score >= 0.6:
        return 4
    if score >= 0.4:
        return 3
    if score >= 0.2:
        return 2
    return 1


def normalise_drivers(raw: Sequence[tuple[str, float, str]]) -> list[RiskDriver]:
    """Turn (label, weight, detail) triples into contributions summing to 1,
    ordered by significance. Every adapter uses this so the explainability
    panel reads consistently no matter which hazard produced it."""
    total = sum(max(0.0, w) for _, w, _ in raw) or 1.0
    drivers = [
        RiskDriver(label=label, contribution=max(0.0, weight) / total, detail=detail)
        for label, weight, detail in raw
    ]
    return sorted(drivers, key=lambda d: d.contribution, reverse=True)


class HazardAdapter(abc.ABC):
    """Implement this once per hazard. Nothing else needs to change."""

    hazard: HazardType
    display_name: str
    maturity: Maturity = Maturity.REGISTERED
    #: Human-readable feed names, shown in the console's source list.
    sources: tuple[str, ...] = ()

    # ---------------------------------------------------------- 1. signal --
    @abc.abstractmethod
    async def fetch_signal(
        self, wards: Sequence[WardContext]
    ) -> Mapping[str, HazardSignal]:
        """Pull the upstream reading for each ward.

        Must not raise on upstream failure: return whatever was retrievable and
        mark the rest `live=False`. A dead API degrades the run; it does not
        cancel it.
        """

    # ----------------------------------------------------------- 2. score --
    @abc.abstractmethod
    def score(self, signal: HazardSignal, ward: WardContext) -> ScoredWard:
        """Turn the reading plus ward context into a score with its drivers.

        The drivers are not decoration. They are the answer to "why am I being
        warned?", and they must actually explain the number.
        """

    # ---------------------------------------------------------- 3. impact --
    def impact(self, scored: ScoredWard, ward: WardContext) -> HazardImpact:
        """Default exposure join: population scaled by score, plus lifelines.

        Adapters override when the exposed population differs from the general
        one — heat, for instance, weights the over-60 share far more heavily.
        """
        at_risk = int(ward.population * scored.score * 0.34)
        return HazardImpact(
            ward_id=ward.ward_id,
            population_at_risk=at_risk,
            lifelines_at_risk=dict(ward.lifelines) if scored.severity >= 4 else {},
            summary=(
                f"{at_risk:,} residents of {ward.name} in the "
                f"{self.display_name.lower()} footprint"
            ),
        )

    # --------------------------------------------------- 4. action policy --
    @abc.abstractmethod
    def action_policy(
        self, scored: ScoredWard, impact: HazardImpact, ward: WardContext
    ) -> list[ProposedAction]:
        """Candidate actions for this ward, in priority order.

        These are proposals only. Whether any of them may issue is decided by
        the policy gate against the delegation matrix, not here.
        """

    # ------------------------------------------------------------ helpers --
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.hazard}:{self.maturity}>"
