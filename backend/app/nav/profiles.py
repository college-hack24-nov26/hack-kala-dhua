"""How each kind of unit moves, and what stops it.

Every number here is a documented assumption, set before any test data was
seen, and every one is a field so the evaluation can report sensitivity to it.
The ETA model later learns how much these actually cost on simulated trips; the
*limits* (fording depth, wind) stay rules, never predictions.

Sources for the assumptions:
  * Depth vs vehicle speed: Pregnolato et al. (2017), "The impact of flooding on
    road transport: a depth-disruption function" (Transp. Res. D 55). Cars stop
    being safely drivable at about 0.3 m of standing water; we use that for
    cars, ambulances and police vehicles and higher limits for high-clearance
    vehicles (fire tenders, buses, JCBs), which are our assumptions.
  * Walking in water: 0.2 m for residents, 0.5 m for a roped rescue team
    (assumption; flowing water is far more dangerous than standing water, so
    near-river segments use half these limits).
  * Drone wind and rain limits: typical small multirotor ratings (assumption);
    your swarm repo's real limits replace these when it is integrated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Mode = Literal["road", "foot", "water", "air"]


@dataclass(frozen=True, slots=True)
class Profile:
    kind: str
    mode: Mode
    label: str
    #: Siren: does not stop at red signals; traffic yields, so congestion bites less.
    siren: bool = False
    #: Maximum standing-water depth the unit may enter (m). Water units: minimum depth.
    depth_limit_m: float = 0.3
    #: Speed multiplier relative to the segment's free-flow speed on a clear road.
    speed_factor: float = 1.0
    #: Absolute cruise speed (km/h) for foot, water and air units.
    cruise_kmh: float | None = None
    #: Seconds lost at each signalised junction (expected, includes green waves).
    signal_delay_s: float = 30.0
    #: How much congestion slows this unit: 1.0 = like normal traffic, 0.3 = barely.
    jam_sensitivity: float = 1.0
    #: Road restrictions.
    main_roads_only: bool = False
    heavy: bool = False             # avoids maxweight < 16 t and width < 3 m
    clears_debris: bool = False
    fire_crew: bool = False         # may enter fire exclusion zones; gas with SCBA
    can_close_roads: bool = False
    #: Air only.
    max_wind_ms: float | None = None
    max_rain_mmph: float | None = None
    battery_reserve: float = 0.0
    endurance_min: float | None = None
    capabilities: tuple[str, ...] = field(default_factory=tuple)


PROFILES: dict[str, Profile] = {p.kind: p for p in (
    Profile("ambulance", "road", "Ambulance", siren=True, depth_limit_m=0.3, speed_factor=1.15,
            signal_delay_s=4, jam_sensitivity=0.45, capabilities=("medical_transport", "medical_care")),
    Profile("police", "road", "Police vehicle", siren=True, depth_limit_m=0.3, speed_factor=1.15,
            signal_delay_s=4, jam_sensitivity=0.45, can_close_roads=True,
            capabilities=("traffic_control", "cordon", "field_assessment")),
    Profile("fire_engine", "road", "Fire tender", siren=True, depth_limit_m=0.6, speed_factor=0.95,
            signal_delay_s=6, jam_sensitivity=0.55, heavy=True, fire_crew=True,
            capabilities=("fire_suppression", "search_rescue", "water_rescue")),
    Profile("bus", "road", "Evacuation bus", depth_limit_m=0.5, speed_factor=0.8, signal_delay_s=35,
            jam_sensitivity=1.0, main_roads_only=True, heavy=True, capabilities=("mass_transport",)),
    Profile("jcb", "road", "JCB", depth_limit_m=0.6, speed_factor=0.5, signal_delay_s=35,
            jam_sensitivity=1.0, clears_debris=True, capabilities=("debris_clearance",)),
    Profile("pump", "road", "Dewatering pump truck", depth_limit_m=0.45, speed_factor=0.8,
            signal_delay_s=30, jam_sensitivity=0.9, capabilities=("dewatering",)),
    Profile("supply_truck", "road", "Relief truck", depth_limit_m=0.5, speed_factor=0.8,
            signal_delay_s=30, jam_sensitivity=1.0, heavy=True, capabilities=("supply_delivery",)),
    Profile("water_tanker", "road", "Water tanker", depth_limit_m=0.5, speed_factor=0.75,
            signal_delay_s=30, jam_sensitivity=1.0, heavy=True, capabilities=("water_supply",)),
    Profile("boat", "water", "Rescue boat", depth_limit_m=0.5, cruise_kmh=8.0,
            capabilities=("water_rescue", "search_rescue")),
    Profile("rescue_team", "foot", "Rescue team on foot", depth_limit_m=0.5, cruise_kmh=4.0,
            signal_delay_s=10, capabilities=("search_rescue", "field_assessment")),
    Profile("resident", "foot", "Resident on foot", depth_limit_m=0.2, cruise_kmh=4.5,
            signal_delay_s=30),
    # Helicopter (state / IAF, requested at level 4): straight line at cruise
    # speed after 10 minutes to start and lift; grounded when it rains harder
    # than its limit (assumption; replaced by the operator's minima).
    Profile("helicopter", "air", "Helicopter", cruise_kmh=180.0, max_wind_ms=18.0, max_rain_mmph=25.0,
            capabilities=("water_rescue", "search_rescue", "medical_transport", "supply_delivery")),
    Profile("drone", "air", "Drone", cruise_kmh=45.0, max_wind_ms=12.0, max_rain_mmph=8.0,
            battery_reserve=0.2, endurance_min=25.0,
            capabilities=("aerial_survey", "survivor_search", "payload_drop", "comms_relay")),
)}

#: resources.kind (database) -> profile. Anything unknown routes as a car.
KIND_TO_PROFILE = {k: k for k in PROFILES} | {"car": "police"}


def for_kind(kind: str) -> Profile:
    return PROFILES.get(KIND_TO_PROFILE.get(kind, ""), PROFILES["police"])
