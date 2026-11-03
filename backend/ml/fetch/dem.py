"""Copernicus DEM GLO-30 tile for the district, clipped.

    python -m ml.fetch.dem

Downloads the 1°×1° Cloud-Optimised GeoTIFF from the public AWS bucket
(copernicus-dem-30m, no account needed) and keeps the original tile in
data/raw/dem/. Clipping happens in ml/terrain.py. Licence: Copernicus DEM
GLO-30 © DLR/Airbus, provided under COPERNICUS by the EU and ESA, free use.
"""
from __future__ import annotations

import urllib.request

from ml.district import BBOX, RAW

TILE = "Copernicus_DSM_COG_10_N18_00_E073_00_DEM"
URL = f"https://copernicus-dem-30m.s3.amazonaws.com/{TILE}/{TILE}.tif"


def main() -> None:
    w, s, e, n = BBOX
    assert 73 <= w and e < 74 and 18 <= s and n < 19, "district must sit in tile N18 E073"
    out = RAW / "dem"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{TILE}.tif"
    if not path.exists():
        urllib.request.urlretrieve(URL, path)
    print(path, path.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
