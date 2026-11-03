"""Seed the Pune reference dataset.

Idempotent: safe to run repeatedly. Reference geography is upserted; nothing
operational (runs, incidents, decisions) is touched, so seeding a live database
cannot destroy an event record.

    python -m app.seed.seed_pune

Uses the direct connection rather than the pooler: pgbouncer in transaction
mode cannot hold the advisory lock this takes.
"""

from __future__ import annotations

import asyncio
import math

import asyncpg

from app.core.config import settings
from app.core.logging import configure_logging, get_logger

log = get_logger(__name__)

# (number, name, lng, lat, population, elderly_share, elevation_m, area_sq_km)
WARDS = [
    ("12", "Kasba–Somwar Peth", 73.8567, 18.5196, 62400, 0.16, 553, 2.1),
    ("15", "Vitthalwadi–Ekta Nagar", 73.8331, 18.4762, 48900, 0.11, 548, 2.6),
    ("18", "Sinhagad Road", 73.8262, 18.4703, 71200, 0.09, 559, 4.2),
    ("21", "Karve Nagar", 73.8155, 18.4921, 58300, 0.12, 567, 3.1),
    ("23", "Kothrud", 73.8074, 18.5074, 94100, 0.14, 574, 5.0),
    ("27", "Warje–Malwadi", 73.7998, 18.4783, 52700, 0.10, 588, 3.7),
    ("31", "Shivajinagar", 73.8492, 18.5305, 66800, 0.15, 560, 2.8),
    ("34", "Deccan Gymkhana", 73.8412, 18.5163, 41200, 0.18, 556, 1.9),
    ("39", "Yerwada", 73.8843, 18.5524, 88600, 0.08, 545, 5.6),
    ("42", "Kalyani Nagar", 73.9002, 18.5478, 39400, 0.11, 542, 2.4),
    ("47", "Hadapsar", 73.9301, 18.4998, 102300, 0.07, 561, 7.1),
    ("51", "Kondhwa Khurd", 73.8884, 18.4631, 76500, 0.09, 592, 4.8),
    ("55", "Katraj", 73.8578, 18.4482, 64900, 0.08, 620, 5.3),
    ("58", "Aundh", 73.8071, 18.5583, 47800, 0.13, 570, 3.3),
]

LIFELINES = [
    ("lf-1", "hospital", "Sassoon General Hospital", "w-12", 0.3, 0.4, None),
    ("lf-2", "hospital", "Deenanath Mangeshkar Hospital", "w-21", -0.4, 0.2, None),
    ("lf-3", "hospital", "Ruby Hall Clinic", "w-31", 0.5, -0.3, None),
    ("lf-4", "hospital", "Noble Hospital", "w-47", -0.6, 0.5, None),
    ("lf-5", "school", "Vitthalwadi Municipal School", "w-15", 0.2, -0.3, None),
    ("lf-6", "school", "Sinhagad Road Primary School", "w-18", -0.5, 0.6, None),
    ("lf-7", "school", "Kothrud Vidyalaya", "w-23", 0.7, 0.1, None),
    ("lf-8", "school", "Yerwada Zilla Parishad School", "w-39", -0.3, -0.6, None),
    ("lf-9", "school", "Hadapsar English Medium School", "w-47", 0.9, -0.4, None),
    ("lf-10", "pump_station", "Vitthalwadi Storm Pump", "w-15", -0.3, 0.2, None),
    ("lf-11", "pump_station", "Mutha Right Bank Pump", "w-18", 0.4, -0.5, None),
    ("lf-12", "substation", "Kalyani Nagar 33kV", "w-42", 0.2, 0.3, None),
    ("lf-13", "substation", "Warje 22kV", "w-27", -0.4, -0.2, None),
    ("lf-14", "shelter", "Kothrud Community Hall", "w-23", -0.6, -0.4, 450),
    ("lf-15", "shelter", "Karve Nagar Samaj Mandir", "w-21", 0.5, -0.5, 300),
    ("lf-16", "shelter", "Sinhagad Road Municipal Hall", "w-18", 0.8, 0.3, 520),
    ("lf-17", "shelter", "Shivajinagar Ward Office Hall", "w-31", -0.4, 0.5, 280),
    ("lf-18", "shelter", "Hadapsar Sports Complex", "w-47", 0.3, 0.8, 700),
    ("lf-19", "shelter", "Yerwada Municipal School Hall", "w-39", 0.6, 0.4, 380),
    ("lf-20", "shelter", "Katraj Zilla Parishad Hall", "w-55", -0.2, 0.6, 260),
]

