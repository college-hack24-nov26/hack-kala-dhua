"""Satellite evidence: Copernicus Global Flood Monitoring (GFM) on Sentinel-1 SAR, plus the radar image itself.

For a point (an incident), find the most recent Sentinel-1 pass that GFM processed over it and read, at that
pixel, what the ensemble of three flood algorithms (DLR, LIST, TUW) concluded:

* ``flood``        1 flooded, 0 not flooded (pixels normally under water are excluded: Reference Water Mask)
* ``likelihood``   0-100 % that the flood classification is right
* ``excluded``     1 where SAR cannot map floods reliably (no sensitivity e.g. dense built-up, water look-alikes,
                   radar shadow, low coverage): **not observable** — neither support nor contradiction
* ``reference``    1 permanent / seasonal water

Free and open, no account: EODC STAC (catalogue), EODC TiTiler (pixel values and rendered crops), Microsoft
Planetary Computer (Sentinel-1 RTC radar backscatter crop of the same pass). The rendered crops are what an
officer sees in the audit trail: the actual radar image (VV, dB), the flood mask and the likelihood map, with
the values extracted at the incident pixel.

Evidence rules (fusion modality "satellite"): flooded -> support, weight by likelihood; observable and dry ->
contradiction (weaker: a flood may have come after the pass); excluded or no pass -> nothing. Satellite
evidence decays with a 24 h half-life (a pass is days apart, not minutes).
"""
from __future__ import annotations

import asyncio
import math
import time
from typing import Any
from urllib.parse import quote

import httpx

from app.core.logging import get_logger

log = get_logger(__name__)

STAC = "https://stac.eodc.eu/api/v1/search"
TITILER = "https://titiler.services.eodc.eu/cog"
PC_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
PC_DATA = "https://planetarycomputer.microsoft.com/api/data/v1"
NODATA = 255.0
LOOKBACK_DAYS = 21
CROP_DEG = 0.012              # ~1.3 km square around the incident
CACHE_S = 1800.0
WATER_CATEGORIES = {"person_stranded", "flooded_road", "waterlogging", "flood"}
_cache: dict[tuple[float, float], tuple[float, dict]] = {}
_lock = asyncio.Lock()


def _key(lng: float, lat: float) -> tuple[float, float]:
    return (round(lng, 3), round(lat, 3))


def crop_url(cog_href: str, lng: float, lat: float, *, rescale: str, colormap: str, size: int = 384) -> str:
    d = CROP_DEG
    bbox = f"{lng - d:.5f},{lat - d:.5f},{lng + d:.5f},{lat + d:.5f}"
    return (f"{TITILER}/bbox/{bbox}/{size}x{size}.png?url={quote(cog_href, safe='')}"
            f"&rescale={rescale}&colormap_name={colormap}&nodata=255&return_mask=true")


def sar_url(item_id: str, lng: float, lat: float, size: int = 384) -> str:
    d = CROP_DEG
    bbox = f"{lng - d:.5f},{lat - d:.5f},{lng + d:.5f},{lat + d:.5f}"
    return (f"{PC_DATA}/item/bbox/{bbox}/{size}x{size}.png?collection=sentinel-1-rtc&item={item_id}"
            f"&expression=10*log10(vv)&rescale=-22,8&colormap_name=gray&asset_as_band=true")


async def _point(client: httpx.AsyncClient, href: str, lng: float, lat: float) -> float | None:
    try:
        r = await client.get(f"{TITILER}/point/{lng},{lat}", params={"url": href}, timeout=25)
        if r.status_code != 200:
            return None
        v = (r.json().get("values") or [None])[0]
        return None if v is None else float(v)
    except (httpx.HTTPError, ValueError):
        return None


