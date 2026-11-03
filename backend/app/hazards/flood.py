"""Flood adapter — LIVE.

Signal is GloFAS river discharge plus forecast rainfall; the score weights that
against terrain, drainage history and recorded past events. Reservoir discharge
is treated as a first-class driver rather than folded into rainfall, because in
Pune it frequently *is* the cause: the July 2024 event followed a planned
45,000-cusec release from Khadakwasla, not an unforecast downpour.
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

#: 6-hour rainfall at which the rainfall driver saturates.
RAIN_SATURATION_MM = 160.0
#: Elevation band, in metres, over which terrain advantage decays to nothing.
ELEVATION_BAND_M = 80.0


class FloodAdapter(HazardAdapter):
    hazard = HazardType.FLOOD
    display_name = "Flood"
    maturity = Maturity.LIVE
    sources = (
        "Open-Meteo forecast (rainfall, hourly)",
        "Open-Meteo Flood / GloFAS v4 (river discharge + 7-day ensemble)",
        "CWC reservoir bulletin (Khadakwasla, Panshet, Varasgaon)",
        "Copernicus DEM + PMC ward geometry (PostGIS)",
    )

    async def fetch_signal(
        self, wards: Sequence[WardContext]
    ) -> Mapping[str, HazardSignal]:
        coords = [w.centroid for w in wards]
        forecast = await open_meteo.fetch_forecast(coords)
        discharge = await open_meteo.fetch_river_discharge(coords)
        observed_at = datetime.now(UTC)
        live = forecast.live and discharge.live

        return {
            w.ward_id: HazardSignal(
                ward_id=w.ward_id,
                values={
                    "rainfall_6h_mm": forecast.rainfall_6h_mm[i],
                    "rainfall_peak_mm_h": forecast.rainfall_peak_mm_h[i],
                    "river_discharge_m3s": discharge.discharge_m3s[i],
                    "discharge_anomaly": discharge.anomaly_ratio[i],
                    # The GloFAS outlook: what the next seven days hold, which
                    # is what makes this early warning rather than reporting.
                    "glofas_peak_ratio": discharge.peak_ratio[i],
                    "glofas_peak_day": float(discharge.peak_day_offset[i]),
                    "glofas_ensemble_peak": discharge.ensemble_peak_ratio[i],
                    "glofas_spread": discharge.ensemble_spread[i],
                },
                observed_at=observed_at,
                live=live,
            )
            for i, w in enumerate(wards)
        }

    def score(self, signal: HazardSignal, ward: WardContext) -> ScoredWard:
        v = signal.values
        rain = v.get("rainfall_6h_mm", 0.0)
        peak = v.get("rainfall_peak_mm_h", 0.0)
        anomaly = v.get("discharge_anomaly", 1.0)
        glofas_peak = v.get("glofas_peak_ratio", 1.0)
        glofas_day = int(v.get("glofas_peak_day", 0.0))
        glofas_upper = v.get("glofas_ensemble_peak", 1.0)
        glofas_spread = v.get("glofas_spread", 0.0)

        # Each component is normalised to 0-1 before weighting, so the driver
        # contributions the citizen sees are directly comparable.
        rain_c = min(1.0, rain / RAIN_SATURATION_MM)
        discharge_c = min(1.0, max(0.0, (anomaly - 1.0) / 1.5))

        # The GloFAS outlook, as one component.
        #
        # Two thirds deterministic forecast peak, one third worst ensemble
        # member, so a confident forecast of a large peak and a single alarming
        # member of an otherwise calm ensemble do not score the same.
        #
        # Each term is clamped BEFORE they are combined, which matters more than
        # it looks. Ensemble tails are long — Pune's upper member today sits at
        # eight times the seasonal mean against a deterministic peak of 2.2 —
        # so combining first and clamping after let one extreme member pin the
        # whole driver at maximum and stop it discriminating between a bad week
        # and a catastrophic one.
        #
        # Different saturation points on purpose: 3x normal for the central
        # forecast, 5x for the ensemble upper, because the upper member is
        # expected to run higher and should not be read on the same scale.
        peak_term = min(1.0, max(0.0, (glofas_peak - 1.0) / 2.0))
        upper_term = min(1.0, max(0.0, (glofas_upper - 1.0) / 4.0))
        outlook_c = 0.67 * peak_term + 0.33 * upper_term
        terrain_c = max(
            0.0,
            1.0 - (ward.elevation_m - ward.city_min_elevation_m) / ELEVATION_BAND_M,
        )
        drainage_c = min(1.0, (peak / 35.0) * (0.5 + 0.5 * terrain_c))
        history_c = min(1.0, ward.past_events / 5.0)

        # Weights sum to 1.0. The previous five were scaled by 0.90 to make
        # room for the outlook at 0.10 rather than being re-tuned, so every
        # existing driver keeps its relative standing and only the new one is
        # new. A larger weight is not justified: GloFAS is a global model at
        # roughly 5 km, and at ward scale it is a strong regional signal rather
        # than a local measurement.
        score = min(
            0.97,
            0.27 * rain_c
            + 0.25 * discharge_c
            + 0.20 * terrain_c
            + 0.11 * drainage_c
            + 0.07 * history_c
            + 0.10 * outlook_c,
        )

        drivers = normalise_drivers(
            [
                (
                    "Forecast rainfall",
                    0.27 * rain_c,
                    f"{rain:.0f} mm expected over 6 hours, peaking at {peak:.0f} mm/h",
                ),
                (
                    "Reservoir discharge",
                    0.25 * discharge_c,
                    f"River discharge running {anomaly:.1f}× the seasonal mean",
                ),
                (
                    "Low-lying terrain",
                    0.20 * terrain_c,
                    f"Mean elevation {ward.elevation_m:.0f} m, "
                    f"{terrain_c * 100:.0f}% of the ward below the flood datum",
                ),
                (
                    "Drainage capacity",
                    0.11 * drainage_c,
                    "Storm drain capacity historically exceeded at this intensity",
                ),
                (
                    "Recorded flood history",
                    0.07 * history_c,
                    f"{ward.past_events} flood events recorded here since 2019"
                    if ward.past_events
                    else "No flood events recorded here since 2019",
                ),
                (
                    "GloFAS 7-day outlook",
                    0.10 * outlook_c,
                    (
                        f"Copernicus GloFAS forecasts a peak of {glofas_peak:.1f}× the "
                        f"seasonal mean "
                        + ("today" if glofas_day == 0
                           else "tomorrow" if glofas_day == 1
                           else f"in {glofas_day} days")
                        + f"; the highest ensemble member reaches {glofas_upper:.1f}×"
                        + (", and the members agree closely"
                           if glofas_spread < 0.25
                           else ", but the members disagree widely")
                    )
                    if glofas_peak > 1.05
                    else "Copernicus GloFAS forecasts no rise above the seasonal mean "
                         "over the next seven days",
                ),
            ]
        )

        # Risk climbs, peaks around hour five, then eases as the pulse passes.
        projection = [
            round(min(0.99, score * (0.42 + 0.58 * math.exp(-(((h - 5) / 3.4) ** 2)))), 3)
            for h in range(12)
        ]

        if score > 0.7:
            lead = 1.2
        elif score > 0.5:
            lead = 2.5
        elif score > 0.3:
            lead = 5.0
        else:
            lead = 9.0

        # An observed lead time beats an inferred one.
        #
        # The bands above derive lead time from the score, which is circular:
        # the score is high partly *because* something is imminent. GloFAS says
        # which day it peaks, and when that peak is further out than the band
        # assumed, the honest answer is that there is more time — capped at the
        # band so this can only ever extend a warning, never shorten one on the
        # strength of a global model disagreeing with local rainfall.
        if glofas_peak > 1.2 and glofas_day > 0:
            lead = max(lead, min(24.0, glofas_day * 24.0))

        return ScoredWard(
            ward_id=ward.ward_id,
            hazard=self.hazard,
            score=round(score, 3),
            # A stale feed is still usable, but we say so in the confidence.
            # A wide ensemble is a real reason to be less sure, and it belongs
            # in the confidence rather than in the score: disagreement among
            # members does not make a flood less likely, it makes our estimate
            # of it worse. Capped at a 12% reduction so one noisy day cannot
            # flatten a well-evidenced warning.
            confidence=round(
                (0.72 + 0.24 * score)
                * (1.0 if signal.live else 0.75)
                * (1.0 - min(0.12, 0.12 * glofas_spread)),
                2,
            ),
            lead_time_hours=lead,
            drivers=drivers,
            projection=projection,
        )

    def action_policy(
        self, scored: ScoredWard, impact: HazardImpact, ward: WardContext
    ) -> list[ProposedAction]:
        actions: list[ProposedAction] = []
        sev = scored.severity

        if sev >= 4:
            actions.append(
                ProposedAction(
                    action_key="issue_warning",
                    action="Issue red flood advisory",
                    target=f"Ward {ward.number} — {ward.name}",
                    ward_id=ward.ward_id,
                    rationale=(
                        f"Severity {sev} with {scored.lead_time_hours:g} h lead time. "
                        f"{impact.population_at_risk:,} residents in the footprint."
                    ),
                    confidence=scored.confidence,
                    severity=sev,
                )
            )
            actions.append(
                ProposedAction(
                    action_key="preposition_equipment",
                    action="Pre-position dewatering pumps and rescue boats",
                    target=f"Ward {ward.number} — {ward.name}",
                    ward_id=ward.ward_id,
                    rationale=(
                        "Pre-positioning is delegated during an active alert and the "
                        "units are currently free."
                    ),
                    confidence=round(scored.confidence * 0.95, 2),
                    severity=sev,
                    resource_need={"dewatering": 1, "water_rescue": 1},
                )
            )

        if sev >= 5:
            actions.append(
                ProposedAction(
                    action_key="activate_shelter",
                    action="Activate shelter and deploy transport",
                    target=f"Nearest designated shelter to {ward.name}",
                    ward_id=ward.ward_id,
                    rationale="A red advisory is in force, which is the condition the clause requires.",
                    confidence=round(scored.confidence * 0.92, 2),
                    severity=sev,
                    resource_need={"mass_transport": 1},
                )
            )
            if impact.lifelines_at_risk.get("school"):
                actions.append(
                    ProposedAction(
                        action_key="evacuate_school",
                        action="Evacuate school",
                        target=(
                            f"{impact.lifelines_at_risk['school']} school(s) inside the "
                            f"severity-{sev} footprint, {ward.name}"
                        ),
                        ward_id=ward.ward_id,
                        rationale=(
                            "Schools sit inside the severity-5 footprint. Evacuation is "
                            "not delegated to the Ward Officer, so this escalates."
                        ),
                        confidence=round(scored.confidence * 0.9, 2),
                        severity=sev,
                        resource_need={"mass_transport": 1},
                    )
                )

        if sev == 4 or (sev == 5 and ward.past_events >= 3):
            actions.append(
                ProposedAction(
                    action_key="close_road",
                    action="Close road and divert traffic",
                    target=f"Known low point in {ward.name}",
                    ward_id=ward.ward_id,
                    rationale="Standing water projected above 0.3 m within the hour at this point.",
                    confidence=round(scored.confidence * 0.86, 2),
                    severity=sev,
                )
            )

        return actions
