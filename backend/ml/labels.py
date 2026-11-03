"""The router's hazard rules, vectorized over every segment at one moment.

`app.nav.hazards.effect` decides one segment for one unit; training labels and
route evaluation need all 43k segments at every step, so this module applies
the same rules to arrays. `tests/test_ml_labels.py` checks the two agree on
random (segment, step, unit) samples, so the labels mean exactly what the
router means by "blocked".

    S = step_state(truth, t)                  # arrays for one step
    blk, why = blocked(PROFILES["ambulance"], S)
    secs = seconds(PROFILES["ambulance"], S)  # inf where blocked
"""
from __future__ import annotations

import numpy as np

from app.nav.hazards import FIRE_RADIUS_M, GAS_RADIUS_M
from app.nav.profiles import Profile
from ml.sim.static import load_static
from ml.sim.world import F_BRIDGE, F_COLLAPSE, F_DEBRIS, F_FLOWING, F_LANDSLIDE, F_WIRE, Truth

#: Reason codes (index into WHY) for blocked segments.
WHY = ("", "closed", "not_a_road", "too_narrow", "weight_width", "bridge", "collapse", "landslide",
       "wire", "gas", "fire", "tree", "water", "shallow")
HAZARD_OF = {"bridge": "bridge", "collapse": "collapse", "landslide": "landslide", "wire": "wire",
             "gas": "gas", "fire": "fire", "tree": "tree", "water": "flood", "closed": "flood"}


def _static() -> dict[str, np.ndarray]:
    st = load_static()
    return {
        "drivable": st["drivable"].to_numpy(), "walkable": st["walkable"].to_numpy(),
        "bus_ok": st["bus_ok"].to_numpy(), "bridge": st["bridge"].to_numpy(),
        "maxweight_t": st["maxweight_t"].fillna(0).to_numpy(), "width_m": st["width_m"].fillna(0).to_numpy(),
        "length_m": st["length_m"].to_numpy(), "free_kmh": st["free_kmh"].replace(0, 20.0).to_numpy(),
        "signals": st["signals"].to_numpy(),
    }


_S: dict[str, np.ndarray] | None = None


def static_arrays() -> dict[str, np.ndarray]:
    global _S
    if _S is None:
        _S = _static()
    return _S


def step_state(tr: Truth, t: int, confirmed_closed: np.ndarray | None = None) -> dict[str, np.ndarray]:
    f = tr.flags[t]
    n = f.shape[0]
    return {
        "depth": tr.depth[t].astype(np.float32),
        "flowing": (f & F_FLOWING) > 0, "debris": (f & F_DEBRIS) > 0, "wire": (f & F_WIRE) > 0,
        "collapse": (f & F_COLLAPSE) > 0, "landslide": (f & F_LANDSLIDE) > 0,
        "bridge_closed": (f & F_BRIDGE) > 0,
        "fire_d": tr.point_dist(t, "fire", FIRE_RADIUS_M), "gas_d": tr.point_dist(t, "gas", GAS_RADIUS_M),
        "jam": tr.jam[t].astype(np.float32) / 250, "crowd": tr.crowd[t].astype(np.float32) / 250,
        "rain": (tr.rain[t] * tr.field).astype(np.float32),
        "confirmed_closed": np.zeros(n, bool) if confirmed_closed is None else confirmed_closed,
    }


def blocked(p: Profile, S: dict[str, np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    """(blocked mask, reason code) in the same precedence as hazards.effect."""
    g = static_arrays()
    n = len(S["depth"])
    why = np.zeros(n, np.int8)

    def rule(mask: np.ndarray, code: str) -> None:
        m = mask & (why == 0)
        why[m] = WHY.index(code)

    if p.mode == "air":
        return np.zeros(n, bool), why
    rule(S["confirmed_closed"], "closed")
    if p.mode == "water":
        rule(S["depth"] < p.depth_limit_m, "shallow")
        return why > 0, why
    road = p.mode == "road"
    if road:
        rule(~g["drivable"], "not_a_road")
        if p.main_roads_only:
            rule(~g["bus_ok"], "too_narrow")
        if p.heavy:
            mw, wd = g["maxweight_t"], g["width_m"]
            rule(((mw > 0) & (mw < 16)) | ((wd > 0) & (wd < 3)), "weight_width")
    else:
        rule(~g["walkable"], "not_a_road")
    rule(S["bridge_closed"] & g["bridge"], "bridge")
    rule(S["collapse"], "collapse")
    rule(S["landslide"], "landslide")
    rule(S["wire"], "wire")
    rule(S["gas_d"] < GAS_RADIUS_M, "gas")
    if not p.fire_crew:
        rule(S["fire_d"] < FIRE_RADIUS_M, "fire")
    if road and not p.clears_debris:
        rule(S["debris"], "tree")
    limit = np.where(S["flowing"] & (p.mode == "foot"), p.depth_limit_m * 0.5, p.depth_limit_m)
    rule(S["depth"] >= limit, "water")
    return why > 0, why


def speed_factor(p: Profile, S: dict[str, np.ndarray]) -> np.ndarray:
    limit = np.where(S["flowing"] & (p.mode == "foot"), p.depth_limit_m * 0.5, p.depth_limit_m)
    d = S["depth"]
    x = np.clip(d / limit, 0, 1)
    f = np.where(d <= 0.02, 1.0, np.where(d >= limit, 0.0, np.maximum(0.05, 1.0 - x ** 1.6)))
    rf = 1.0 / (1.0 + 0.012 * np.maximum(0.0, S["rain"]) ** 0.9)
    jf = np.ones_like(f)
    if p.mode == "road":
        jf = np.where(S["jam"] > 0, np.maximum(0.08, 1.0 - S["jam"] * p.jam_sensitivity), 1.0)
    cf = np.where(S["crowd"] > 0, np.maximum(0.2, 1.0 - 0.7 * S["crowd"]), 1.0)
    if p.mode == "road" and p.clears_debris:
        cf = np.where(S["debris"], cf * 0.3, cf)
    return f * rf * jf * cf


def seconds(p: Profile, S: dict[str, np.ndarray]) -> np.ndarray:
    """Traversal time per segment (inf where blocked), as app.nav.cost.traverse."""
    g = static_arrays()
    blk, _ = blocked(p, S)
    kmh = g["free_kmh"] * p.speed_factor if p.mode == "road" else np.full(len(blk), float(p.cruise_kmh or 4.5))
    v = np.maximum(0.5, kmh * speed_factor(p, S)) / 3.6
    t = g["length_m"] / v
    if p.mode in ("road", "foot"):
        t = t + g["signals"] * p.signal_delay_s
    return np.where(blk, np.inf, t)
