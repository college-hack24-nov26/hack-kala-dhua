"""Download the district's roads, waterways, signals and context from OpenStreetMap.

    python -m ml.fetch.osm            (from backend/)

Standard library only. Writes data/raw/osm/{roads,water,context}.json, the
Overpass JSON as returned, plus a manifest with the query and timestamp.
OpenStreetMap data © OpenStreetMap contributors, ODbL 1.0.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from ml.district import BBOX, RAW

ENDPOINTS = ("https://overpass-api.de/api/interpreter",
             "https://overpass.kumi.systems/api/interpreter")
UA = "DisruptionOps-research/1.0 (disaster-response hackathon; github.com/officialayush23)"

W, S, E, N = BBOX
BB = f"({S},{W},{N},{E})"

QUERIES = {
    "roads": f"""[out:json][timeout:300];
(
  way["highway"]["area"!="yes"]{BB};
  node["highway"~"^(traffic_signals|crossing|stop|give_way)$"]{BB};
  node["barrier"]{BB};
);
out body;
>;
out skel qt;""",
    "water": f"""[out:json][timeout:300];
(
  way["waterway"~"^(river|stream|canal|drain|ditch|riverbank)$"]{BB};
  way["natural"="water"]{BB};
  relation["natural"="water"]{BB};
);
out body;
>;
out skel qt;""",
    "context": f"""[out:json][timeout:300];
(
  way["landuse"~"^(residential|military|industrial)$"]{BB};
  relation["landuse"~"^(residential|military)$"]{BB};
  node["place"~"^(suburb|neighbourhood|village|hamlet|quarter)$"]{BB};
  way["power"="line"]{BB};
  node["amenity"~"^(hospital|fire_station|police|school|community_centre)$"]{BB};
  way["amenity"~"^(hospital|fire_station|police|school|community_centre)$"]{BB};
  way["natural"="tree_row"]{BB};
  node["aeroway"]{BB};
  way["military"]{BB};
);
out body center;
>;
out skel qt;""",
}


def fetch(name: str, query: str) -> dict:
    last = None
    for url in ENDPOINTS:
        for attempt in range(3):
            try:
                body = urllib.parse.urlencode({"data": query}).encode()
                req = urllib.request.Request(url, data=body, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=320) as r:
                    return json.load(r)
            except Exception as exc:  # noqa: BLE001
                last = exc
                print(f"  {name}: {url} attempt {attempt + 1} failed: {exc}", flush=True)
                time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"{name}: every Overpass endpoint failed: {last}")


def main() -> None:
    out = RAW / "osm"
    out.mkdir(parents=True, exist_ok=True)
    manifest = {"bbox_wsen": BBOX, "fetched_at": datetime.now(timezone.utc).isoformat(),
                "source": "OpenStreetMap via Overpass API", "licence": "ODbL 1.0", "files": {}}
    for name, q in QUERIES.items():
        print(f"fetching {name} ...", flush=True)
        data = fetch(name, q)
        path = out / f"{name}.json"
        path.write_text(json.dumps(data))
        n = len(data.get("elements", []))
        manifest["files"][name] = {"elements": n, "query": q, "osm_base": data.get("osm3s", {}).get("timestamp_osm_base")}
        print(f"  {name}: {n} elements -> {path}", flush=True)
        time.sleep(5)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))


if __name__ == "__main__":
    main()
