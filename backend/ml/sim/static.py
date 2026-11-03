"""Per-segment static features shared by the simulator, the models and the router.

    from ml.sim.static import load_static
    st = load_static()          # cached in data/processed/graph/static.parquet

Everything here comes from the graph (OSM), terrain (DEM) and OSM context
(land use, amenities). Columns, one row per segment, in segments.parquet order:

    seg_id, length_m, highway, cls (main | minor | local | path), drivable,
    walkable, bus_ok, free_kmh, signals, bridge, river_bridge, low_bridge,
    underpass, maxweight_t, width_m, x, y (UTM 43N metres), lon, lat,
    elev_m, slope_pct, hand_m, dist_river_m, dist_water_m, sink_m, pond_m,
    river, cell, bank_m, industrial, military, hospital_m, shelter_m,
    density, ward_id, sector_id

`pond_m` is sink depth beyond 1.5 m. A surface model (GLO-30 measures roofs and
canopy too) makes a road between buildings look sunk by a metre or more,
so only the excess is treated as a real low spot (assumption; 26% of segments
have some). Underpasses are handled on their own: over a tunnel the DEM sees
the deck above, so their sink is meaningless.

Adjacency (segments sharing a junction) is a sparse matrix in static_adj.npz.
"""
from __future__ import annotations

import json
from functools import lru_cache

import numpy as np
import pandas as pd
from pyproj import Transformer
from scipy import sparse
from scipy.spatial import cKDTree
from shapely.geometry import Point, Polygon
from shapely.strtree import STRtree

from ml.district import PAVANA_SPLIT_LON, PROCESSED, RAW, RIVER_CELL, RIVER_POINTS, WARD_SEEDS

TO_UTM = Transformer.from_crs("EPSG:4326", "EPSG:32643", always_xy=True)
MAIN = {"motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
        "secondary", "secondary_link", "busway"}
MINOR = {"tertiary", "tertiary_link", "unclassified", "road"}
LOCAL = {"residential", "living_street", "service", "track"}
CELLS = list(RIVER_POINTS)
GRAPH = PROCESSED / "graph"


def _sector(ward: str) -> str:
    from app.nav.sectors import sector_of  # the same mapping the agent logs use
    return sector_of(ward) or ""


def _context():
    d = json.loads((RAW / "osm" / "context.json").read_text())
    coords = {e["id"]: (e["lon"], e["lat"]) for e in d["elements"] if e["type"] == "node"}
    polys: dict[str, list[Polygon]] = {"industrial": [], "military": []}
    pts: dict[str, list[tuple[float, float]]] = {"hospital": [], "shelter": []}
    for e in d["elements"]:
        t = e.get("tags", {})
        if e["type"] == "node":
            ll = (e["lon"], e["lat"])
        elif e["type"] == "way":
            ring = [coords[n] for n in e.get("nodes", []) if n in coords]
            if len(ring) < 3:
                continue
            xs, ys = TO_UTM.transform([p[0] for p in ring], [p[1] for p in ring])
            poly = Polygon(list(zip(xs, ys))).buffer(0)
            lu = t.get("landuse")
            if lu in polys:
                polys[lu].append(poly)
            ll = (float(np.mean([p[0] for p in ring])), float(np.mean([p[1] for p in ring])))
        else:
            continue
        am = t.get("amenity")
        if am == "hospital" or t.get("building") == "hospital":
            pts["hospital"].append(ll)
        elif am in ("school", "college", "community_centre"):
            pts["shelter"].append(ll)
    return polys, pts


