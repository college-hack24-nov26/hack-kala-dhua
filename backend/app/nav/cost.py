"""Seconds to traverse one segment for one unit, from its profile and state.

    seconds, effect = traverse(profile, segment, state)

`seconds` is None when the segment is blocked for that unit. This is the
deterministic physics; the ETA model (ml/eta.py) is trained to correct it from
simulated trips, and the passability model to predict `state` at arrival.
"""
from __future__ import annotations

from typing import Any

from app.nav.hazards import Effect, SegmentState, effect
from app.nav.profiles import Profile


def traverse(p: Profile, seg: dict[str, Any], st: SegmentState) -> tuple[float | None, Effect]:
    eff = effect(p, seg, st)
    if eff.blocked:
        return None, eff
    length = float(seg["length_m"])
    if p.mode == "road":
        kmh = float(seg.get("free_kmh") or 20.0) * p.speed_factor
    else:
        kmh = float(p.cruise_kmh or 4.5)
    v = max(0.5, kmh * eff.speed_factor) / 3.6
    t = length / v
    if p.mode in ("road", "foot"):
        t += float(seg.get("signals") or 0) * p.signal_delay_s
    return t, eff
