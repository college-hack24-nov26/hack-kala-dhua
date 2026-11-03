"""Heatwave adapter — LIVE.

The health hazard in the domain. Its exposure join deliberately differs from
flood's: heat mortality concentrates in the over-60 population and in outdoor
workers, so scoring a ward purely by headcount would rank the wrong wards.
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
from app.ingest import open_meteo
from app.schemas.domain import HazardType

#: IMD declares a heatwave for the plains at 40 °C; danger rises steeply after.
HEAT_THRESHOLD_C = 40.0
HEAT_CEILING_C = 48.0


class HeatwaveAdapter(HazardAdapter):
    hazard = HazardType.HEAT
    display_name = "Heatwave"
    maturity = Maturity.LIVE
    sources = (
        "Open-Meteo forecast (temperature, apparent temperature, humidity)",
        "IMD heatwave bulletin, Pune district",
        "Census age structure + PMC ward geometry (PostGIS)",
    )

    async def fetch_signal(
        self, wards: Sequence[WardContext]
    ) -> Mapping[str, HazardSignal]:
        forecast = await open_meteo.fetch_forecast([w.centroid for w in wards])
        observed_at = datetime.now(UTC)
        return {
            w.ward_id: HazardSignal(
                ward_id=w.ward_id,
                values={
                    "temp_max_c": forecast.temp_max_c[i],
                    "apparent_max_c": forecast.apparent_max_c[i],
                    "humidity_pct": forecast.humidity_pct[i],
                },
                observed_at=observed_at,
                live=forecast.live,
            )
            for i, w in enumerate(wards)
        }

    def score(self, signal: HazardSignal, ward: WardContext) -> ScoredWard:
        v = signal.values
        temp = v.get("temp_max_c", 0.0)
        apparent = v.get("apparent_max_c", temp)
        humidity = v.get("humidity_pct", 0.0)

        heat_c = min(
            1.0,
            max(0.0, (apparent - HEAT_THRESHOLD_C) / (HEAT_CEILING_C - HEAT_THRESHOLD_C)),
        )
        humidity_c = min(1.0, humidity / 80.0) * heat_c
        # Dense, low-vegetation wards retain heat overnight; area per person is
        # a crude but honest proxy for the urban heat island effect.
        density = ward.population / max(ward.area_sq_km, 0.1)
        island_c = min(1.0, density / 25_000.0)
        vulnerable_c = min(1.0, ward.elderly_share / 0.20)

        score = min(
            0.97,
            0.46 * heat_c + 0.16 * humidity_c + 0.20 * island_c + 0.18 * vulnerable_c,
        )

        drivers = normalise_drivers(
            [
                (
                    "Apparent temperature",
                    0.46 * heat_c,
                    f"Feels like {apparent:.0f} °C against a {HEAT_THRESHOLD_C:.0f} °C heatwave threshold",
                ),
                (
                    "Humidity load",
                    0.16 * humidity_c,
                    f"{humidity:.0f}% relative humidity suppresses evaporative cooling",
                ),
                (
                    "Urban heat island",
                    0.20 * island_c,
                    f"{density:,.0f} residents per km² with limited tree cover",
                ),
                (
                    "Vulnerable population",
                    0.18 * vulnerable_c,
                    f"{ward.elderly_share * 100:.0f}% of residents are over 60",
                ),
            ]
        )

        # Heat peaks mid-afternoon rather than as a pulse.
        projection = [
            round(min(0.99, score * (0.35 + 0.65 * math.exp(-(((h - 7) / 4.0) ** 2)))), 3)
            for h in range(12)
        ]

        return ScoredWard(
            ward_id=ward.ward_id,
            hazard=self.hazard,
            score=round(score, 3),
            confidence=round((0.78 + 0.18 * score) * (1.0 if signal.live else 0.75), 2),
            lead_time_hours=6.0 if score > 0.6 else 12.0,
            drivers=drivers,
            projection=projection,
        )

    def impact(self, scored: ScoredWard, ward: WardContext) -> HazardImpact:
        """Heat exposure is not the general population.

        Weighted towards the over-60 share, which is where heat mortality
        actually concentrates.
        """
        elderly = ward.population * ward.elderly_share
        others = ward.population - elderly
        at_risk = int((elderly * 0.85 + others * 0.15) * scored.score)
        return HazardImpact(
            ward_id=ward.ward_id,
            population_at_risk=at_risk,
            lifelines_at_risk=(
                {k: v for k, v in ward.lifelines.items() if k in ("hospital", "school")}
                if scored.severity >= 4
                else {}
            ),
            summary=(
                f"{at_risk:,} heat-vulnerable residents of {ward.name}, "
                f"weighted to the over-60 population"
            ),
        )

    def action_policy(
        self, scored: ScoredWard, impact: HazardImpact, ward: WardContext
    ) -> list[ProposedAction]:
        sev = scored.severity
        if sev < 3:
            return []

        actions = [
            ProposedAction(
                action_key="issue_advisory",
                action="Issue heat advisory",
                target=f"Ward {ward.number} — {ward.name}",
                ward_id=ward.ward_id,
                rationale=(
                    f"Severity {sev}. {impact.population_at_risk:,} heat-vulnerable "
                    "residents; advisory is delegated to the Ward Officer."
                ),
                confidence=scored.confidence,
                severity=sev,
            )
        ]

        if sev >= 4:
            actions.append(
                ProposedAction(
                    action_key="activate_shelter",
                    action="Open cooling centre",
                    target=f"Designated shelter serving {ward.name}",
                    ward_id=ward.ward_id,
                    rationale="Shelter activation is delegated once an advisory is in force.",
                    confidence=round(scored.confidence * 0.93, 2),
                    severity=sev,
                    resource_need={"mass_transport": 1},
                )
            )
            actions.append(
                ProposedAction(
                    action_key="preposition_equipment",
                    action="Stage ambulance for heat casualties",
                    target=f"Ward {ward.number} — {ward.name}",
                    ward_id=ward.ward_id,
                    rationale="Heat casualty presentations rise sharply above severity 4.",
                    confidence=round(scored.confidence * 0.88, 2),
                    severity=sev,
                    resource_need={"medical_transport": 1},
                )
            )
        return actions
