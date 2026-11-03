"""Bounded road graph for the district: OSM ways split into segments.

    python -m ml.graph          (from backend/, after ml.fetch.osm)

A *segment* is a stretch of one OSM way between two junctions (or ends). It is
the unit everything else is about: the passability model predicts per segment,
the ETA model times per segment, the simulator floods per segment, and routes
are lists of segment ids.

Ids are `"{way_id}:{k}"` (k-th piece of that way), which is stable as long as
the OSM extract is the same one; the manifest records the OSM timestamp.

Writes:
    data/processed/graph/segments.parquet   one row per segment (both directions
                                            are a property, not two rows)
    data/processed/graph/nodes.parquet      junctions: id, lon, lat, signal, degree
    data/processed/graph/segments.geojson   drivable segments, for the map
    data/processed/graph/summary.json       counts and assumptions

Free-flow speeds by road class are assumptions for Indian urban roads (km/h),
used only where OSM has no `maxspeed`; the ETA model learns real speeds.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

from ml.district import BBOX, PROCESSED, RAW

#: highway=* values we keep, with (drivable, walkable, default speed km/h).
CLASSES: dict[str, tuple[bool, bool, float]] = {
    "motorway": (True, False, 60), "motorway_link": (True, False, 40),
    "trunk": (True, True, 50), "trunk_link": (True, True, 35),
    "primary": (True, True, 40), "primary_link": (True, True, 30),
    "secondary": (True, True, 35), "secondary_link": (True, True, 28),
    "tertiary": (True, True, 30), "tertiary_link": (True, True, 25),
    "unclassified": (True, True, 25), "residential": (True, True, 20),
    "living_street": (True, True, 10), "service": (True, True, 15),
    "busway": (True, False, 35), "track": (True, True, 12),
    "road": (True, True, 20),
    "footway": (False, True, 0), "path": (False, True, 0), "pedestrian": (False, True, 0),
    "steps": (False, True, 0), "cycleway": (False, True, 0), "corridor": (False, True, 0),
}
#: Classes a bus may use (no narrow lanes, no tracks).
BUS_OK = {"motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
          "secondary", "secondary_link", "tertiary", "tertiary_link", "busway", "unclassified"}
WALK_KMH = 4.5


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def _num(v: str | None) -> float | None:
    if not v:
        return None
    m = re.match(r"\s*([\d.]+)", str(v))
    return float(m.group(1)) if m else None


def _layer(v: str | None) -> int:
    try:
        return int(str(v).split(";")[0].strip())
    except (TypeError, ValueError):
        return 0


def _oneway(tags: dict) -> int:
    """1 forward only, -1 backward only, 0 both."""
    ow = tags.get("oneway", "")
    if ow in ("yes", "true", "1"):
        return 1
    if ow == "-1":
        return -1
    if tags.get("junction") in ("roundabout", "circular") and ow != "no":
        return 1
    if tags.get("highway") in ("motorway", "motorway_link") and ow != "no":
        return 1
    return 0


def load(path: Path) -> tuple[dict[int, tuple[float, float]], dict[int, dict], list[dict]]:
    els = json.loads(path.read_text())["elements"]
    coords: dict[int, tuple[float, float]] = {}
    ntags: dict[int, dict] = {}
    ways: list[dict] = []
    for e in els:
        if e["type"] == "node":
            coords[e["id"]] = (e["lon"], e["lat"])
            if e.get("tags"):
                ntags.setdefault(e["id"], {}).update(e["tags"])
        elif e["type"] == "way" and e.get("tags", {}).get("highway") in CLASSES:
            ways.append(e)
    return coords, ntags, ways


def build() -> dict:
    coords, ntags, ways = load(RAW / "osm" / "roads.json")
    w, s, e, n = BBOX
    inside = lambda p: w <= p[0] <= e and s <= p[1] <= n  # noqa: E731

    use = Counter()
    for way in ways:
        nds = way["nodes"]
        for i, nid in enumerate(nds):
            use[nid] += 1 if 0 < i < len(nds) - 1 else 2   # ends always split

    rows: list[dict] = []
    deg = Counter()
    for way in ways:
        t = way.get("tags", {})
        hw = t["highway"]
        drive, walk, v0 = CLASSES[hw]
        access = t.get("access", "")
        motor = t.get("motor_vehicle", t.get("motorcar", ""))
        if access in ("no",) and hw not in ("footway", "path"):
            drive = False
        if motor == "no":
            drive = False
        nds = [nd for nd in way["nodes"] if nd in coords]
        if len(nds) < 2:
            continue
        cut = [0] + [i for i in range(1, len(nds) - 1) if use[nds[i]] >= 2] + [len(nds) - 1]
        for k in range(len(cut) - 1):
            part = nds[cut[k]: cut[k + 1] + 1]
            pts = [coords[nd] for nd in part]
            if not any(inside(p) for p in pts):
                continue
            length = sum(haversine_m(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
            if length < 0.5:
                continue
            signals = sum(1 for nd in part[1:] if ntags.get(nd, {}).get("highway") == "traffic_signals")
            speed = _num(t.get("maxspeed")) or v0
            u, v = part[0], part[-1]
            deg[u] += 1
            deg[v] += 1
            rows.append({
                "seg_id": f"{way['id']}:{k}", "way_id": way["id"], "u": u, "v": v,
                "highway": hw, "name": t.get("name") or t.get("ref") or "",
                "oneway": _oneway(t), "length_m": round(length, 2),
                "lanes": _num(t.get("lanes")), "width_m": _num(t.get("width")),
                "maxweight_t": _num(t.get("maxweight")),
                "bridge": t.get("bridge") not in (None, "", "no"),
                "tunnel": t.get("tunnel") not in (None, "", "no"),
                # a road passing under something; not a passage through a building
                "underpass": ((t.get("tunnel") not in (None, "", "no", "building_passage", "passage"))
                              or (_layer(t.get("layer")) < 0 and t.get("tunnel") not in ("building_passage", "passage"))),
                "layer": _layer(t.get("layer")),
                "surface": t.get("surface", ""),
                "access": access or "", "service": t.get("service", ""),
                "drivable": drive, "walkable": walk, "bus_ok": hw in BUS_OK and drive,
                "free_kmh": float(speed) if drive else 0.0,
                "signals": signals,
                "mid_lon": pts[len(pts) // 2][0], "mid_lat": pts[len(pts) // 2][1],
                "coords": json.dumps([[round(x, 6), round(y, 6)] for x, y in pts]),
            })
    seg = pd.DataFrame(rows)
    used_nodes = set(seg["u"]) | set(seg["v"])
    nodes = pd.DataFrame([{"node_id": nd, "lon": coords[nd][0], "lat": coords[nd][1],
                           "signal": ntags.get(nd, {}).get("highway") == "traffic_signals",
                           "degree": deg[nd]} for nd in used_nodes])

    # Connectivity for driving: segments outside the main drivable component
    # are kept (a crew on foot can still use them) but flagged.
    import networkx as nx

    g = nx.Graph()
    for r in seg.itertuples():
        if r.drivable:
            g.add_edge(r.u, r.v)
    main = max(nx.connected_components(g), key=len) if g.number_of_nodes() else set()
    seg["drive_main"] = seg["drivable"] & seg["u"].isin(main) & seg["v"].isin(main)

    out = PROCESSED / "graph"
    out.mkdir(parents=True, exist_ok=True)
    seg.to_parquet(out / "segments.parquet", index=False)
    nodes.to_parquet(out / "nodes.parquet", index=False)
    feats = [{"type": "Feature", "properties": {"id": r.seg_id, "hw": r.highway, "name": r.name},
              "geometry": {"type": "LineString", "coordinates": json.loads(r.coords)}}
             for r in seg[seg["drivable"]].itertuples()]
    (out / "segments.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    summary = {
        "segments": int(len(seg)), "drivable": int(seg["drivable"].sum()),
        "drivable_main_component": int(seg["drive_main"].sum()),
        "walk_only": int((~seg["drivable"] & seg["walkable"]).sum()),
        "nodes": int(len(nodes)), "signal_nodes": int(nodes["signal"].sum()),
        "bridges": int(seg["bridge"].sum()), "underpasses": int(seg["underpass"].sum()),
        "km_drivable": round(float(seg.loc[seg["drivable"], "length_m"].sum()) / 1000, 1),
        "by_class": seg["highway"].value_counts().to_dict(),
        "assumed_free_flow_kmh": {k: v[2] for k, v in CLASSES.items() if v[0]},
        "osm_manifest": json.loads((RAW / "osm" / "manifest.json").read_text()).get("files", {}).get("roads", {}).get("osm_base"),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    return summary


if __name__ == "__main__":
    print(json.dumps(build(), indent=1))
