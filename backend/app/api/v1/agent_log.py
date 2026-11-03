"""The agent log, filed by hazard and sector (migration 030).

    GET /agent-log/board   hazard x sector grid: decisions, reroutes, holds,
                           guardrail trips, agent steps, in the last N hours
    GET /agent-log         the entries behind one cell (or any filter)
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.core.security import StaffPrincipal
from app.db import session as db
from app.nav.sectors import name_of

router = APIRouter(tags=["agent-log"])

#: Event kinds the board counts, grouped the way an officer reads them.
GROUPS = {
    "decisions": ("decision.%", "plan.generated", "agent.graph_sector"),
    "reroutes": ("assignment.rerouted", "route.changed"),
    "holds": ("agent.graph_waiting",),
    "guardrails": ("guardrail.tripped",),
    "agent_steps": ("agent.step", "agent.episode_%"),
    "incidents": ("incident.opened", "incident.severity_changed"),
    "closures": ("road.blocked", "segment.status_changed"),
}


def _like_any(col: str, pats: tuple[str, ...]) -> str:
    return "(" + " or ".join(f"{col} like '{p}'" for p in pats) + ")"


@router.get("/agent-log/board")
async def board(_: StaffPrincipal, city_id: str = "pune",
                hours: int = Query(default=24, ge=1, le=24 * 14)) -> dict:
    cols = ",\n".join(f"count(*) filter (where {_like_any('kind', pats)}) as {g}" for g, pats in GROUPS.items())
    rows = await db.fetch(
        f"""
        select coalesce(sector_id, 'city') sector_id, coalesce(hazard_id, 'unknown') hazard_id,
               {cols}, max(recorded_at) last_at
          from agent_log
         where city_id = $1 and recorded_at > now() - make_interval(hours => $2)
         group by 1, 2
        """,
        city_id, hours,
    )
    cells = [dict(r) | {"sector_name": name_of(r["sector_id"] if r["sector_id"] != "city" else None)}
             for r in rows if any(r[g] for g in GROUPS)]
    cells.sort(key=lambda c: (-(c["decisions"] + c["guardrails"] + c["holds"]), c["sector_id"]))
    return {"hours": hours, "groups": list(GROUPS), "cells": cells,
            "sectors": sorted({c["sector_id"] for c in cells}),
            "hazards": sorted({c["hazard_id"] for c in cells})}


@router.get("/agent-log")
async def entries(_: StaffPrincipal, city_id: str = "pune", sector_id: str | None = None,
                  hazard_id: str | None = None, group: str | None = None,
                  limit: int = Query(default=100, ge=1, le=500)) -> dict:
    where = ["city_id = $1"]
    args: list = [city_id]
    if sector_id:
        args.append(sector_id)
        where.append(f"coalesce(sector_id, 'city') = ${len(args)}")
    if hazard_id:
        args.append(hazard_id)
        where.append(f"coalesce(hazard_id, 'unknown') = ${len(args)}")
    if group in GROUPS:
        where.append(_like_any("kind", GROUPS[group]))
    args.append(limit)
    rows = await db.fetch(
        f"""select id, recorded_at, kind, actor, subject_type, subject_id, ward_id,
                   sector_id, hazard_id, payload, causation_id
              from agent_log where {' and '.join(where)}
             order by id desc limit ${len(args)}""",
        *args,
    )
    return {"entries": [dict(r) for r in rows]}