def build() -> pd.DataFrame:
    seg = pd.read_parquet(GRAPH / "segments.parquet")
    ter = pd.read_parquet(GRAPH / "terrain.parquet")
    df = seg.merge(ter, on="seg_id", how="left")
    df["river"] = df["river"].fillna("Pavana")
    for c in ("elev_m", "slope_pct", "hand_m", "dist_river_m", "dist_water_m", "sink_m"):
        df[c] = df[c].fillna(df[c].median())
    x, y = TO_UTM.transform(df["mid_lon"].to_numpy(), df["mid_lat"].to_numpy())
    df["x"], df["y"] = x, y
    df["lon"], df["lat"] = df["mid_lon"], df["mid_lat"]
    hw = df["highway"]
    df["cls"] = np.where(hw.isin(MAIN), "main", np.where(hw.isin(MINOR), "minor",
                         np.where(hw.isin(LOCAL), "local", "path")))
    df["river_bridge"] = df["bridge"] & (df["dist_river_m"] < 80)
    df["low_bridge"] = df["river_bridge"] & ~hw.isin({"motorway", "trunk", "primary", "trunk_link", "primary_link"})
    df["pond_m"] = (df["sink_m"] - 1.5).clip(0, 4)

    cell = df["river"].map(RIVER_CELL).fillna("pawana_chinchwad")
    up = (df["river"] == "Pavana") & (df["lon"] < PAVANA_SPLIT_LON)
    cell = cell.where(~up, "pawana_ravet")
    df["cell"] = cell.map({c: i for i, c in enumerate(CELLS)}).astype("int8")
    near = (df["dist_river_m"] < 100) & ~df["bridge"]
    # Bank height: the 10th percentile of HAND within 100 m of the river, so at
    # bankfull only the lowest riverside roads (ghats, causeway approaches) are wet.
    bank = df[near].groupby("river")["hand_m"].quantile(0.10)
    df["bank_m"] = df["river"].map(bank).fillna(float(bank.median()))

    polys, pts = _context()
    mids = [Point(a, b) for a, b in zip(x, y)]
    for k in ("industrial", "military"):
        inside = np.zeros(len(df), bool)
        if polys[k]:
            tree = STRtree(polys[k])
            hits = tree.query(mids, predicate="within")
            inside[np.unique(hits[0])] = True
        df[k] = inside
    for k in ("hospital", "shelter"):
        if pts[k]:
            px, py = TO_UTM.transform([p[0] for p in pts[k]], [p[1] for p in pts[k]])
            dist, _ = cKDTree(np.c_[px, py]).query(np.c_[x, y])
        else:
            dist = np.full(len(df), 1e5)
        df[f"{k}_m"] = dist.round(0)

    kd = cKDTree(np.c_[x, y])
    cnt = np.array([len(v) for v in kd.query_ball_point(np.c_[x, y], r=300)])
    df["density"] = np.clip(cnt / np.percentile(cnt, 95), 0, 1).round(3)

    ids = list(WARD_SEEDS)
    _, wi = cKDTree(np.array([WARD_SEEDS[i] for i in ids])).query(np.c_[df["lon"], df["lat"]])
    df["ward_id"] = [ids[i] for i in wi]
    df["sector_id"] = df["ward_id"].map(_sector)

    # adjacency through shared junctions
    node = pd.concat([pd.DataFrame({"i": np.arange(len(df)), "n": df["u"]}),
                      pd.DataFrame({"i": np.arange(len(df)), "n": df["v"]})])
    codes, _ = pd.factorize(node["n"])
    inc = sparse.csr_matrix((np.ones(len(node)), (node["i"].to_numpy(), codes)),
                            shape=(len(df), codes.max() + 1))
    adj = (inc @ inc.T).tocsr()
    adj.setdiag(0)
    adj.eliminate_zeros()
    adj.data[:] = 1
    sparse.save_npz(GRAPH / "static_adj.npz", adj.astype(np.float32))

    keep = ["seg_id", "length_m", "highway", "cls", "drivable", "walkable", "bus_ok", "free_kmh",
            "signals", "bridge", "river_bridge", "low_bridge", "underpass", "maxweight_t", "width_m",
            "x", "y", "lon", "lat", "elev_m", "slope_pct", "hand_m", "dist_river_m", "dist_water_m",
            "sink_m", "pond_m", "river", "cell", "bank_m", "industrial", "military", "hospital_m",
            "shelter_m", "density", "ward_id", "sector_id"]
    out = df[keep].reset_index(drop=True)
    out.to_parquet(GRAPH / "static.parquet", index=False)
    return out


@lru_cache(maxsize=1)
def load_static() -> pd.DataFrame:
    p = GRAPH / "static.parquet"
    if not p.exists():
        return build()
    return pd.read_parquet(p)


@lru_cache(maxsize=1)
def load_adj() -> sparse.csr_matrix:
    if not (GRAPH / "static_adj.npz").exists():
        build()
    return sparse.load_npz(GRAPH / "static_adj.npz").tocsr()


if __name__ == "__main__":
    s = build()
    print(len(s), "segments")
    print(s[["cls", "industrial", "military", "river_bridge", "low_bridge", "underpass"]].apply(
        lambda c: c.value_counts().to_dict()).to_string())
    print(s.groupby("river")["bank_m"].first())
    print(s.groupby("sector_id").size())
    print((s["pond_m"] > 0).sum(), "segments with pond_m > 0")
