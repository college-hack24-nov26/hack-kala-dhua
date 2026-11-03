"""Terrain features per segment from the Copernicus 30 m DEM and OSM waterways.

    python -m ml.terrain        (after ml.graph and ml.fetch.dem)

For every segment:
    elev_m              road elevation: the lower quartile of DEM samples every
                        ~15 m along it (GLO-30 is a surface model, so the lower
                        samples are more likely the road than a roof beside it)
    slope_pct           (max - min) / length, capped
    river               name of the nearest named river (Pavana, Mula, Indrayani, Kasarsai)
    dist_river_m        distance to that river's centreline
    dist_water_m        distance to any waterway (rivers, streams, canals, drains)
    hand_m              height above the nearest river: elev_m minus the river's
                        water-surface elevation at its nearest point (the lowest
                        DEM value within 60 m of the river line there)
    sink_m              how far the road sits below its surroundings: mean DEM in
                        a 100-200 m ring minus elev_m, floored at 0. Rain ponds here.

Writes data/processed/graph/terrain.parquet and adds a summary. All lengths in
metres, in UTM zone 43N (EPSG:32643), which covers Pune.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import rasterio
from pyproj import Transformer
from rasterio.windows import from_bounds
from shapely.geometry import LineString
from shapely.strtree import STRtree

from ml.district import BBOX, PROCESSED, RAW
from ml.fetch.dem import TILE

TO_UTM = Transformer.from_crs("EPSG:4326", "EPSG:32643", always_xy=True)
RIVERS = {"pavana", "pavana river", "pawana", "mula", "mula river", "indrayani", "indrayani river", "kasarsai"}


def _waterways() -> tuple[list[LineString], list[str], list[LineString]]:
    d = json.loads((RAW / "osm" / "water.json").read_text())
    coords = {e["id"]: (e["lon"], e["lat"]) for e in d["elements"] if e["type"] == "node"}
    rivers, names, allw = [], [], []
    for e in d["elements"]:
        if e["type"] != "way" or "waterway" not in e.get("tags", {}):
            continue
        pts = [coords[n] for n in e["nodes"] if n in coords]
        if len(pts) < 2:
            continue
        xs, ys = TO_UTM.transform([p[0] for p in pts], [p[1] for p in pts])
        line = LineString(list(zip(xs, ys)))
        allw.append(line)
        nm = (e["tags"].get("name:en") or e["tags"].get("name") or "").strip().lower()
        if e["tags"]["waterway"] == "river" and nm in RIVERS:
            rivers.append(line)
            names.append(nm.replace(" river", "").replace("pawana", "pavana").title())
    return rivers, names, allw


def build() -> dict:
    seg = pd.read_parquet(PROCESSED / "graph" / "segments.parquet")
    w, s, e, n = BBOX
    pad = 0.01
    with rasterio.open(RAW / "dem" / f"{TILE}.tif") as src:
        win = from_bounds(w - pad, s - pad, e + pad, n + pad, src.transform)
        dem = src.read(1, window=win).astype("float32")
        tr = src.window_transform(win)
        nod = src.nodata
    if nod is not None:
        dem[dem == nod] = np.nan
    inv = ~tr

    def sample(lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
        cols, rows = inv * (lon, lat)
        r = np.clip(np.floor(rows).astype(int), 0, dem.shape[0] - 1)
        c = np.clip(np.floor(cols).astype(int), 0, dem.shape[1] - 1)
        return dem[r, c]

    rivers, rnames, allw = _waterways()
    rtree, wtree = STRtree(rivers), STRtree(allw)
    # Water-surface elevation along each river: lowest DEM within ~60 m of
    # points every 30 m on the line.
    to_ll = Transformer.from_crs("EPSG:32643", "EPSG:4326", always_xy=True)
    river_pts: list[tuple[float, float, float, str]] = []
    for line, nm in zip(rivers, rnames):
        L = line.length
        for d in np.arange(0, L, 30.0):
            p = line.interpolate(d)
            ring = [(p.x + dx, p.y + dy) for dx in (-60, -30, 0, 30, 60) for dy in (-60, -30, 0, 30, 60)]
            lo, la = to_ll.transform([q[0] for q in ring], [q[1] for q in ring])
            z = np.nanmin(sample(np.array(lo), np.array(la)))
            river_pts.append((p.x, p.y, float(z), nm))
    rp = np.array([(a, b, z) for a, b, z, _ in river_pts])
    rn = [x[3] for x in river_pts]
    from scipy.spatial import cKDTree

    kd = cKDTree(rp[:, :2])

    out = []
    for r in seg.itertuples():
        pts = json.loads(r.coords)
        lon = np.array([p[0] for p in pts]); lat = np.array([p[1] for p in pts])
        xs, ys = TO_UTM.transform(lon, lat)
        line = LineString(list(zip(xs, ys)))
        k = max(2, int(line.length // 15) + 1)
        dd = np.linspace(0, line.length, k)
        sx = np.array([line.interpolate(t).x for t in dd]); sy = np.array([line.interpolate(t).y for t in dd])
        slo, sla = to_ll.transform(sx, sy)
        z = sample(np.array(slo), np.array(sla))
        z = z[~np.isnan(z)]
        if len(z) == 0:
            out.append({"seg_id": r.seg_id}); continue
        elev = float(np.percentile(z, 25))
        slope = float(min(30.0, 100 * (z.max() - z.min()) / max(line.length, 1.0)))
        mid = line.interpolate(0.5, normalized=True)
        dist, idx = kd.query([mid.x, mid.y])
        river_z = rp[idx, 2]
        ri = rtree.nearest(mid)
        wi = wtree.nearest(mid)
        # ring 100-200 m around the midpoint for sink depth
        ang = np.linspace(0, 2 * np.pi, 16, endpoint=False)
        rx = np.concatenate([mid.x + 100 * np.cos(ang), mid.x + 200 * np.cos(ang)])
        ry = np.concatenate([mid.y + 100 * np.sin(ang), mid.y + 200 * np.sin(ang)])
        rlo, rla = to_ll.transform(rx, ry)
        ringz = sample(np.array(rlo), np.array(rla))
        sink = max(0.0, float(np.nanmean(ringz)) - elev)
        out.append({
            "seg_id": r.seg_id, "elev_m": round(elev, 2), "slope_pct": round(slope, 2),
            "river": rn[idx], "dist_river_m": round(float(rivers[ri].distance(mid)), 1),
            "dist_water_m": round(float(allw[wi].distance(mid)), 1),
            "hand_m": round(elev - river_z, 2), "sink_m": round(sink, 2),
        })
    terr = pd.DataFrame(out)
    path = PROCESSED / "graph" / "terrain.parquet"
    terr.to_parquet(path, index=False)
    summary = {
        "segments": int(len(terr)), "rivers": sorted(set(rnames)), "river_points": len(river_pts),
        "hand_m": terr["hand_m"].describe(percentiles=[.05, .25, .5, .75, .95]).round(2).to_dict(),
        "sink_m": terr["sink_m"].describe(percentiles=[.5, .9, .99]).round(2).to_dict(),
        "dist_river_m": terr["dist_river_m"].describe(percentiles=[.05, .5, .95]).round(1).to_dict(),
        "segments_within_3m_of_river_level": int((terr["hand_m"] < 3).sum()),
        "dem": "Copernicus GLO-30 (surface model; road = lower-quartile sample)",
    }
    (PROCESSED / "graph" / "terrain_summary.json").write_text(json.dumps(summary, indent=1))
    return summary


if __name__ == "__main__":
    print(json.dumps(build(), indent=1))