RESOURCES = [
    ("res-1", "boat", "Rescue Boat R-1", "PMC Fire Brigade", "w-18", 8),
    ("res-2", "boat", "Rescue Boat R-2", "PMC Fire Brigade", "w-12", 8),
    ("res-3", "boat", "Rescue Boat R-3", "NDRF 5th Bn", "w-39", 12),
    ("res-4", "pump", "Dewatering Pump P-1", "PMC Drainage", "w-15", 1),
    ("res-5", "pump", "Dewatering Pump P-2", "PMC Drainage", "w-18", 1),
    ("res-6", "pump", "Dewatering Pump P-3", "PMC Drainage", "w-23", 1),
    ("res-7", "pump", "Dewatering Pump P-4", "PMC Drainage", "w-47", 1),
    ("res-8", "ambulance", "Ambulance A-1", "PMC Health", "w-12", 2),
    ("res-9", "ambulance", "Ambulance A-2", "PMC Health", "w-31", 2),
    ("res-10", "ambulance", "Ambulance A-3", "108 Service", "w-47", 2),
    ("res-11", "fire_engine", "Fire Tender F-1", "PMC Fire Brigade", "w-21", 6),
    ("res-12", "fire_engine", "Fire Tender F-2", "PMC Fire Brigade", "w-39", 6),
    ("res-13", "rescue_team", "Rescue Team T-1", "NDRF 5th Bn", "w-18", 10),
    ("res-14", "rescue_team", "Rescue Team T-2", "PMC Disaster Cell", "w-31", 8),
    ("res-15", "bus", "PMPML Bus B-1", "PMPML", "w-23", 45),
    ("res-16", "bus", "PMPML Bus B-2", "PMPML", "w-21", 45),
    ("res-17", "bus", "PMPML Bus B-3", "PMPML", "w-47", 45),
    ("res-18", "jcb", "JCB Unit J-1", "PMC Works", "w-27", 1),
]

KM_PER_DEG_LAT = 110.57
KM_PER_DEG_LNG = 105.6  # at ~18.5°N


def ward_ring(cx: float, cy: float, radius_km: float, seed: int) -> str:
    """A deterministic, slightly irregular outline.

    Stand-in for the PMC ward shapefile: same ids, same centroids, so swapping
    in the real geometry is an UPDATE and nothing above the database changes.
    """
    points: list[tuple[float, float]] = []
    n = 9
    s = seed

    def rand() -> float:
        nonlocal s
        s = (s * 9301 + 49297) % 233280
        return s / 233280

    for i in range(n):
        angle = (i / n) * math.tau
        r = radius_km * (0.78 + rand() * 0.44)
        points.append(
            (cx + (math.cos(angle) * r) / KM_PER_DEG_LNG,
             cy + (math.sin(angle) * r) / KM_PER_DEG_LAT)
        )
    points.append(points[0])
    body = ", ".join(f"{x:.6f} {y:.6f}" for x, y in points)
    return f"SRID=4326;POLYGON(({body}))"


async def seed(conn: asyncpg.Connection) -> None:
    for i, (num, name, cx, cy, pop, eld, elev, area) in enumerate(WARDS):
        await conn.execute(
            """
            insert into wards (id, number, name, boundary, centroid, population,
                               elderly_share, elevation_m, area_sq_km)
            values ($1, $2, $3,
                    extensions.ST_GeogFromText($4),
                    extensions.ST_GeogFromText($5),
                    $6, $7, $8, $9)
            on conflict (id) do update
              set name = excluded.name,
                  boundary = excluded.boundary,
                  population = excluded.population
            """,
            f"w-{num}", num, name,
            ward_ring(cx, cy, math.sqrt(area) * 0.62, 1000 + i * 137),
            f"SRID=4326;POINT({cx} {cy})",
            pop, eld, elev, area,
        )
    log.info("seeded_wards", count=len(WARDS))

    for lid, kind, name, ward, dx, dy, capacity in LIFELINES:
        await conn.execute(
            """
            insert into lifelines (id, kind, name, ward_id, location, capacity)
            select $1, $2::lifeline_kind, $3, $4,
                   extensions.ST_SetSRID(extensions.ST_MakePoint(
                     extensions.ST_X(w.centroid::extensions.geometry) + $5 / 105.6,
                     extensions.ST_Y(w.centroid::extensions.geometry) + $6 / 110.57
                   ), 4326)::extensions.geography,
                   $7
            from wards w where w.id = $4
            on conflict (id) do nothing
            """,
            lid, kind, name, ward, dx, dy, capacity,
        )
    log.info("seeded_lifelines", count=len(LIFELINES))

    for rid, kind, label, operator, ward, capacity in RESOURCES:
        await conn.execute(
            """
            insert into resources (id, kind, label, operator, base_location,
                                   location, capacity, status)
            select $1, $2::resource_kind, $3, $4, w.centroid, w.centroid, $6, 'available'
            from wards w where w.id = $5
            on conflict (id) do nothing
            """,
            rid, kind, label, operator, ward, capacity,
        )
    log.info("seeded_resources", count=len(RESOURCES))


async def main() -> None:
    configure_logging(settings.log_level)
    dsn = settings.migration_dsn
    if not dsn:
        raise SystemExit("Set SUPABASE_DIRECT_CONNECTION_STRING before seeding.")
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            await seed(conn)
    finally:
        await conn.close()
    log.info("seed_complete")


if __name__ == "__main__":
    asyncio.run(main())