async def _sar_item(client: httpx.AsyncClient, acquired: str, lng: float, lat: float) -> str | None:
    """The Planetary Computer Sentinel-1 RTC scene of the same pass (same minute)."""
    try:
        t = acquired.replace("Z", "+00:00")
        from datetime import datetime, timedelta
        dt = datetime.fromisoformat(t)
        window = f"{(dt - timedelta(minutes=3)).isoformat()}/{(dt + timedelta(minutes=3)).isoformat()}"
        r = await client.post(PC_STAC, json={"collections": ["sentinel-1-rtc"], "datetime": window,
                                             "intersects": {"type": "Point", "coordinates": [lng, lat]}, "limit": 1}, timeout=25)
        feats = r.json().get("features") or []
        return feats[0]["id"] if feats else None
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return None


def cached(lng: float, lat: float) -> dict | None:
    hit = _cache.get(_key(lng, lat))
    return hit[1] if hit else None


async def observe(lng: float, lat: float, *, force: bool = False) -> dict:
    """Latest GFM reading at a point, with image URLs. Cached 30 min per ~100 m cell. Never raises."""
    k = _key(lng, lat)
    hit = _cache.get(k)
    if hit and not force and time.time() - hit[0] < CACHE_S:
        return hit[1]
    async with _lock:                                   # one satellite lookup at a time (polite to free services)
        hit = _cache.get(k)
        if hit and not force and time.time() - hit[0] < CACHE_S:
            return hit[1]
        out = await _observe(lng, lat)
        _cache[k] = (time.time(), out)
        if len(_cache) > 400:
            _cache.pop(next(iter(_cache)))
        return out


