"""Open-Meteo: forecast, GloFAS river discharge, and air quality.

Chosen as the primary feed because it needs no key, has no request ceiling for
non-commercial use, and — crucially for a flood system — exposes GloFAS river
discharge, which is the actual hydrological signal rather than a proxy for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.core.config import settings
from app.ingest.client import get_json

# Open-Meteo accepts batched coordinates, so one call covers every ward.
_MAX_BATCH = 100


@dataclass(slots=True)
class ForecastBatch:
    """Per-coordinate results, index-aligned with the request."""

    rainfall_6h_mm: list[float]
    rainfall_peak_mm_h: list[float]
    temp_max_c: list[float]
    apparent_max_c: list[float]
    humidity_pct: list[float]
    live: bool


def _as_list(payload: object) -> list[dict]:
    """A single coordinate returns an object; several return a list."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return [payload]
    return []


async def fetch_forecast(coords: Sequence[tuple[float, float]]) -> ForecastBatch:
    lats = ",".join(f"{lat:.4f}" for _, lat in coords[:_MAX_BATCH])
    lngs = ",".join(f"{lng:.4f}" for lng, _ in coords[:_MAX_BATCH])

    result = await get_json(
        settings.open_meteo_forecast_url,
        {
            "latitude": lats,
            "longitude": lngs,
            "hourly": "precipitation,relative_humidity_2m",
            "daily": "temperature_2m_max,apparent_temperature_max",
            "forecast_days": 2,
            "timezone": "Asia/Kolkata",
        },
        cache_key=f"om:forecast:{lats}",
    )

    n = len(coords)
    rain6 = [0.0] * n
    peak = [0.0] * n
    tmax = [0.0] * n
    amax = [0.0] * n
    hum = [0.0] * n

    for i, block in enumerate(_as_list(result.data)):
        if i >= n:
            break
        hourly = block.get("hourly") or {}
        precip = [p or 0.0 for p in (hourly.get("precipitation") or [])][:6]
        rh = [h or 0.0 for h in (hourly.get("relative_humidity_2m") or [])][:6]
        daily = block.get("daily") or {}
        rain6[i] = float(sum(precip))
        peak[i] = float(max(precip)) if precip else 0.0
        hum[i] = float(sum(rh) / len(rh)) if rh else 0.0
        tmax[i] = float((daily.get("temperature_2m_max") or [0.0])[0] or 0.0)
        amax[i] = float((daily.get("apparent_temperature_max") or [0.0])[0] or 0.0)

    return ForecastBatch(rain6, peak, tmax, amax, hum, live=result.live)


@dataclass(slots=True)
class DischargeBatch:
    discharge_m3s: list[float]
    #: Discharge relative to the 30-day mean for the same points.
    anomaly_ratio: list[float]
    #: The GloFAS outlook, which is the part that makes this an *early* warning
    #: rather than a description of today.
    #:
    #: The adapter used to read `river_discharge[0]` and discard the other six
    #: days, which threw away the entire forecast: a ward whose river is normal
    #: today and triples on Tuesday scored exactly like one that is normal all
    #: week. These three carry that.
    #:
    #: Peak of the deterministic forecast over the horizon, as a multiple of the
    #: seasonal mean.
    peak_ratio: list[float]
    #: Days until that peak. This is a real lead time, observed rather than
    #: inferred from how high the score came out.
    peak_day_offset: list[int]
    #: The worst ensemble member at the peak, as a multiple of the mean. GloFAS
    #: is an ensemble; `river_discharge_max` is the upper member, and the gap
    #: between it and the deterministic run is the tail this system should care
    #: about — a 1-in-20 member at four times normal is a different situation
    #: from a confident forecast of the same central value.
    ensemble_peak_ratio: list[float]
    #: Interquartile spread at the peak, relative to the central value. Low
    #: means the members agree. Used to temper confidence, never the score.
    ensemble_spread: list[float]
    live: bool


async def fetch_river_discharge(
    coords: Sequence[tuple[float, float]],
) -> DischargeBatch:
    lats = ",".join(f"{lat:.4f}" for _, lat in coords[:_MAX_BATCH])
    lngs = ",".join(f"{lng:.4f}" for lng, _ in coords[:_MAX_BATCH])

    result = await get_json(
        settings.open_meteo_flood_url,
        {
            "latitude": lats,
            "longitude": lngs,
            "daily": (
                "river_discharge,river_discharge_mean,river_discharge_max,"
                "river_discharge_p25,river_discharge_p75"
            ),
            "forecast_days": 7,
        },
        cache_key=f"om:flood:{lats}",
    )

    n = len(coords)
    discharge = [0.0] * n
    anomaly = [1.0] * n
    peak_ratio = [1.0] * n
    peak_day = [0] * n
    ensemble_peak = [1.0] * n
    spread = [0.0] * n

    for i, block in enumerate(_as_list(result.data)):
        if i >= n:
            break
        daily = block.get("daily") or {}
        series = [float(d) for d in (daily.get("river_discharge") or []) if d is not None]
        today = series[0] if series else 0.0
        mean_series = [m for m in (daily.get("river_discharge_mean") or []) if m]
        mean = sum(mean_series) / len(mean_series) if mean_series else 0.0
        discharge[i] = float(today)
        anomaly[i] = float(today / mean) if mean > 0 else 1.0

        if series and mean > 0:
            highest = max(series)
            peak_ratio[i] = highest / mean
            peak_day[i] = series.index(highest)

            upper = [float(m) for m in (daily.get("river_discharge_max") or []) if m is not None]
            if upper:
                ensemble_peak[i] = max(upper) / mean

            # Spread is read at the forecast peak rather than averaged over the
            # week: the only day whose uncertainty matters is the bad one.
            p25 = [float(m) for m in (daily.get("river_discharge_p25") or []) if m is not None]
            p75 = [float(m) for m in (daily.get("river_discharge_p75") or []) if m is not None]
            d = peak_day[i]
            if len(p25) > d and len(p75) > d and highest > 0:
                spread[i] = max(0.0, (p75[d] - p25[d]) / highest)

    return DischargeBatch(
        discharge, anomaly, peak_ratio, peak_day, ensemble_peak, spread,
        live=result.live,
    )


@dataclass(slots=True)
class AirBatch:
    pm25: list[float]
    pm10: list[float]
    aqi: list[float]
    live: bool


async def fetch_air_quality(coords: Sequence[tuple[float, float]]) -> AirBatch:
    lats = ",".join(f"{lat:.4f}" for _, lat in coords[:_MAX_BATCH])
    lngs = ",".join(f"{lng:.4f}" for lng, _ in coords[:_MAX_BATCH])

    result = await get_json(
        settings.open_meteo_air_url,
        {
            "latitude": lats,
            "longitude": lngs,
            "hourly": "pm2_5,pm10",
            "current": "pm2_5,pm10,us_aqi",
            "forecast_days": 2,
            "timezone": "Asia/Kolkata",
        },
        cache_key=f"om:air:{lats}",
    )

    n = len(coords)
    pm25 = [0.0] * n
    pm10 = [0.0] * n
    aqi = [0.0] * n
    for i, block in enumerate(_as_list(result.data)):
        if i >= n:
            break
        current = block.get("current") or {}
        pm25[i] = float(current.get("pm2_5") or 0.0)
        pm10[i] = float(current.get("pm10") or 0.0)
        aqi[i] = float(current.get("us_aqi") or 0.0)

    return AirBatch(pm25, pm10, aqi, live=result.live)
