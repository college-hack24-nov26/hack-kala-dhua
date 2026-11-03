"""Historical rain and wind (ERA5) and river discharge (GloFAS v4) for the district.

    python -m ml.fetch.meteo [--start 2015-01-01] [--end 2026-09-30]

Standard library only. Open-Meteo, non-commercial use without a key.
  * ERA5 hourly: precipitation, rain, wind speed and gusts at 10 m, cloud cover,
    from archive-api.open-meteo.com (0.25°, ~25 km). Per calendar year per point.
  * GloFAS v4 daily river discharge from flood-api.open-meteo.com (0.05°, ~5 km):
    reanalysis to July 2022, forecast-based after that (flagged in the output).
  * Historical weather *forecasts* (what the forecast said at the time, short
    lead) from historical-forecast-api.open-meteo.com, 2022 onward, hourly
    precipitation. Compared with ERA5 by ml.forecast_error to measure how wrong
    a real rain forecast is, so training stops inventing that error.
Writes data/raw/meteo/era5_<point>_<year>.json, data/raw/meteo/hfc_<point>_<year>.json
and data/raw/glofas/<point>.json.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from datetime import date

from ml.district import RAW, RIVER_POINTS, WEATHER_POINTS

UA = "DisruptionOps-research/1.0"
ERA5 = "https://archive-api.open-meteo.com/v1/archive"
FLOOD = "https://flood-api.open-meteo.com/v1/flood"
HFC = "https://historical-forecast-api.open-meteo.com/v1/forecast"
#: The historical-forecast archive starts in 2022 for the stitched best-match.
HFC_FIRST_YEAR = 2022
HOURLY = "precipitation,rain,wind_speed_10m,wind_gusts_10m,cloud_cover"


def get(url: str, params: dict) -> dict:
    q = url + "?" + urllib.parse.urlencode(params)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(q, headers={"User-Agent": UA}), timeout=120) as r:
                return json.load(r)
        except Exception as exc:  # noqa: BLE001
            print(f"  retry {attempt + 1}: {exc}", flush=True)
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(q)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default="2026-09-30")
    ap.add_argument("--only", choices=["era5", "glofas", "hfc"], default=None)
    a = ap.parse_args()
    y0, y1 = int(a.start[:4]), int(a.end[:4])

    if a.only in (None, "era5"):
        out = RAW / "meteo"
        out.mkdir(parents=True, exist_ok=True)
        for name, (lon, lat) in WEATHER_POINTS.items():
            for y in range(y0, y1 + 1):
                path = out / f"era5_{name}_{y}.json"
                if path.exists():
                    continue
                s = max(a.start, f"{y}-01-01")
                e = min(a.end, f"{y}-12-31", date.today().isoformat())
                d = get(ERA5, {"latitude": lat, "longitude": lon, "start_date": s, "end_date": e,
                               "hourly": HOURLY, "timezone": "Asia/Kolkata", "models": "era5"})
                path.write_text(json.dumps(d))
                print(f"era5 {name} {y}: {len(d.get('hourly', {}).get('time', []))} hours", flush=True)
                time.sleep(1)

    if a.only in (None, "hfc"):
        out = RAW / "meteo"
        out.mkdir(parents=True, exist_ok=True)
        for name, (lon, lat) in WEATHER_POINTS.items():
            for y in range(max(y0, HFC_FIRST_YEAR), y1 + 1):
                path = out / f"hfc_{name}_{y}.json"
                if path.exists() and y < date.today().year:
                    continue
                s = max(a.start, f"{y}-01-01")
                e = min(a.end, f"{y}-12-31", date.today().isoformat())
                d = get(HFC, {"latitude": lat, "longitude": lon, "start_date": s, "end_date": e,
                              "hourly": "precipitation", "timezone": "Asia/Kolkata"})
                path.write_text(json.dumps(d))
                vals = [v for v in d.get("hourly", {}).get("precipitation", []) if v is not None]
                print(f"hfc {name} {y}: {len(vals)} hours with a forecast", flush=True)
                time.sleep(1)

    if a.only in (None, "glofas"):
        out = RAW / "glofas"
        out.mkdir(parents=True, exist_ok=True)
        for name, (lon, lat) in RIVER_POINTS.items():
            path = out / f"{name}.json"
            d = get(FLOOD, {"latitude": lat, "longitude": lon, "start_date": a.start, "end_date": a.end,
                            "daily": "river_discharge", "models": "seamless_v4"})
            path.write_text(json.dumps(d))
            print(f"glofas {name}: {len(d.get('daily', {}).get('time', []))} days "
                  f"(cell {d.get('latitude')},{d.get('longitude')})", flush=True)
            time.sleep(1)


if __name__ == "__main__":
    main()