async def _observe(lng: float, lat: float) -> dict:
    base: dict[str, Any] = {"source": "Copernicus GFM · Sentinel-1 SAR (C-band, 20 m)", "lng": lng, "lat": lat,
                            "status": "no_pass", "checked_at": time.time()}
    try:
        async with httpx.AsyncClient(headers={"User-Agent": "Indradhanu-DisruptionOps/1.0"}) as client:
            from datetime import UTC, datetime, timedelta
            since = (datetime.now(UTC) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
            r = await client.post(STAC, json={"collections": ["GFM"], "intersects": {"type": "Point", "coordinates": [lng, lat]},
                                              "datetime": f"{since}/..", "limit": 10,
                                              "sortby": [{"field": "datetime", "direction": "desc"}]}, timeout=30)
            items = r.json().get("features") or []
            base["passes_checked"] = len(items)
            for it in items:
                a = it.get("assets") or {}
                if "ensemble_flood_extent" not in a:
                    continue
                flood = await _point(client, a["ensemble_flood_extent"]["href"], lng, lat)
                if flood is None or flood == NODATA:
                    continue                            # this scene's swath does not cover the point
                vals = await asyncio.gather(
                    _point(client, a["ensemble_likelihood"]["href"], lng, lat) if "ensemble_likelihood" in a else asyncio.sleep(0),
                    _point(client, a["exclusion_mask"]["href"], lng, lat) if "exclusion_mask" in a else asyncio.sleep(0),
                    _point(client, a["reference_water_mask"]["href"], lng, lat) if "reference_water_mask" in a else asyncio.sleep(0),
                    _point(client, a["advisory_flags"]["href"], lng, lat) if "advisory_flags" in a else asyncio.sleep(0))
                likelihood, excluded, reference, advisory = [None if (v is None or v == NODATA) else v for v in vals]
                p = it.get("properties") or {}
                acquired = p.get("datetime")
                sar = await _sar_item(client, acquired, lng, lat) if acquired else None
                age_h = None
                if acquired:
                    age_h = (time.time() - datetime.fromisoformat(acquired.replace("Z", "+00:00")).timestamp()) / 3600
                observable = not (excluded and excluded >= 1)
                status = "flooded" if flood >= 1 else ("dry" if observable else "not_observable")
                images = [
                    {"key": "sar_vv", "label": "Sentinel-1 radar backscatter (VV, dB)", "level": "L1 RTC · C-band SAR",
                     "url": sar_url(sar, lng, lat) if sar else None,
                     "explain": "Dark = smooth surfaces (open water, bare ground); bright = buildings and rough terrain."},
                    {"key": "flood_mask", "label": "GFM observed flood extent", "level": "L3 ensemble (DLR+LIST+TUW)",
                     "url": crop_url(a["ensemble_flood_extent"]["href"], lng, lat, rescale="0,1", colormap="blues"),
                     "explain": "Blue = flooded this pass; transparent = no flood or excluded."},
                ]
                if "ensemble_likelihood" in a:
                    images.append({"key": "likelihood", "label": "Flood classification likelihood", "level": "L3 · 0-100 %",
                                   "url": crop_url(a["ensemble_likelihood"]["href"], lng, lat, rescale="0,100", colormap="viridis"),
                                   "explain": "How sure the three algorithms are, per 20 m pixel."})
                if "exclusion_mask" in a:
                    images.append({"key": "exclusion", "label": "Exclusion mask (not observable by SAR)", "level": "static + per-orbit",
                                   "url": crop_url(a["exclusion_mask"]["href"], lng, lat, rescale="0,1", colormap="reds"),
                                   "explain": "Red = SAR cannot map floods here (built-up, shadow, look-alikes)."})
                base.update({
                    "status": status, "item_id": it.get("id"), "scene": p.get("parent"), "sar_item": sar,
                    "acquired_at": acquired, "age_hours": round(age_h, 1) if age_h is not None else None,
                    "values": {"flood": flood, "likelihood": likelihood, "excluded": excluded, "reference_water": reference,
                               "advisory": advisory},
                    "scene_flooded_pixels": p.get("flooded_pixels"), "processing": p.get("processing:version"),
                    "images": [im for im in images if im["url"]],
                    "summary": _summary(status, likelihood, acquired),
                })
                return base
    except Exception as exc:  # noqa: BLE001 - satellites are context, never a reason to fail
        base.update(status="error", error=f"{type(exc).__name__}: {str(exc)[:160]}")
        log.warning("gfm_observe_failed", error=base["error"])
        return base
    base["summary"] = f"No Sentinel-1 pass processed by GFM over this point in the last {LOOKBACK_DAYS} days."
    return base


def _summary(status: str, likelihood: float | None, acquired: str | None) -> str:
    when = (acquired or "")[:16].replace("T", " ")
    if status == "flooded":
        return f"Sentinel-1 pass {when} UTC: flooded at this point ({likelihood or '?'}% likelihood)."
    if status == "dry":
        return f"Sentinel-1 pass {when} UTC: no flood at this point" + (f" ({likelihood:.0f}% likelihood)." if likelihood is not None else ".")
    return (f"Sentinel-1 pass {when} UTC: this spot is in GFM's exclusion mask (built-up or radar-limited), "
            "so the satellite cannot confirm or rule out a flood here.")


def evidence_for(obs: dict, category: str) -> dict | None:
    """Fusion inputs from an observation, or None when the satellite says nothing about this incident."""
    if category not in WATER_CATEGORIES or obs.get("status") not in ("flooded", "dry"):
        return None
    lik = (obs.get("values") or {}).get("likelihood")
    conf = (lik / 100.0) if isinstance(lik, (int, float)) else 0.7
    flooded = obs["status"] == "flooded"
    return {"polarity": 1 if flooded else -1, "confidence": conf if flooded else 0.5 * conf,
            "age_min": (obs.get("age_hours") or 0) * 60.0, "note": obs.get("summary") or "", "ref": obs.get("item_id")}


async def fetch_png(url: str) -> bytes | None:
    try:
        async with httpx.AsyncClient(headers={"User-Agent": "Indradhanu-DisruptionOps/1.0"}) as client:
            r = await client.get(url, timeout=60)
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/") and len(r.content) <= 3_500_000:
            return r.content
    except httpx.HTTPError:
        return None
    return None


def distance_ok(lat: float) -> bool:
    return -80 < lat < 80 and not math.isnan(lat)
