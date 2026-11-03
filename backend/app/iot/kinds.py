"""What kind of sensor node an id is, and what it can sense.

Real Arduino nodes (node.ino) carry the full Indradhanu field module. Virtual
nodes (app.iot.virtual) are named V-<REGION>-<KIND><n>, so the kind is read
from the id and no schema change is needed.
"""
from __future__ import annotations

VIRTUAL_PREFIX = "V-"

KINDS: dict[str, dict] = {
    "field": {"label": "Field module", "detail": "Uno + SX1278: gas, heat, sound, piezo, IMU, tilt",
              "cues": "fct", "channels": ("mq2", "mq135", "temp_c", "tilt_deg", "gyro_dps",
                                          "vib_g", "mic", "piezo", "knocks", "tilt_sw")},
    "gas": {"label": "Gas & heat sentinel", "detail": "MQ-2, MQ-135 and temperature",
            "cues": "f", "channels": ("mq2", "mq135", "temp_c")},
    "struct": {"label": "Structural monitor", "detail": "IMU tilt, shock, vibration, piezo, tilt switch",
               "cues": "c", "channels": ("tilt_deg", "gyro_dps", "vib_g", "piezo", "knocks", "tilt_sw")},
    "rescue": {"label": "Rubble listening probe", "detail": "microphone, piezo tapping, warmth, CO2, PIR, IMU",
               "cues": "t", "channels": ("mic", "piezo", "knocks", "temp_c", "mq135", "pir",
                                         "tilt_deg", "gyro_dps")},
}
_CODES = {"GAS": "gas", "STR": "struct", "RES": "rescue", "FLD": "field"}
CODE_OF = {v: k for k, v in _CODES.items()}


def is_virtual(node_id: str) -> bool:
    return node_id.startswith(VIRTUAL_PREFIX)


def kind_of(node_id: str) -> str:
    """V-PUN-GAS2 -> gas; anything not virtual is a real field module."""
    if not is_virtual(node_id):
        return "field"
    parts = node_id.split("-")
    code = parts[2][:3] if len(parts) >= 3 else ""
    return _CODES.get(code, "field")


def can_cue(node_id: str, code: str) -> bool:
    return code == "0" or code in KINDS[kind_of(node_id)]["cues"]


def describe(node_id: str) -> dict:
    k = kind_of(node_id)
    return {"kind": k, "kind_label": KINDS[k]["label"], "kind_detail": KINDS[k]["detail"],
            "channels": list(KINDS[k]["channels"]), "virtual": is_virtual(node_id)}
