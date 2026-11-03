"""Operating regions inside one tenant.

Pune and Ghaziabad (IPEC, Sahibabad) share city_id 'pune' (see migration 026)
but are 1,200 km apart. The console shows one at a time, and a simulation
started for one only creates reports there. Longitude 75.5 E splits them
cleanly; `region_of` mirrors the frontend's `regionOf` in routes/admin/zones.ts.
"""
from __future__ import annotations

import math

REGIONS: dict[str, dict] = {
    "pune": {"name": "Pune", "center": (73.86, 18.55)},
    "ncr": {"name": "Ghaziabad · IPEC", "center": (77.375, 28.662)},
}
SPLIT_LNG = 75.5
REACH_KM = 150.0


def valid(region: str | None) -> str | None:
    return region if region in REGIONS else None


def region_of(lng: float, lat: float) -> str | None:
    best, d = None, math.inf
    for rid, r in REGIONS.items():
        clng, clat = r["center"]
        k = _km(lng, lat, clng, clat)
        if k < d:
            best, d = rid, k
    return best if d <= REACH_KM else None


def sql(expr_lng: str) -> str:
    """SQL expression naming the region of a longitude expression."""
    return f"(case when {expr_lng} > {SPLIT_LNG} then 'ncr' else 'pune' end)"


def _km(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))
