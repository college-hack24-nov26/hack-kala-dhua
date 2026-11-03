"""Operational sectors: named groups of wards, for the agent logs and the board.

Follows the incident-command idea of geographic *divisions*: an officer runs a
sector, and every agent decision, reroute and guardrail trip is filed under the
sector it happened in (and under its hazard). The PCMC zone (migration 029) is
split along the hazard it actually faces; any other ward is its own sector
until an officer groups it.
"""
from __future__ import annotations

SECTORS: dict[str, dict] = {
    "pcmc-pawana": {"name": "Pawana riverside", "wards": ["w-pc-03", "w-pc-05", "w-pc-07", "w-pc-10"],
                    "faces": ["flood", "bridge", "crowd"]},
    "pcmc-nigdi-akurdi": {"name": "Nigdi–Akurdi (PCCOE)", "wards": ["w-pc-01", "w-pc-02"],
                          "faces": ["flood", "tree", "wire"]},
    "pcmc-wakad-thergaon": {"name": "Wakad–Thergaon", "wards": ["w-pc-04", "w-pc-08", "w-pc-09", "w-pc-14"],
                            "faces": ["flood", "jam"]},
    "pcmc-midc-chikhli": {"name": "MIDC–Chikhli", "wards": ["w-pc-06", "w-pc-11"],
                          "faces": ["fire", "gas", "flood"]},
    "pcmc-dehu-talawade": {"name": "Dehu Road–Talawade", "wards": ["w-pc-12", "w-pc-13"],
                           "faces": ["landslide", "flood", "airspace"]},
}
_WARD_SECTOR = {w: sid for sid, s in SECTORS.items() for w in s["wards"]}


def sector_of(ward_id: str | None) -> str | None:
    if not ward_id:
        return None
    if ward_id in _WARD_SECTOR:
        return _WARD_SECTOR[ward_id]
    if ward_id.startswith("w-gzb-"):
        return "ncr"
    return f"ward:{ward_id}"


def name_of(sector_id: str | None) -> str:
    if not sector_id:
        return "City-wide"
    if sector_id in SECTORS:
        return SECTORS[sector_id]["name"]
    if sector_id == "ncr":
        return "Ghaziabad"
    return sector_id.removeprefix("ward:")
