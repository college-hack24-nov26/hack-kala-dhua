"""Travel over the road graph, and the roads themselves.

Three jobs, in one place because they all need the same router:

  * **A matrix** of durations, for the allocator. Mapbox first, then OSRM, then
    great-circle distance at an urban event speed. Every fallback is *slower*
    than reality, so a degraded plan never promises an ETA it cannot meet.
  * **A route**, as the geometry of actual streets plus the turn instructions
    that name them. This is what a person can follow and what a unit drives
    along; a straight line between two dots is neither.
  * **Snapping a point to a road**, which is what makes a hazard a hazard. A
    flooded road has to be *on* a road, or nothing routes around it and the
    whole avoidance story is decoration.

The blockage handling is the part worth arguing about. No public router knows
which streets are under water tonight, so we cannot ask it to avoid them.
What we can do is ask for alternatives and choose the one that passes nearest to
none of the hazards people have reported, and when every alternative is exposed,
return the least exposed one *with a count* rather than a false promise.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.core.config import settings
from app.core.logging import get_logger
from app.ingest.client import get_json

log = get_logger(__name__)

EARTH_RADIUS_KM = 6371.0
#: Effective speed during an active urban flood event.
EVENT_SPEED_KMH = 18.0
#: Extra minutes added when a route passes near a blocked point.
BLOCKED_DETOUR_MINUTES = 11.0
#: A route within this distance of a blockage is treated as affected.
#:
#: Was 0.35 km. At that radius a single block covered every parallel street in a
#: dense ward, so no alternative the router offered could ever avoid it, every
#: route stayed "exposed", and the re-planner redrew the same road on every
#: pass. A closed road closes that road and its junctions; 150 m is that.
BLOCK_RADIUS_KM = 0.15
#: A block this close to either end of a trip is not something the trip can go
#: round: it is at the job, or at the depot. It is still a hazard on the map;
#: it just does not count against this route. Without this every unit sent to
#: a flooded road was "routed through a block" — the block was the incident.
ENDPOINT_CLEARANCE_KM = 0.25
#: Mapbox Directions accepts at most 50 excluded points per request.
MAX_EXCLUDE_POINTS = 50
#: How far to the side of a block a detour waypoint is placed when the router
#: cannot exclude points itself (OSRM).
DETOUR_OFFSET_KM = 0.6
#: Mapbox Matrix allows 25 coordinates per request on the standard profile.
MATRIX_LIMIT = 25


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1 = math.radians(a[0]), math.radians(a[1])
    lon2, lat2 = math.radians(b[0]), math.radians(b[1])
    dlon, dlat = lon2 - lon1, lat2 - lat1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def _xy_km(p: tuple[float, float], ref_lat: float) -> tuple[float, float]:
    """Equirectangular projection. Exact enough inside one city."""
    k = math.pi / 180.0 * EARTH_RADIUS_KM
    return (p[0] * k * math.cos(math.radians(ref_lat)), p[1] * k)


def point_segment_km(
    p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> float:
    """Shortest distance from a point to a segment, in km.

    The old exposure test sampled every third vertex of a route, so a block
    sitting between two sampled vertices on a long straight road was simply not
    seen, and the same road could be "clear" on one pass and "blocked" on the
    next depending on where the router put its vertices.
    """
    ref = (a[1] + b[1] + p[1]) / 3.0
    px, py = _xy_km(p, ref)
    ax, ay = _xy_km(a, ref)
    bx, by = _xy_km(b, ref)
    dx, dy = bx - ax, by - ay
    seg = dx * dx + dy * dy
    t = 0.0 if seg == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def relevant_blocks(
    origin: tuple[float, float],
    destination: tuple[float, float],
    blocked: Sequence[tuple[float, float]],
) -> list[tuple[float, float]]:
    """The blocks a trip from origin to destination could actually go round."""
    return [
        b for b in blocked
        if haversine_km(b, origin) > ENDPOINT_CLEARANCE_KM
        and haversine_km(b, destination) > ENDPOINT_CLEARANCE_KM
    ]


def _coords(pairs: Sequence[tuple[float, float]]) -> str:
    return ";".join(f"{lng:.5f},{lat:.5f}" for lng, lat in pairs)


def _data(result: Any) -> dict | None:
    """Unwrap `get_json`, which returns a FeedResult and not the payload.

    This was the bug behind "the navigation is a straight line": the route code
    treated the wrapper as the JSON, `.get("routes")` raised, the except arm
    caught it, and every route in the system quietly became the fallback. It had
    never once called a router successfully.
    """
    payload = getattr(result, "data", result)
    return payload if isinstance(payload, dict) else None


@dataclass(slots=True)
class TravelMatrix:
    """durations[i][j] = minutes from source i to destination j."""

    durations: list[list[float]]
    distances: list[list[float]]
    engine: str

    def eta(self, i: int, j: int) -> int:
        return max(4, round(self.durations[i][j]))


def _segment_is_blocked(
    a: tuple[float, float],
    b: tuple[float, float],
    blocked: Sequence[tuple[float, float]],
) -> bool:
    """Cheap proximity test: does the straight line a→b pass near a blockage?

    Sampling the segment is enough at city scale and avoids pulling in a full
    geometry engine on the hot path.
    """
    if not blocked:
        return False
    # A straight segment is a proxy for the road here, so the corridor is
    # wider than BLOCK_RADIUS_KM: the real road wanders either side of it.
    corridor = BLOCK_RADIUS_KM * 2.5
    return any(
        point_segment_km(block, a, b) <= corridor
        for block in relevant_blocks(a, b, blocked)
    )


def _haversine_np(lon1, lat1, lon2, lat2):
    import numpy as np
    lon1, lat1, lon2, lat2 = (np.radians(v) for v in (lon1, lat1, lon2, lat2))
    h = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.minimum(1.0, h)))


def blocked_pairs(
    sources: Sequence[tuple[float, float]],
    destinations: Sequence[tuple[float, float]],
    blocked: Sequence[tuple[float, float]],
) -> list[list[bool]]:
    """`_segment_is_blocked` for every source x destination pair at once (numpy).

    Same rule, same formulas. The per-pair Python loop was units x demands x
    blocks calls on the event loop; with a few hundred blocks it froze the whole
    API for over a minute per re-plan (seen with /status/loop on Render).
    """
    S, D = len(sources), len(destinations)
    if not blocked or not S or not D:
        return [[False] * D for _ in range(S)]
    import numpy as np
    src = np.asarray(sources, dtype=float)           # (S, 2) lon, lat
    dst = np.asarray(destinations, dtype=float)      # (D, 2)
    blk = np.asarray(blocked, dtype=float)           # (B, 2)
    corridor = BLOCK_RADIUS_KM * 2.5
    k = math.pi / 180.0 * EARTH_RADIUS_KM
    # endpoint clearance: a block at either end is the job itself, not something to go round
    far_src = _haversine_np(blk[None, :, 0], blk[None, :, 1], src[:, None, 0], src[:, None, 1]) > ENDPOINT_CLEARANCE_KM   # (S, B)
    far_dst = _haversine_np(blk[None, :, 0], blk[None, :, 1], dst[:, None, 0], dst[:, None, 1]) > ENDPOINT_CLEARANCE_KM   # (D, B)
    out = np.zeros((S, D), dtype=bool)
    for i in range(S):                                # (D, B) per source keeps memory small
        a = src[i]
        ref = np.radians((a[1] + dst[:, None, 1] + blk[None, :, 1]) / 3.0)
        c = np.cos(ref) * k
        ax, ay = a[0] * c, a[1] * k
        bx, by = dst[:, None, 0] * c, dst[:, None, 1] * k
        px, py = blk[None, :, 0] * c, blk[None, :, 1] * k
        dx, dy = bx - ax, by - ay
        seg = dx * dx + dy * dy
        with np.errstate(invalid="ignore", divide="ignore"):
            t = np.where(seg == 0, 0.0, np.clip(((px - ax) * dx + (py - ay) * dy) / np.where(seg == 0, 1.0, seg), 0.0, 1.0))
        dist = np.hypot(px - (ax + t * dx), py - (ay + t * dy))
        out[i] = np.any((dist <= corridor) & far_src[i][None, :] & far_dst, axis=1)
    return out.tolist()


def _fallback_matrix(
    sources: Sequence[tuple[float, float]],
    destinations: Sequence[tuple[float, float]],
    blocked: Sequence[tuple[float, float]],
) -> TravelMatrix:
    durations: list[list[float]] = []
    distances: list[list[float]] = []
    hit = blocked_pairs(sources, destinations, blocked)
    for i, s in enumerate(sources):
        row_d: list[float] = []
        row_km: list[float] = []
        for j, d in enumerate(destinations):
            km = haversine_km(s, d)
            # Straight-line distance understates road distance; 1.35 is the
            # usual detour factor for a dense Indian city grid.
            road_km = km * 1.35
            minutes = (road_km / EVENT_SPEED_KMH) * 60.0
            if hit[i][j]:
                minutes += BLOCKED_DETOUR_MINUTES
            row_d.append(minutes)
            row_km.append(round(road_km, 2))
        durations.append(row_d)
        distances.append(row_km)
    return TravelMatrix(durations, distances, engine="haversine-fallback")


def _apply_blocks(
    durations: list[list[float]],
    sources: Sequence[tuple[float, float]],
    destinations: Sequence[tuple[float, float]],
    blocked: Sequence[tuple[float, float]],
) -> None:
    hit = blocked_pairs(sources, destinations, blocked)
    for i in range(len(sources)):
        for j in range(len(destinations)):
            if hit[i][j]:
                durations[i][j] += BLOCKED_DETOUR_MINUTES


async def _mapbox_matrix(
    sources: Sequence[tuple[float, float]],
    destinations: Sequence[tuple[float, float]],
) -> TravelMatrix | None:
    if not settings.mapbox_token:
        return None
    if len(sources) + len(destinations) > MATRIX_LIMIT:
        return None
    n = len(sources)
    payload = _data(
        await get_json(
            f"{settings.mapbox_matrix_url}/driving/"
            f"{_coords(list(sources) + list(destinations))}",
            {
                "access_token": settings.mapbox_token,
                "sources": ";".join(str(i) for i in range(n)),
                "destinations": ";".join(str(n + j) for j in range(len(destinations))),
                "annotations": "duration,distance",
            },
            cache_key=f"mbx:matrix:{hash(_coords(list(sources) + list(destinations)))}",
        )
    )
    if not payload or payload.get("code") != "Ok":
        return None
    try:
        durations = [[(d or 0.0) / 60.0 for d in row] for row in payload["durations"]]
        distances = [
            [round((d or 0.0) / 1000.0, 2) for d in row]
            for row in payload.get("distances") or [[0.0] * len(destinations)] * n
        ]
    except (KeyError, TypeError):
        return None
    return TravelMatrix(durations, distances, engine="mapbox")


async def _osrm_matrix(
    sources: Sequence[tuple[float, float]],
    destinations: Sequence[tuple[float, float]],
) -> TravelMatrix | None:
    coord_str = _coords(list(sources) + list(destinations))
    n = len(sources)
    payload = _data(
        await get_json(
            f"{settings.osrm_url}/table/v1/driving/{coord_str}",
            {
                "sources": ";".join(str(i) for i in range(n)),
                "destinations": ";".join(str(n + j) for j in range(len(destinations))),
                "annotations": "duration,distance",
            },
            cache_key=f"osrm:table:{hash(coord_str)}",
        )
    )
    if not payload or payload.get("code") != "Ok":
        return None
    try:
        durations = [[(d or 0.0) / 60.0 for d in row] for row in payload["durations"]]
        distances = [
            [round((d or 0.0) / 1000.0, 2) for d in row]
            for row in payload.get("distances") or [[0.0] * len(destinations)] * n
        ]
    except (KeyError, TypeError):
        return None
    return TravelMatrix(durations, distances, engine="osrm")


async def travel_matrix(
    sources: Sequence[tuple[float, float]],
    destinations: Sequence[tuple[float, float]],
    blocked: Sequence[tuple[float, float]] = (),
) -> TravelMatrix:
    if not sources or not destinations:
        return TravelMatrix([], [], engine="empty")

    matrix = await _mapbox_matrix(sources, destinations)
    if matrix is None:
        matrix = await _osrm_matrix(sources, destinations)
    # Off the event loop: numpy work, but with hundreds of units x demands x blocks it is still not free.
    if matrix is None:
        return await asyncio.to_thread(_fallback_matrix, sources, destinations, blocked)

    await asyncio.to_thread(_apply_blocks, matrix.durations, sources, destinations, blocked)
    return matrix


# --------------------------------------------------------------- one route ---
@dataclass(slots=True)
class Step:
    """One turn, with the street it happens on."""

    instruction: str
    street: str
    distance_m: int


@dataclass(slots=True)
class RouteLine:
    """A path a person can actually follow, not a line between two dots."""

    coordinates: list[list[float]]
    km: float
    minutes: int
    engine: str
    #: Blocked points the returned path still passes close to. Zero is the goal;
    #: a non-zero number is reported rather than hidden, because the honest
    #: answer during a flood is sometimes "this is the least bad way".
    passes_near_blocks: int = 0
    #: Turn instructions naming real streets. Empty when the router was not
    #: reachable and the geometry is the fallback line.
    steps: list[Step] = field(default_factory=list)
    #: How many alternatives were compared to pick this one.
    considered: int = 1
    #: Blocked points the router was told to keep out of the path, or that a
    #: detour waypoint was placed to go round.
    avoided: int = 0
    #: How the avoidance was done: "exclude" (Mapbox excluded the points),
    #: "detour" (a via point to one side), "alternatives" (picked the least
    #: exposed of the router's own alternatives) or "" when nothing was blocked.
    avoidance: str = ""

    @property
    def is_real_road(self) -> bool:
        return self.engine in ("mapbox", "osrm")


def _exposed_blocks(
    coords: Sequence[Sequence[float]], blocked: Sequence[tuple[float, float]]
) -> list[tuple[float, float]]:
    """The distinct blocks a polyline passes within BLOCK_RADIUS_KM of."""
    if not blocked or len(coords) < 2:
        return []
    pts = [(float(c[0]), float(c[1])) for c in coords]
    hit: list[tuple[float, float]] = []
    for b in blocked:
        for a, c in zip(pts, pts[1:]):
            if point_segment_km(b, a, c) <= BLOCK_RADIUS_KM:
                hit.append(b)
                break
    return hit


def _exposure(coords: Sequence[Sequence[float]], blocked: Sequence[tuple[float, float]]) -> int:
    if len(coords) < 2:
        return 0
    relevant = relevant_blocks(
        (float(coords[0][0]), float(coords[0][1])),
        (float(coords[-1][0]), float(coords[-1][1])),
        blocked,
    )
    return len(_exposed_blocks(coords, relevant))


def exposure(
    coords: list[list[float]], blocked: Sequence[tuple[float, float]]
) -> int:
    """How many distinct blocked points a path still passes close to.

    Public because the replanner asks it of a road a unit is *already* driving,
    not only of a candidate. Same question, same answer, one implementation —
    a second copy of this would be a second opinion about whether a street is
    passable.

    Counts distinct blocks, not vertices near blocks. The old count grew with
    how finely the router happened to draw the road, so the same street could
    score 2 on one pass and 5 on the next and look like it had got worse.
    """
    return _exposure(coords, blocked)


def remaining_path(
    coords: Sequence[Sequence[float]], progress: float
) -> list[list[float]]:
    """The part of a stored route a unit has not driven yet.

    A block behind a unit is not a reason to re-route it. Judging the whole
    stored route did exactly that: a crew that had already passed a junction
    got a new road every time somebody reported the junction closed.
    """
    pts = [[float(c[0]), float(c[1])] for c in coords]
    if len(pts) < 2 or progress <= 0:
        return pts
    if progress >= 1:
        return pts[-1:] * 2
    lengths = [haversine_km(tuple(a), tuple(b)) for a, b in zip(pts, pts[1:])]
    total = sum(lengths) or 1e-9
    target = total * progress
    run = 0.0
    for i, seg in enumerate(lengths):
        if run + seg >= target:
            t = (target - run) / seg if seg else 0.0
            a, b = pts[i], pts[i + 1]
            cut = [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]
            return [cut] + pts[i + 1:]
        run += seg
    return pts[-1:] * 2


def _mapbox_steps(route: dict) -> list[Step]:
    out: list[Step] = []
    for leg in route.get("legs") or []:
        for s in leg.get("steps") or []:
            man = s.get("maneuver") or {}
            text = man.get("instruction") or ""
            name = s.get("name") or ""
            if not text:
                continue
            out.append(
                Step(
                    instruction=text,
                    street=name,
                    distance_m=int(round(float(s.get("distance") or 0.0))),
                )
            )
    return out[:12]


#: OSRM answers with a maneuver type and a modifier and leaves the sentence to
#: the client. Mapbox writes the sentence for you, which is why this did not
#: exist until now — and why, with no Mapbox token set, `route_steps` came back
#: empty on every guidance call, the phone found no steps to follow, and
#: turn-by-turn simply never appeared. The line was drawn on the map and nobody
#: was ever told to turn anywhere.
_TURNS = {
    "left": "Turn left", "right": "Turn right",
    "sharp left": "Take a sharp left", "sharp right": "Take a sharp right",
    "slight left": "Bear left", "slight right": "Bear right",
    "straight": "Carry straight on",
    "uturn": "Turn around",
}


def _osrm_steps(route: dict) -> list[Step]:
    """Turn OSRM's maneuvers into sentences somebody can follow while walking."""
    out: list[Step] = []
    for leg in route.get("legs") or []:
        for s in leg.get("steps") or []:
            man = s.get("maneuver") or {}
            kind = str(man.get("type") or "")
            modifier = str(man.get("modifier") or "")
            name = str(s.get("name") or "")
            distance = int(round(float(s.get("distance") or 0.0)))

            if kind == "depart":
                text = "Set off" + (f" along {name}" if name else "")
            elif kind == "arrive":
                text = "You have arrived"
            elif kind == "roundabout" or kind == "rotary":
                exit_no = man.get("exit")
                text = (
                    f"At the roundabout, take exit {exit_no}"
                    if exit_no else "Go around the roundabout"
                )
            elif kind in ("merge", "on ramp", "off ramp", "fork"):
                text = _TURNS.get(modifier, "Keep going")
                if kind == "fork":
                    text = text.replace("Turn", "Fork")
            else:
                text = _TURNS.get(modifier, "Continue")
            if name and kind not in ("depart", "arrive"):
                text = f"{text} onto {name}"

            out.append(Step(instruction=text, street=name, distance_m=distance))
    return out[:12]


