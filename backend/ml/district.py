"""The one district the final round is scoped to.

Pimpri-Chinchwad (PCMC) zone round PCCOE, Pune district. The envelope is the
same one migration 029 clips its wards to, so the road graph, the wards and the
simulator all describe exactly the same area.
"""
from __future__ import annotations

from pathlib import Path

NAME = "PCMC · Pawana corridor (Pune district)"
HAZARD = "flood"
#: west, south, east, north (WGS84)
BBOX = (73.715, 18.585, 73.845, 18.712)

#: River cells for GloFAS discharge (lon, lat). GloFAS v4 is a 0.05° grid with
#: cell centres at .x25/.x75, and the API returns the cell nearest the point, so
#: these are cell centres, picked by querying the neighbouring cells and keeping
#: the one that carries the main stem (2019-20 peak in m³/s in brackets). The
#: first picks for Kasarwadi and Talawade landed on tributary cells (annual peak
#: ≈ 10 m³/s) and were replaced; Pawana below Chinchwad shares no cell of its own
#: with the main stem, so the lower Pawana uses the Chinchwad cell.
RIVER_POINTS = {
    "pawana_ravet": (73.725, 18.625),        # Pawana entering the district (833)
    "pawana_chinchwad": (73.775, 18.625),    # Pawana at Chinchwad (856)
    "mula_wakad": (73.775, 18.575),          # Mula along Wakad–Pimple Nilakh (1922)
    "indrayani_chikhli": (73.825, 18.675),   # Indrayani at Chikhli–Moshi (1640)
}
#: Which cell drives which river in terrain.py's `river` column.
#: Pavana west of PAVANA_SPLIT_LON (above Ravet) uses the Ravet cell.
RIVER_CELL = {"Pavana": "pawana_chinchwad", "Mula": "mula_wakad",
              "Indrayani": "indrayani_chikhli", "Kasarsai": "pawana_ravet"}
PAVANA_SPLIT_LON = 73.752
#: ERA5 is 0.25° (~25 km): one point covers the district; two give a check.
WEATHER_POINTS = {
    "district": (73.78, 18.645),
    "maval_upstream": (73.60, 18.70),   # rain upstream on the Pawana catchment
}

REPO = Path(__file__).resolve().parents[2]
#: DISRUPTIONOPS_DATA points elsewhere when deployed (the API image carries a
#: small serving bundle, backend/serving/data; see ml/export_serving.py).
import os as _os  # noqa: E402
DATA = Path(_os.environ.get("DISRUPTIONOPS_DATA") or (REPO / "data"))
RAW = DATA / "raw"
PROCESSED = DATA / "processed"
SIM = DATA / "sim"

#: Ward seeds from migration 029 (the wards are the Voronoi cells of these
#: points clipped to BBOX), so a segment's ward is its nearest seed.
WARD_SEEDS = {
    "w-pc-01": (73.7624, 18.6527), "w-pc-02": (73.7773, 18.6598), "w-pc-03": (73.7451, 18.6433),
    "w-pc-04": (73.7502, 18.6154), "w-pc-05": (73.7804, 18.6294), "w-pc-06": (73.7915, 18.6395),
    "w-pc-07": (73.8025, 18.6230), "w-pc-08": (73.7729, 18.6093), "w-pc-09": (73.7644, 18.6022),
    "w-pc-10": (73.8209, 18.6081), "w-pc-11": (73.8180, 18.6700), "w-pc-12": (73.7343, 18.6800),
    "w-pc-13": (73.7908, 18.6987), "w-pc-14": (73.7978, 18.5982),
}
