"""Duplicate effort.

The deduplication in `clustering.py` stops four reports of one flooded bridge
becoming four incidents. This module catches the more expensive mistake: two
agencies dispatching into the same incident because neither could see the
other's assignments.

That is the duplication that actually wastes rescue capacity, and it is the one
most systems never surface, because you cannot see it until reports have been
clustered into incidents in the first place.

Two shapes, both queries over what is already recorded:

  * **Same incident, two agencies.** Unambiguous. Usually a handoff that was
    never acknowledged, or two control rooms working the same phone call.
  * **Different incidents, overlapping ground.** Two open incidents within a
    short distance and time of each other, each with its own units en route.
    Sometimes correct, often one event that the clustering split because the
    reports arrived far enough apart. Flagged, never auto-merged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.db import session as db

#: Two incidents this close, this recent, are worth a second look.
OVERLAP_METRES = 400
OVERLAP_MINUTES = 60


@dataclass(slots=True)
class DuplicateEffort:
    kind: str                      # "same_incident" | "overlapping_incidents"
    incident_ids: list[str]
    ward_id: str | None
    agencies: list[str]
    resources: list[str]
    detail: str
    wasted_units: int


async def detect(
    *, city_id: str = "pune", sim_run_id: str | None = None
) -> list[DuplicateEffort]:
    """Everything currently being worked twice."""
    found: list[DuplicateEffort] = []

    same = await db.fetch(
        """
        select a.incident_id::text as incident_id,
               i.title, i.ward_id,
               array_agg(distinct coalesce(ag.short_name, r.operator)) as agencies,
               array_agg(distinct r.id)                                as resources,
               count(distinct coalesce(r.agency_id, r.operator))::int  as agency_count,
               count(*)::int                                           as unit_count
          from assignments a
          join incidents i  on i.id = a.incident_id
          join resources  r on r.id = a.resource_id
          left join agencies ag on ag.id = r.agency_id
         where a.incident_id is not null
           and a.status in ('proposed','approved','en_route','on_site')
           and i.city_id = $1
           and a.sim_run_id is not distinct from $2::uuid
         group by a.incident_id, i.title, i.ward_id
        having count(distinct coalesce(r.agency_id, r.operator)) > 1
        """,
        city_id, sim_run_id,
    )
    for r in same:
        found.append(
            DuplicateEffort(
                kind="same_incident",
                incident_ids=[r["incident_id"]],
                ward_id=r["ward_id"],
                agencies=[a for a in (r["agencies"] or []) if a],
                resources=list(r["resources"] or []),
                detail=(
                    f"{r['agency_count']} agencies have sent {r['unit_count']} units to "
                    f"\"{r['title']}\". Unless this was a deliberate joint response, "
                    "one of them can be released."
                ),
                wasted_units=max(0, r["unit_count"] - 1),
            )
        )

    overlap = await db.fetch(
        """
        select i1.id::text as a_id, i2.id::text as b_id,
               i1.title as a_title, i2.title as b_title, i1.ward_id,
               extensions.ST_Distance(i1.location, i2.location) as metres,
               abs(extract(epoch from (i1.created_at - i2.created_at)))/60.0 as minutes,
               (select count(*) from assignments x
                 where x.incident_id = i1.id and x.status::text not in ('complete','cancelled'))::int as a_units,
               (select count(*) from assignments x
                 where x.incident_id = i2.id and x.status::text not in ('complete','cancelled'))::int as b_units
          from incidents i1
          join incidents i2
            on i1.id < i2.id
           and i1.city_id = i2.city_id
           and i1.sim_run_id is not distinct from i2.sim_run_id
         where i1.city_id = $1
           and i1.sim_run_id is not distinct from $2::uuid
           and i1.status <> 'resolved' and i2.status <> 'resolved'
           and extensions.ST_DWithin(i1.location, i2.location, $3)
           and abs(extract(epoch from (i1.created_at - i2.created_at))) < $4 * 60
        """,
        city_id, sim_run_id, OVERLAP_METRES, OVERLAP_MINUTES,
    )
    for r in overlap:
        if not (r["a_units"] or r["b_units"]):
            continue  # nobody is working either one, so nothing is being wasted
        found.append(
            DuplicateEffort(
                kind="overlapping_incidents",
                incident_ids=[r["a_id"], r["b_id"]],
                ward_id=r["ward_id"],
                agencies=[],
                resources=[],
                detail=(
                    f"\"{r['a_title']}\" and \"{r['b_title']}\" are {r['metres']:.0f} m "
                    f"and {r['minutes']:.0f} min apart, with "
                    f"{r['a_units']} and {r['b_units']} units committed. "
                    "These may be one event that was reported in two waves."
                ),
                wasted_units=min(r["a_units"], r["b_units"]),
            )
        )

    found.sort(key=lambda d: d.wasted_units, reverse=True)
    return found