def _pick(routes: list[dict], blocked: Sequence[tuple[float, float]]) -> tuple[dict, int]:
    """Least exposed first, then fastest. Exposure outranks speed on purpose."""
    scored: list[tuple[int, float, int, dict]] = []
    for n, r in enumerate(routes):
        coords = (r.get("geometry") or {}).get("coordinates") or []
        scored.append((_exposure(coords, blocked), float(r.get("duration") or 0.0), n, r))
    scored.sort(key=lambda t: (t[0], t[1], t[2]))
    return scored[0][3], scored[0][0]


def _exclude_param(
    origin: tuple[float, float],
    destination: tuple[float, float],
    relevant: Sequence[tuple[float, float]],
) -> str:
    """Mapbox `exclude=point(lon lat),...`, nearest the direct corridor first.

    This is the fix for "rerouting is not stable". Picking the least exposed of
    the two or three alternatives a router volunteers only works when one of
    them happens to miss the block; in a dense grid none of them does, so the
    least-bad one was chosen, stayed exposed, and was redrawn on every re-plan.
    Excluding the point makes the router itself find a path that avoids it.
    """
    ranked = sorted(relevant, key=lambda b: point_segment_km(b, origin, destination))
    return ",".join(f"point({b[0]:.5f} {b[1]:.5f})" for b in ranked[:MAX_EXCLUDE_POINTS])


