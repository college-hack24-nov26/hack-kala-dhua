"""Live hazards and their effect on a segment, per unit profile.

A segment's world state at a moment is a `SegmentState`; `effect(profile,
segment, state)` turns it into "blocked, with a reason" or "passable at this
speed factor, with reasons". Blocks are rules. Slowdowns are physics-shaped
assumptions the ETA model refines. The reasons are what reroute explanations
are made of, so each one is short and specific.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.nav.profiles import Profile

#: Hazard ids, shared with the agent log tags (events.hazard_id).
HAZARDS = ("flood", "tree", "wire", "collapse", "landslide", "fire", "gas", "crowd",
           "jam", "storm", "bridge", "airspace")

#: Incident category -> hazard tag, for logs and the sector board.
CATEGORY_HAZARD = {
    "flooded_road": "flood", "waterlogging": "flood", "blocked_drain": "flood",
    "person_stranded": "flood", "shelter_full": "flood", "supply_shortage": "flood",
    "fallen_tree": "tree", "power_line": "wire", "structural_damage": "collapse",
    "earthquake_damage": "collapse", "fire": "fire", "air_quality": "gas",
    "heat_casualty": "heat", "unknown_report": "unknown",
}

FIRE_RADIUS_M = 150.0
GAS_RADIUS_M = 300.0


@dataclass(slots=True)
class SegmentState:
    depth_m: float = 0.0            # standing water on the road
    flowing: bool = False           # water moving (near the river): halves wading limits
    debris: bool = False            # fallen tree or debris across it
    wire: bool = False              # live wire down, not yet isolated
    collapse: bool = False          # wall or building collapsed onto it
    landslide: bool = False
    fire_dist_m: float | None = None
    gas_dist_m: float | None = None
    crowd: float = 0.0              # 0..1 pedestrians on the carriageway
    jam: float = 0.0                # 0..1 congestion (0 = free flow, 1 = standstill)
    rain_mmph: float = 0.0
    bridge_closed: bool = False     # closed at the river's danger mark
    confirmed_closed: bool = False  # a binding closure (crew, officer, high-confidence drone)


@dataclass(slots=True)
class Effect:
    blocked: bool
    speed_factor: float = 1.0
    reasons: list[str] = field(default_factory=list)
    hazard: str | None = None       # the hazard that blocked or most slowed it


def depth_speed_factor(depth_m: float, limit_m: float) -> float:
    """Speed falls with standing water and reaches 0 at the vehicle's limit.

    Shaped like the depth-disruption curve (Pregnolato et al. 2017): little
    effect for a few centimetres, steep near the limit. Scaled to each
    vehicle's own limit, which is our assumption for non-cars.
    """
    if depth_m <= 0.02:
        return 1.0
    if depth_m >= limit_m:
        return 0.0
    x = depth_m / limit_m
    return max(0.05, 1.0 - x ** 1.6)


def rain_speed_factor(mmph: float) -> float:
    """Heavy rain slows traffic: about 10% at 10 mm/h, 25% at 40 mm/h (assumption)."""
    return 1.0 / (1.0 + 0.012 * max(0.0, mmph) ** 0.9)


def effect(p: Profile, seg: dict[str, Any], st: SegmentState) -> Effect:
    reasons: list[str] = []

    def block(reason: str, hazard: str) -> Effect:
        return Effect(True, 0.0, [reason], hazard)

    if p.mode == "air":
        return Effect(False, 1.0, [])          # air routing is separate (air_effect)
    if st.confirmed_closed:
        return block("confirmed closed", "flood")
    if p.mode == "water":
        if st.depth_m < p.depth_limit_m:
            return block(f"too shallow for a boat ({st.depth_m:.2f} m)", "flood")
        return Effect(False, 1.0, [f"boat over {st.depth_m:.1f} m of water"], "flood")

    road = p.mode == "road"
    if road and not seg.get("drivable", True):
        return block("not a road for vehicles", "none")
    if p.mode == "foot" and not seg.get("walkable", True):
        return block("not walkable", "none")
    if road and p.main_roads_only and not seg.get("bus_ok", True):
        return block("too narrow for a bus", "none")
    if road and p.heavy:
        mw, wd = seg.get("maxweight_t"), seg.get("width_m")
        if (mw and mw < 16) or (wd and wd < 3):
            return block("weight or width limit for a heavy vehicle", "none")
    if st.bridge_closed and seg.get("bridge"):
        return block("bridge closed at the river's danger level", "bridge")
    if st.collapse:
        return block("collapsed wall or building on the road", "collapse")
    if st.landslide:
        return block("landslide across the road", "landslide")
    if st.wire:
        return block("live wire down in the area", "wire")
    if st.gas_dist_m is not None and st.gas_dist_m < GAS_RADIUS_M:
        return block(f"gas leak {int(st.gas_dist_m)} m away", "gas")
    if st.fire_dist_m is not None and st.fire_dist_m < FIRE_RADIUS_M and not p.fire_crew:
        return block(f"fire {int(st.fire_dist_m)} m away", "fire")
    if st.debris and road and not p.clears_debris:
        return block("tree or debris across the road", "tree")

    limit = p.depth_limit_m * (0.5 if (st.flowing and p.mode == "foot") else 1.0)
    if st.depth_m >= limit:
        what = "wade" if p.mode == "foot" else "drive through"
        return block(f"water {st.depth_m:.2f} m deep, too deep to {what} (limit {limit:.2f} m)", "flood")

    f = depth_speed_factor(st.depth_m, limit)
    hazard = None
    if f < 0.999:
        reasons.append(f"water {st.depth_m:.2f} m slows it to {f:.0%}")
        hazard = "flood"
    rf = rain_speed_factor(st.rain_mmph)
    if rf < 0.97:
        reasons.append(f"heavy rain {st.rain_mmph:.0f} mm/h")
        hazard = hazard or "storm"
    jf = 1.0
    if road and st.jam > 0:
        jf = max(0.08, 1.0 - st.jam * p.jam_sensitivity)
        if jf < 0.9:
            reasons.append(f"traffic at {1 - jf:.0%} slowdown" + (" (siren)" if p.siren else ""))
            hazard = hazard or "jam"
    cf = 1.0
    if st.crowd > 0:
        cf = max(0.2, 1.0 - 0.7 * st.crowd)
        if cf < 0.9:
            reasons.append("crowd on the road")
            hazard = hazard or "crowd"
    if st.debris and road and p.clears_debris:
        cf *= 0.3
        reasons.append("clearing debris on the way")
        hazard = hazard or "tree"
    if st.fire_dist_m is not None and st.fire_dist_m < FIRE_RADIUS_M and p.fire_crew:
        reasons.append("entering the fire zone")
        hazard = hazard or "fire"
    return Effect(False, f * rf * jf * cf, reasons, hazard)


@dataclass(slots=True)
class AirState:
    wind_ms: float = 0.0
    gust_ms: float = 0.0
    rain_mmph: float = 0.0
    no_fly: bool = False
    plume: bool = False         # fire smoke or gas on the line


def air_effect(p: Profile, st: AirState) -> Effect:
    if p.mode != "air":
        raise ValueError("air_effect is for air units")
    if st.no_fly:
        return Effect(True, 0.0, ["restricted airspace"], "airspace")
    if p.max_wind_ms is not None and max(st.wind_ms, st.gust_ms * 0.8) > p.max_wind_ms:
        return Effect(True, 0.0, [f"grounded: wind {st.wind_ms:.0f} m/s, gusts {st.gust_ms:.0f} m/s"], "storm")
    if p.max_rain_mmph is not None and st.rain_mmph > p.max_rain_mmph:
        return Effect(True, 0.0, [f"grounded: rain {st.rain_mmph:.0f} mm/h"], "storm")
    if st.plume:
        return Effect(True, 0.0, ["smoke or gas on the flight line"], "fire")
    headwind = max(0.5, 1.0 - st.wind_ms / (2.5 * (p.max_wind_ms or 12)))
    return Effect(False, headwind, [f"wind {st.wind_ms:.0f} m/s"] if headwind < 0.9 else [], None)
