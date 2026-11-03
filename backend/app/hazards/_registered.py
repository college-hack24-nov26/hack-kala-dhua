"""Registered-but-not-trained adapters.

These implement the full contract and are wired into the orchestrator, the
solver and all three interfaces exactly like the live ones. What they do not
have is a trained scorer — `score()` is a documented heuristic placeholder.

They are declared `Maturity.REGISTERED` so that the API, the console and the
deck all say so. The point of the abstraction is that promoting one to LIVE
means replacing the body of `score()` and nothing else.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Mapping, Sequence

from app.hazards.base import (
    HazardAdapter,
    HazardImpact,
    HazardSignal,
    Maturity,
    ProposedAction,
    ScoredWard,
    WardContext,
    normalise_drivers,
)
from app.schemas.domain import HazardType


class RegisteredAdapter(HazardAdapter):
    """Shared behaviour for adapters awaiting a trained scorer."""

    maturity = Maturity.REGISTERED
    #: Named measurements this adapter will consume once it is live.
    expected_signals: tuple[str, ...] = ()

    async def fetch_signal(
        self, wards: Sequence[WardContext]
    ) -> Mapping[str, HazardSignal]:
        now = datetime.now(UTC)
        return {
            w.ward_id: HazardSignal(
                ward_id=w.ward_id, values={}, observed_at=now, live=False
            )
            for w in wards
        }

    def _flat_score(self, ward: WardContext, base: float, detail: str) -> ScoredWard:
        return ScoredWard(
            ward_id=ward.ward_id,
            hazard=self.hazard,
            score=base,
            # Low confidence is the honest signal that this is not a model.
            confidence=0.25,
            lead_time_hours=24.0,
            drivers=normalise_drivers([(f"{self.display_name} baseline", 1.0, detail)]),
            projection=[round(base * (0.9 + 0.1 * math.sin(h)), 3) for h in range(12)],
        )

    def action_policy(
        self, scored: ScoredWard, impact: HazardImpact, ward: WardContext
    ) -> list[ProposedAction]:
        if scored.severity < 4:
            return []
        return [
            ProposedAction(
                action_key="issue_advisory",
                action=f"Issue {self.display_name.lower()} advisory",
                target=f"Ward {ward.number} — {ward.name}",
                ward_id=ward.ward_id,
                rationale=(
                    f"{self.display_name} adapter is registered but not yet trained; "
                    "an advisory is the only action it proposes."
                ),
                confidence=scored.confidence,
                severity=scored.severity,
            )
        ]


class AirQualityAdapter(RegisteredAdapter):
    hazard = HazardType.AIR
    display_name = "Air Quality"
    sources = (
        "Open-Meteo Air Quality / CAMS (PM2.5, PM10)",
        "CPCB CAAQMS station feed via data.gov.in",
        "NASA FIRMS active fire detections (biomass burning attribution)",
    )
    expected_signals = ("pm2_5", "pm10", "us_aqi", "fire_count_50km")

    def score(self, signal: HazardSignal, ward: WardContext) -> ScoredWard:
        return self._flat_score(
            ward, 0.18, "Awaiting the trained source-attribution model; CAMS feed is wired."
        )


class WildfireAdapter(RegisteredAdapter):
    hazard = HazardType.FIRE
    display_name = "Wildfire"
    sources = (
        "NASA FIRMS VIIRS active fire, near real time",
        "Open-Meteo forecast (wind speed, humidity, temperature)",
        "Forest cover and wildland-urban interface layer",
    )
    expected_signals = ("fire_radiative_power", "wind_speed_ms", "humidity_pct", "fuel_load")

    def score(self, signal: HazardSignal, ward: WardContext) -> ScoredWard:
        return self._flat_score(
            ward, 0.10, "FIRMS feed is wired; spread model not yet trained for this geography."
        )


class SeismicAdapter(RegisteredAdapter):
    hazard = HazardType.SEISMIC
    display_name = "Seismic"
    sources = (
        "USGS earthquake feed (real time)",
        "NCS India seismic bulletin",
        "Building vulnerability class by ward",
    )
    expected_signals = ("magnitude", "depth_km", "epicentre_distance_km", "soil_class")

    def score(self, signal: HazardSignal, ward: WardContext) -> ScoredWard:
        return self._flat_score(
            ward,
            0.06,
            "Live USGS feed; no recent Indian urban event with usable local "
            "impact data to validate a shaking-to-damage model against.",
        )