def _detour_waypoints(
    origin: tuple[float, float],
    destination: tuple[float, float],
    block: tuple[float, float],
) -> list[tuple[float, float]]:
    """Two points either side of a block, perpendicular to the trip."""
    ref = (origin[1] + destination[1]) / 2.0
    ox, oy = _xy_km(origin, ref)
    dx, dy = _xy_km(destination, ref)
    vx, vy = dx - ox, dy - oy
    norm = math.hypot(vx, vy) or 1.0
    nx, ny = -vy / norm, vx / norm
    k = math.pi / 180.0 * EARTH_RADIUS_KM
    out = []
    for side in (1, -1):
        east = nx * DETOUR_OFFSET_KM * side
        north = ny * DETOUR_OFFSET_KM * side
        out.append((
            block[0] + east / (k * math.cos(math.radians(ref))),
            block[1] + north / k,
        ))
    return out


def _line_from(route: dict, engine: str, exposure_count: int, steps: list[Step],
               considered: int, avoided: int, avoidance: str) -> RouteLine:
    coords = (route.get("geometry") or {}).get("coordinates") or []
    return RouteLine(
        coordinates=[[float(c[0]), float(c[1])] for c in coords],
        km=round(float(route.get("distance") or 0.0) / 1000.0, 2),
        minutes=max(1, round(float(route.get("duration") or 0.0) / 60.0))
        + (int(BLOCKED_DETOUR_MINUTES) if exposure_count else 0),
        engine=engine,
        passes_near_blocks=exposure_count,
        steps=steps,
        considered=considered,
        avoided=avoided,
        avoidance=avoidance if avoided else "",
    )


