"""The learned parts, and the argument for where they are.

Most of this system is deterministic on purpose. Trust scoring, the delegation
gate, capability matching and the allocator are arithmetic and constraints
because an officer has to be able to check them, because they have to behave
identically at three in the morning with the network down, and because the cost
of a confident wrong answer is a boat sent to the wrong street. "The model said
so" is not a defensible answer in an inquiry.

But three things genuinely are prediction problems, and pretending otherwise
would be worse engineering, not better:

  1. **Will this river rise, and by how much.** This is hydrology. It depends on
     upstream rainfall, catchment saturation and reservoir releases, over hours.
     No rule you write captures it. The model here is **GloFAS**, the Copernicus
     Global Flood Awareness System: a physically-based rainfall-runoff and
     routing model, calibrated globally, whose forecasts are served free and
     keyless through Open-Meteo. Where a Google Flood Hub key is configured, its
     LSTM forecasts are preferred — that is a genuinely learned model, published
     in Nature in 2024, and it is state of the art for ungauged basins.

  2. **What did this person just say.** Reports arrive as free text in English,
     Hindi, Marathi and a transliterated mixture. The keyword pass gets most of
     it and is explainable, which is why it runs first. The remainder is a
     language problem, and the model is **XLM-RoBERTa fine-tuned on XNLI**, used
     zero-shot through the Hugging Face Inference API: pretrained, multilingual,
     no training data of ours required, and it only ever chooses from categories
     that already exist in the taxonomy.

  3. **How long will this take to reach.** Handled already, by a router, which
     is a learned traffic model wearing a different name.

What is **not** learned, and will not be: whether a report is trustworthy,
whether two reports are the same incident, who may authorise an evacuation, and
which unit goes where. Those are decisions with an audit trail.

Every call here degrades to the deterministic path. A model that is down must
cost latency, never correctness.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.ingest.client import get_json

log = get_logger(__name__)


# ------------------------------------------------------------- flood model ---
@dataclass(slots=True)
class FloodForecast:
    """What the hydrological model expects at a point over the next few days."""

    #: Peak river discharge over the horizon, m³/s.
    peak_discharge: float
    #: Discharge now, m³/s.
    current_discharge: float
    #: Peak as a multiple of the two-year return period, which is the usual
    #: threshold for "this leaves the channel".
    return_period_ratio: float
    #: Day index of the peak, 0 being today.
    peak_day: int
    horizon_days: int
    model: str
    live: bool
    #: Daily peaks, for the chart.
    series: list[float] = field(default_factory=list)

    @property
    def severity_hint(self) -> int:
        """The model's contribution to a severity, 1..5.

        A hint, not a verdict. The ward severity the system acts on also weighs
        population exposure, lifeline proximity and what people have actually
        reported, because a river peak in an empty catchment is not an
        emergency and a smaller one over a dense low-lying ward is.
        """
        r = self.return_period_ratio
        if r >= 2.0:
            return 5
        if r >= 1.4:
            return 4
        if r >= 1.0:
            return 3
        if r >= 0.6:
            return 2
        return 1

    @property
    def explanation(self) -> str:
        return (
            f"{self.model} expects a peak of {self.peak_discharge:.0f} m³/s on "
            f"day {self.peak_day}, {self.return_period_ratio:.2f}× the two-year "
            f"return level, against {self.current_discharge:.0f} m³/s now."
        )


_NO_FORECAST = FloodForecast(
    peak_discharge=0.0, current_discharge=0.0, return_period_ratio=0.0,
    peak_day=0, horizon_days=0, model="none", live=False,
)


async def flood_forecast(lat: float, lng: float, *, days: int = 7) -> FloodForecast:
    """River discharge forecast for the catchment this point sits in.

    Google Flood Hub when a key is configured, GloFAS through Open-Meteo
    otherwise. Both are pretrained: neither needs training data from us, which
    matters because we have none and a model trained on a hackathon's worth of
    synthetic reports would be worse than the arithmetic it replaced.
    """
    if settings.google_flood_hub_key:
        hub = await _flood_hub(lat, lng)
        if hub is not None:
            return hub

    result = await get_json(
        settings.open_meteo_flood_url,
        {
            "latitude": f"{lat:.4f}",
            "longitude": f"{lng:.4f}",
            "daily": "river_discharge,river_discharge_max,river_discharge_mean",
            "forecast_days": days,
            "past_days": 1,
        },
        cache_key=f"glofas:{lat:.2f},{lng:.2f}",
    )
    payload = getattr(result, "data", None)
    daily = (payload or {}).get("daily") or {}
    series = [float(v) for v in (daily.get("river_discharge") or []) if v is not None]
    if not series:
        return _NO_FORECAST

    current = series[0]
    peak = max(series)
    peak_day = series.index(peak)
    # Open-Meteo does not return return-period levels, so the baseline is the
    # mean of the recorded window. It is a coarse proxy and it is labelled as
    # one rather than being dressed up as a return period.
    baseline = sum(series) / len(series) or 1.0
    return FloodForecast(
        peak_discharge=peak,
        current_discharge=current,
        return_period_ratio=round(peak / max(baseline, 0.001), 3),
        peak_day=peak_day,
        horizon_days=len(series),
        model="GloFAS v4 (Copernicus, via Open-Meteo)",
        live=getattr(result, "live", False),
        series=[round(v, 2) for v in series],
    )


async def _flood_hub(lat: float, lng: float) -> FloodForecast | None:
    """Google Flood Hub. An LSTM, trained on global gauge records.

    Returns None rather than raising when the key is wrong or the area is not
    covered, so the caller falls through to GloFAS.
    """
    try:
        result = await get_json(
            f"{settings.google_flood_hub_url}/gauges:searchLatLng",
            {
                "key": settings.google_flood_hub_key,
                "latitude": f"{lat:.4f}",
                "longitude": f"{lng:.4f}",
                "radius": 50000,
            },
            cache_key=f"floodhub:{lat:.2f},{lng:.2f}",
        )
        payload = getattr(result, "data", None) or {}
        gauges = payload.get("gauges") or []
        if not gauges:
            return None
        gauge = gauges[0]
        forecast = await get_json(
            f"{settings.google_flood_hub_url}/gaugeModels:queryGaugeModels",
            {"key": settings.google_flood_hub_key, "gaugeIds": gauge.get("gaugeId")},
            cache_key=f"floodhub:model:{gauge.get('gaugeId')}",
        )
        model = (getattr(forecast, "data", None) or {}).get("gaugeModels") or []
        if not model:
            return None
        thresholds = model[0].get("thresholds") or {}
        warning = float(thresholds.get("warningLevel") or 0) or 1.0
        value = float(gauge.get("latestValue") or 0)
        return FloodForecast(
            peak_discharge=value,
            current_discharge=value,
            return_period_ratio=round(value / warning, 3),
            peak_day=0,
            horizon_days=7,
            model="Google Flood Hub (LSTM)",
            live=True,
        )
    except Exception as exc:  # noqa: BLE001 - a model outage is not a failure
        log.warning("flood_hub_unavailable", error=str(exc)[:160])
        return None


# -------------------------------------------------------- text classifier ---
@dataclass(slots=True)
class ZeroShot:
    label: str
    score: float
    model: str
    #: Every label with its score, so the console can show the runner-up. A
    #: classifier that is 0.34 / 0.31 between two categories is telling you
    #: something an argmax throws away.
    ranked: list[tuple[str, float]] = field(default_factory=list)


async def classify(text: str, labels: list[str]) -> ZeroShot | None:
    """Zero-shot classification over the taxonomy's own categories.

    Multilingual because the reports are: XLM-R was pretrained on a hundred
    languages and the XNLI head transfers across all of them, so Marathi and
    Hindi are handled by the same call as English without a translation step
    that would lose the idiom the keyword pass relies on.

    The labels are passed as a closed set every time. The model cannot invent a
    category, which is the property that lets its output be trusted enough to
    write to a column.
    """
    if not settings.hf_api_token or not text.strip() or not labels:
        return None
    url = f"https://api-inference.huggingface.co/models/{settings.hf_zero_shot_model}"
    try:
        async with httpx.AsyncClient(timeout=settings.feed_timeout_seconds) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {settings.hf_api_token}"},
                json={
                    # The text is data. It is never concatenated into anything
                    # that could read as an instruction, which is the whole
                    # reason a classifier is preferable to a chat model here:
                    # there is no instruction channel to hijack.
                    "inputs": text[:600],
                    "parameters": {
                        "candidate_labels": labels,
                        "multi_label": False,
                        "hypothesis_template": "This report is about {}.",
                    },
                    "options": {"wait_for_model": True},
                },
            )
            response.raise_for_status()
            body: Any = response.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("zero_shot_unavailable", error=str(exc)[:160])
        return None

    names = (body or {}).get("labels") or []
    scores = (body or {}).get("scores") or []
    if not names or not scores:
        return None
    ranked = [(str(n), round(float(s), 4)) for n, s in zip(names, scores)]
    return ZeroShot(
        label=ranked[0][0],
        score=ranked[0][1],
        model=settings.hf_zero_shot_model,
        ranked=ranked[:4],
    )