async def _mapbox_routes(o: str, d: str, exclude: str = "") -> list[dict]:
    params = {
        "access_token": settings.mapbox_token,
        "geometries": "geojson",
        "overview": "full",
        "alternatives": "true",
        "steps": "true",
        "language": "en",
    }
    if exclude:
        params["exclude"] = exclude
    payload = _data(
        await get_json(
            f"{settings.mapbox_directions_url}/driving/{o};{d}",
            params,
            cache_key=f"mbx:route:{o}:{d}:{hash(exclude)}",
        )
    )
    return (payload or {}).get("routes") or []


async def _osrm_routes(coords: str) -> list[dict]:
    payload = _data(
        await get_json(
            f"{settings.osrm_url}/route/v1/driving/{coords}",
            {"overview": "full", "geometries": "geojson", "alternatives": "true",
             # Without steps OSRM returns geometry and no instructions, and the
             # citizen app has nothing to say after "here is a line".
             "steps": "true"},
            cache_key=f"osrm:route:{coords}",
        )
    )
    return (payload or {}).get("routes") or []


async def route_line(
    origin: tuple[float, float],
    destination: tuple[float, float],
    blocked: Sequence[tuple[float, float]] = (),
) -> RouteLine:
    """Street geometry from A to B, going round the hazards people reported.

    Order of attempts, each only when the previous left the path exposed:

      1. Mapbox with the blocks *excluded*, so the router itself avoids them.
      2. Mapbox without exclusion (an excluded point can make a trip
         impossible), least exposed of its alternatives.
      3. OSRM, least exposed alternative; if still exposed, a via point placed
         either side of the first block it crosses.
      4. A straight line, labelled as one.

    Blocks at either end of the trip are ignored for this trip only (see
    `relevant_blocks`).
    """
    o = f"{origin[0]:.5f},{origin[1]:.5f}"
    d = f"{destination[0]:.5f},{destination[1]:.5f}"
    relevant = relevant_blocks(origin, destination, blocked)

    if settings.mapbox_token:
        if relevant:
            routes = await _mapbox_routes(o, d, _exclude_param(origin, destination, relevant))
            if routes:
                best, exp = _pick(routes, blocked)
                return _line_from(best, "mapbox", exp, _mapbox_steps(best),
                                  len(routes), len(relevant), "exclude")
            log.info("mapbox_exclude_no_route", origin=o, destination=d,
                     excluded=len(relevant))
        routes = await _mapbox_routes(o, d)
        if routes:
            best, exp = _pick(routes, blocked)
            return _line_from(best, "mapbox", exp, _mapbox_steps(best), len(routes),
                              len(relevant) - exp if relevant else 0, "alternatives")
        # A token is set and Mapbox still gave us nothing. `get_json` never
        # raises, so this used to fall through to OSRM in complete silence and
        # the only visible symptom was directions that named no streets. A bad
        # scope, a URL restriction on the token, or a 422 all land here.
        log.warning("mapbox_route_empty", origin=o, destination=d,
                    note="falling back to OSRM; check the token's scopes and "
                         "URL restrictions")

    routes = await _osrm_routes(f"{o};{d}")
    if routes:
        best, exp = _pick(routes, blocked)
        considered = len(routes)
        avoidance = "alternatives"
        if exp and relevant:
            coords = (best.get("geometry") or {}).get("coordinates") or []
            first = _exposed_blocks(coords, relevant)
            candidates = [best]
            if first:
                for via in _detour_waypoints(origin, destination, first[0]):
                    v = f"{via[0]:.5f},{via[1]:.5f}"
                    candidates.extend(await _osrm_routes(f"{o};{v};{d}"))
            considered = len(routes) + len(candidates) - 1
            chosen, chosen_exp = _pick(candidates, blocked)
            if chosen_exp < exp:
                best, exp, avoidance = chosen, chosen_exp, "detour"
        return _line_from(best, "osrm", exp, _osrm_steps(best), considered,
                          len(relevant) - exp if relevant else 0, avoidance)

    km = haversine_km(origin, destination)
    detour = BLOCKED_DETOUR_MINUTES if _segment_is_blocked(origin, destination, blocked) else 0.0
    log.warning("route_fallback", origin=o, destination=d)
    return RouteLine(
        coordinates=[[origin[0], origin[1]], [destination[0], destination[1]]],
        km=round(km * 1.35, 2),
        minutes=max(1, round(km * 1.35 / EVENT_SPEED_KMH * 60 + detour)),
        engine="straight-line-fallback",
        passes_near_blocks=1 if detour else 0,
        # One honest step rather than none. With no steps at all the phone shows
        # no navigation whatsoever, which is a worse answer than a bearing and a
        # distance — and the instruction says plainly that no street was
        # consulted, so nobody mistakes it for a route.
        steps=[
            Step(
                instruction=(
                    f"Head {_bearing_word(origin, destination)} towards the "
                    "destination. No street router was reachable, so this is a "
                    "direction and a distance, not a route."
                ),
                street="",
                distance_m=int(round(km * 1.35 * 1000)),
            )
        ],
    )


def _bearing_word(a: tuple[float, float], b: tuple[float, float]) -> str:
    """Eight-point compass. Enough to start walking the right way."""
    import math

    dx = (b[0] - a[0]) * math.cos(math.radians((a[1] + b[1]) / 2))
    dy = b[1] - a[1]
    deg = (math.degrees(math.atan2(dx, dy)) + 360) % 360
    return ["north", "north-east", "east", "south-east",
            "south", "south-west", "west", "north-west"][int((deg + 22.5) % 360 // 45)]


# ---------------------------------------------------------------- snapping ---
@dataclass(slots=True)
class Snapped:
    lng: float
    lat: float
    street: str
    #: How far the original point had to move to reach a road, in metres.
    moved_m: int
    engine: str

    @property
    def on_road(self) -> bool:
        return self.engine != "none"


#: Snapping is a network call and a flood report lands roughly where the last
#: one did. Cached on a ~40 m grid, which is finer than the accuracy of a phone
#: GPS in a built-up street.
_snap_cache: dict[tuple[int, int], Snapped] = {}


def _grid(lng: float, lat: float) -> tuple[int, int]:
    return (round(lng * 2800), round(lat * 2800))


async def snap_to_road(lng: float, lat: float) -> Snapped:
    """Move a point onto the nearest routable road, and name that road.

    A hazard that sits in the middle of a block is not on anybody's way
    anywhere: no route passes near it, so no route is ever diverted by it, and
    the avoidance logic has nothing to bite on. Snapping first is what makes
    "flooded road" mean a road.

    Mapbox returns the snapped coordinate and the street name as
    `waypoints[0]`; a route is requested to a point a few hundred metres away
    because the Directions API needs two coordinates to answer at all.
    """
    key = _grid(lng, lat)
    hit = _snap_cache.get(key)
    if hit is not None:
        return hit

    result = Snapped(lng=lng, lat=lat, street="", moved_m=0, engine="none")
    if settings.mapbox_token:
        near = (lng + 0.0030, lat + 0.0030)
        payload = _data(
            await get_json(
                f"{settings.mapbox_directions_url}/driving/"
                f"{lng:.5f},{lat:.5f};{near[0]:.5f},{near[1]:.5f}",
                {
                    "access_token": settings.mapbox_token,
                    "geometries": "geojson",
                    "overview": "simplified",
                    "steps": "false",
                },
                cache_key=f"mbx:snap:{lng:.4f},{lat:.4f}",
            )
        )
        points = (payload or {}).get("waypoints") or []
        if points:
            loc = points[0].get("location") or []
            if len(loc) == 2:
                snapped = (float(loc[0]), float(loc[1]))
                result = Snapped(
                    lng=snapped[0],
                    lat=snapped[1],
                    street=(points[0].get("name") or "").strip(),
                    moved_m=int(round(haversine_km((lng, lat), snapped) * 1000)),
                    engine="mapbox",
                )

    _snap_cache[key] = result
    return result
