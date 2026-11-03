"""What the Command Agent is allowed to know, and how it finds it out.

The rule that makes this defensible: **the model never sees the database and
never invents a number.** It sees the catalogue below, chooses a tool, and gets
back a structured result. Everything it then says is written from that result.
Take the model away and the tools still answer; the prose just gets plainer.

Three tiers, and the boundary between the second and third is the policy gate:

* **read** — facts. Cheap, safe, and the only tier a question ever needs.
* **analyse** — facts put through the solver or the forecaster. Still writes
  nothing; `simulate_*` re-solves the real allocation model on a copy.
* **act** — proposes. Never executes directly: `propose_action` goes through
  `agents.gate`, which decides whether a person has to see it first.

Each tool declares its parameters so the catalogue can be handed to a model and
shown to a user on the same screen. A commissioner who can see the list can see
the limits, which is the more important half.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.agents import forecast as forecasting
from app.agents import gate
from app.agents import policy
from app.copilot import simulate, strategies
from app.core.logging import get_logger
from app.db import session as db
from app import taxonomy

log = get_logger(__name__)

Tier = str  # "read" | "analyse" | "act"


@dataclass(slots=True)
class Tool:
    name: str
    tier: Tier
    description: str
    params: dict[str, str] = field(default_factory=dict)
    fn: Callable[..., Awaitable[Any]] | None = None

    def as_dict(self) -> dict:
        return {"name": self.name, "tier": self.tier,
                "description": self.description, "params": self.params}


REGISTRY: dict[str, Tool] = {}


def tool(name: str, tier: Tier, description: str, **params: str):
    def wrap(fn):
        REGISTRY[name] = Tool(name=name, tier=tier, description=description,
                              params=params, fn=fn)
        return fn
    return wrap


# ------------------------------------------------------------------- read ---
@tool("get_situation", "read", "Headline counts: incidents, units, unmet demand, alerts.",
      city_id="City, default pune")
async def get_situation(city_id: str = "pune") -> dict:
    row = await db.fetchrow(
        """
        select
          (select count(*) from incidents
            where city_id = $1 and status <> 'resolved')                     incidents,
          (select count(*) from incidents
            where city_id = $1 and status <> 'resolved' and severity >= 4)   critical,
          (select count(*) from resources where city_id = $1)                units,
          (select count(*) from resources
            where city_id = $1 and status = 'available')                     available,
          (select count(*) from assignments
            where sim_run_id is null
              and status in ('proposed','approved','en_route','on_site'))    committed,
          (select coalesce(sum(greatest(0, n.required - n.met)), 0)
             from incident_needs n join incidents i on i.id = n.incident_id
            where i.status <> 'resolved')                                    unmet,
          (select count(*) from decisions
            where sim_run_id is null and status = 'awaiting_approval')       awaiting,
          (select count(*) from alerts where sim_run_id is null)             alerts,
          (select count(*) from citizen_reports
            where city_id = $1 and sim_run_id is null)                       reports,
          (select round(avg(eta_minutes))::int from assignments
            where sim_run_id is null
              and status in ('proposed','approved','en_route'))              avg_eta
        """,
        city_id,
    )
    return dict(row) if row else {}


@tool("rank_wards", "read",
      "Wards ordered by how bad it is: severity, exposure, unmet demand, cover.",
      city_id="City", limit="How many, default 8")
async def rank_wards(city_id: str = "pune", limit: int = 8) -> list[dict]:
    """The ranking is arithmetic over the current world, not a model's opinion.

    Four terms, each of which an officer can check independently: the worst open
    incident in the ward, how many there are, how many people the risk model has
    exposed there, and how much of the demand has nobody assigned. They are
    reported alongside the score rather than folded into it, so "why is 4 above
    7" has an answer that is four numbers instead of a vector.
    """
    rows = await db.fetch(
        """
        with open as (
          select i.ward_id,
                 count(*)                              incidents,
                 max(i.severity)                       worst,
                 sum(case when i.severity >= 4 then 1 else 0 end) critical
            from incidents i
           where i.city_id = $1 and i.status <> 'resolved'
           group by i.ward_id
        ),
        need as (
          select i.ward_id,
                 sum(greatest(0, n.required - n.met)) unmet
            from incident_needs n
            join incidents i on i.id = n.incident_id
           where i.status <> 'resolved'
           group by i.ward_id
        ),
        cover as (
          select a.ward_id, count(*) units, round(avg(a.eta_minutes))::int eta
            from assignments a
           where a.sim_run_id is null
             and a.status in ('proposed','approved','en_route','on_site')
           group by a.ward_id
        ),
        risk as (
          select distinct on (ward_id) ward_id, score, severity, population_at_risk
            from ward_risks order by ward_id, created_at desc
        )
        select w.id ward_id, w.name, w.population,
               coalesce(o.incidents, 0) incidents,
               coalesce(o.worst, 0)     worst_severity,
               coalesce(o.critical, 0)  critical,
               coalesce(n.unmet, 0)     unmet,
               coalesce(c.units, 0)     units_committed,
               c.eta                    avg_eta,
               r.score                  risk_score,
               r.severity               risk_severity,
               coalesce(r.population_at_risk, w.population / 8) exposed
          from wards w
          left join open  o on o.ward_id = w.id
          left join need  n on n.ward_id = w.id
          left join cover c on c.ward_id = w.id
          left join risk  r on r.ward_id = w.id
         where w.city_id = $1
         order by coalesce(o.worst,0) desc, coalesce(n.unmet,0) desc,
                  coalesce(r.population_at_risk, 0) desc
         limit $2
        """,
        city_id, int(limit),
    )
    out = []
    for rank, r in enumerate(rows, start=1):
        d = dict(r)
        d["rank"] = rank
        d["score"] = _ward_score(d)
        out.append(d)
    out.sort(key=lambda d: -d["score"])
    for rank, d in enumerate(out, start=1):
        d["rank"] = rank
    return out


def _ward_score(d: dict) -> int:
    """0-100, from four named terms. Deliberately simple and deliberately shown.

    A learned score would need labels nobody has, and an officer who cannot
    reproduce the ranking on paper will not act on it.
    """
    severity = (d.get("worst_severity") or 0) / 5.0            # 0-1
    unmet = min(1.0, (d.get("unmet") or 0) / 6.0)              # 0-1
    exposure = min(1.0, (d.get("exposed") or 0) / 25_000.0)    # 0-1
    volume = min(1.0, (d.get("incidents") or 0) / 8.0)         # 0-1
    cover = 1.0 if (d.get("units_committed") or 0) == 0 else 0.4
    raw = 0.40 * severity + 0.25 * unmet + 0.20 * exposure + 0.15 * volume
    return int(round(100 * min(1.0, raw * (0.75 + 0.25 * cover) + 0.0)))


@tool("get_incidents", "read", "Open incidents, worst first.",
      city_id="City", ward_id="Optional ward filter", limit="Default 20")
async def get_incidents(city_id: str = "pune", ward_id: str | None = None,
                        limit: int = 20) -> list[dict]:
    rows = await db.fetch(
        """
        select i.id::text, i.title, i.category, i.ward_id, w.name ward_name,
               i.severity, i.status::text status, i.report_count, i.confidence,
               i.trust_score, i.street, i.created_at,
               (select count(*) from assignments a
                 where a.incident_id = i.id
                   and a.status in ('proposed','approved','en_route','on_site'))::int units,
               coalesce((select sum(greatest(0, n.required - n.met))
                           from incident_needs n where n.incident_id = i.id), 0) unmet
          from incidents i join wards w on w.id = i.ward_id
         where i.city_id = $1 and i.status <> 'resolved'
           and ($2::text is null or i.ward_id = $2)
         order by i.severity desc, i.created_at desc
         limit $3
        """,
        city_id, ward_id, int(limit),
    )
    return [dict(r) for r in rows]


@tool("get_resources", "read", "The fleet: kind, operator, status, what it is on.",
      city_id="City", capability="Optional capability filter", status="Optional status filter")
async def get_resources(city_id: str = "pune", capability: str | None = None,
                        status: str | None = None) -> list[dict]:
    rows = await db.fetch(
        """
        select r.id, r.kind, r.label, r.operator, r.agency_id, r.capacity,
               r.status::text status, a.incident_id::text incident_id,
               i.title incident_title, a.eta_minutes, a.ward_id
          from resources r
          left join lateral (
            select * from assignments x
             where x.resource_id = r.id and x.sim_run_id is null
               and x.status in ('proposed','approved','en_route','on_site')
             order by x.created_at desc limit 1
          ) a on true
          left join incidents i on i.id = a.incident_id
         where r.city_id = $1 and ($2::text is null or r.status::text = $2)
         order by r.kind, r.label
        """,
        city_id, status,
    )
    out = [dict(r) for r in rows]
    if capability:
        kinds = set(taxonomy.cache.kinds_providing(capability))
        out = [r for r in out if r["kind"] in kinds]
    for r in out:
        r["capabilities"] = sorted(
            getattr(taxonomy.cache.resource_kinds.get(r["kind"]), "capabilities", []) or []
        )
    return out


@tool("get_facilities", "read", "Hospitals, shelters, relief centres and their stock.",
      city_id="City", kind="Optional lifeline kind")
async def get_facilities(city_id: str = "pune", kind: str | None = None) -> list[dict]:
    rows = await db.fetch(
        """
        select l.id, l.name, l.kind, k.display_name kind_label, l.status,
               l.capacity, l.occupancy, l.supplies, l.ward_id,
               l.people_served_per_hour
          from lifelines l left join lifeline_kinds k on k.id = l.kind
         where l.city_id = $1 and ($2::text is null or l.kind = $2)
         order by l.kind, l.name
        """,
        city_id, kind,
    )
    return [dict(r) for r in rows]


@tool("get_forecast", "read", "Projected recurrence by ward, facility saturation, capability demand.",
      city_id="City")
async def get_forecast(city_id: str = "pune") -> dict:
    return forecasting.as_dict(await forecasting.build(city_id=city_id))


@tool("get_decisions", "read", "Decisions and what the gate did with them.",
      status="Optional status filter", limit="Default 20")
async def get_decisions(status: str | None = None, limit: int = 20) -> list[dict]:
    rows = await db.fetch(
        """
        select id::text, action, action_key, target, ward_id, rationale,
               confidence, status::text status, authority, decided_by, created_at
          from decisions
         where sim_run_id is null and ($1::text is null or status::text = $1)
         order by (status = 'awaiting_approval') desc, created_at desc
         limit $2
        """,
        status, int(limit),
    )
    return [dict(r) for r in rows]


@tool("get_alerts", "read", "Advisories actually issued to the public.", limit="Default 15")
async def get_alerts(limit: int = 15) -> list[dict]:
    rows = await db.fetch(
        """
        select a.id::text, a.ward_id, w.name ward_name, a.headline, a.action,
               a.severity, a.reach, a.issued_at, a.decision_id::text decision_id
          from alerts a left join wards w on w.id = a.ward_id
         where a.sim_run_id is null order by a.issued_at desc limit $1
        """,
        int(limit),
    )
    return [dict(r) for r in rows]


@tool("get_sensor_field", "read",
      "LoRa field sensor nodes: per node the latest human-presence, structural-movement and "
      "gas/heat scores (0..1), what drove them (sound, tapping on rubble, lean, gas rise...), "
      "flags, raw readings and location. Use it before sending people into a building or "
      "when asked whether anyone is trapped. Nodes named SIM-* are simulated.",
      city_id="City")
async def get_sensor_field(city_id: str = "pune") -> list[dict]:
    from app.iot import service as iot

    return await iot.field_summary(city_id)


@tool("get_agency_status", "read", "Which agency holds what, and open mutual-aid requests.",
      city_id="City")
async def get_agency_status(city_id: str = "pune") -> dict:
    requests = await db.fetch(
        """
        select r.id::text, r.from_agency, r.to_agency, r.capability_id, r.quantity,
               r.status, r.requested_at, f.name from_name, t.name to_name
          from agency_requests r
          left join agencies f on f.id = r.from_agency
          left join agencies t on t.id = r.to_agency
         order by r.requested_at desc limit 20
        """
    )
    return {
        "agencies": [
            {"id": a.id, "name": a.name, "kind": a.kind,
             "capabilities": sorted(a.capabilities)}
            for a in taxonomy.cache.agencies.values()
            if a.city_id == city_id and getattr(a, "active", True)
        ],
        "requests": [dict(r) for r in requests],
    }


@tool("get_reports", "read", "Raw reports as they arrived, with trust and what they merged into.",
      city_id="City", incident_id="Optional incident filter", limit="Default 25")
async def get_reports(city_id: str = "pune", incident_id: str | None = None,
                      limit: int = 25) -> list[dict]:
    rows = await db.fetch(
        """
        select r.id::text, r.note, r.category, r.source, r.reporter_name,
               r.trust_score, r.verification_status, r.street, r.ward_id,
               r.created_at, r.photo_path is not null has_photo,
               r.incident_id::text incident_id, l.link_score
          from citizen_reports r
          left join report_links l on l.report_id = r.id
         where r.city_id = $1 and r.sim_run_id is null
           and ($2::text is null or r.incident_id = $2::uuid)
         order by r.created_at desc limit $3
        """,
        city_id, incident_id, int(limit),
    )
    return [dict(r) for r in rows]


@tool("get_policy", "read", "The clause governing an action, and who holds it.",
      action_key="e.g. evacuate_school, requisition_ndrf, reallocate_unit")
async def get_policy(action_key: str) -> dict:
    clause = await policy.find_clause(action_key)
    if clause is None:
        return {"actionKey": action_key, "found": False,
                "note": "No clause authorises this, so it escalates by default."}
    return {
        "actionKey": action_key, "found": True, "clause": clause.clause,
        "source": clause.source, "delegatedTo": clause.delegated_to,
        "maxSeverityWithoutEscalation": clause.max_severity_without_escalation,
        "body": clause.body,
    }


@tool("get_timeline", "read", "What happened, in order, from the audit log.", limit="Default 30")
async def get_timeline(limit: int = 30) -> list[dict]:
    rows = await db.fetch(
        """
        with mark as (
          select coalesce(max(id), 0) id from events
           where sim_run_id is null and kind = 'world.reset'
        )
        select e.id, e.kind, e.actor, e.subject_type, e.subject_id, e.ward_id,
               e.payload, e.occurred_at, e.causation_id
          from events e, mark
         where e.sim_run_id is null and e.id > mark.id
         order by e.id desc limit $1
        """,
        int(limit),
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- analyse ---
@tool("explain_priority", "analyse",
      "Why one ward ranks where it does, term by term, against another.",
      ward_id="The ward", against="Optional ward to compare with", city_id="City")
async def explain_priority(ward_id: str, against: str | None = None,
                           city_id: str = "pune") -> dict:
    ranked = await rank_wards(city_id=city_id, limit=60)
    by_id = {r["ward_id"]: r for r in ranked}
    a = by_id.get(ward_id)
    if a is None:
        return {"error": f"{ward_id} is not in this city or has nothing open."}
    terms = _terms(a)
    out = {"ward": a, "terms": terms}
    if against and against in by_id:
        b = by_id[against]
        out["against"] = b
        out["comparison"] = [
            {"term": t["term"], "a": t["value"], "b": bt["value"],
             "weight": t["weight"],
             "contribution_a": t["contribution"], "contribution_b": bt["contribution"]}
            for t, bt in zip(terms, _terms(b))
        ]
    return out


def _terms(d: dict) -> list[dict]:
    severity = (d.get("worst_severity") or 0) / 5.0
    unmet = min(1.0, (d.get("unmet") or 0) / 6.0)
    exposure = min(1.0, (d.get("exposed") or 0) / 25_000.0)
    volume = min(1.0, (d.get("incidents") or 0) / 8.0)
    return [
        {"term": "Worst open incident", "value": d.get("worst_severity") or 0,
         "normalised": round(severity, 3), "weight": 0.40,
         "contribution": round(40 * severity, 1)},
        {"term": "Unmet demand", "value": d.get("unmet") or 0,
         "normalised": round(unmet, 3), "weight": 0.25,
         "contribution": round(25 * unmet, 1)},
        {"term": "People exposed", "value": d.get("exposed") or 0,
         "normalised": round(exposure, 3), "weight": 0.20,
         "contribution": round(20 * exposure, 1)},
        {"term": "Open incidents", "value": d.get("incidents") or 0,
         "normalised": round(volume, 3), "weight": 0.15,
         "contribution": round(15 * volume, 1)},
    ]


@tool("evidence_for", "analyse", "Everything the system believes about one incident, and why.",
      incident_id="The incident")
async def evidence_for(incident_id: str) -> dict:
    incident = await db.fetchrow(
        """
        select i.id::text, i.title, i.category, i.severity, i.status::text status,
               i.report_count, i.confidence, i.trust_score, i.street,
               i.created_at, w.name ward_name, i.ward_id
          from incidents i join wards w on w.id = i.ward_id
         where i.id = $1::uuid
        """,
        incident_id,
    )
    if incident is None:
        return {"error": "No such incident."}
    reports = await get_reports(incident_id=incident_id, limit=50)
    needs = await db.fetch(
        "select capability_id, required, met from incident_needs where incident_id = $1::uuid",
        incident_id,
    )
    units = await db.fetch(
        """
        select r.label, r.kind, r.operator, a.status::text status, a.eta_minutes
          from assignments a join resources r on r.id = a.resource_id
         where a.incident_id = $1::uuid and a.sim_run_id is null
           and a.status in ('proposed','approved','en_route','on_site')
        """,
        incident_id,
    )
    sources: dict[str, int] = {}
    for r in reports:
        sources[r["source"] or "unknown"] = sources.get(r["source"] or "unknown", 0) + 1
    return {
        "incident": dict(incident),
        "reports": reports,
        "sources": sources,
        "photos": sum(1 for r in reports if r.get("has_photo")),
        "needs": [dict(n) for n in needs],
        "units": [dict(u) for u in units],
    }


@tool("simulate_reallocation", "analyse",
      "Re-solve the plan with a unit committed by hand. Writes nothing.",
      resource_id="Unit to move", incident_id="Where to send it", city_id="City")
async def simulate_reallocation(resource_id: str, incident_id: str,
                                city_id: str = "pune") -> dict:
    world = await simulate.inputs(city_id)
    now, proposed = await simulate.compare(
        world, label="Proposed", pin={resource_id: incident_id}
    )
    return simulate.as_table(now, proposed, world)


@tool("simulate_surge", "analyse",
      "Re-solve with extra demand that has not happened yet. Writes nothing.",
      ward_id="Where", capability="What is wanted", count="How many", city_id="City")
async def simulate_surge(ward_id: str, capability: str, count: int = 10,
                         city_id: str = "pune") -> dict:
    world = await simulate.inputs(city_id)
    now, proposed = await simulate.compare(
        world, label="With surge", surge=[(ward_id, capability, int(count))]
    )
    return simulate.as_table(now, proposed, world)


@tool("simulate_withdrawal", "analyse",
      "Re-solve with units held in reserve. Writes nothing.",
      resource_ids="Units to hold back", city_id="City")
async def simulate_withdrawal(resource_ids: list[str], city_id: str = "pune") -> dict:
    world = await simulate.inputs(city_id)
    now, proposed = await simulate.compare(
        world, label="Held back", withdraw=list(resource_ids)
    )
    return simulate.as_table(now, proposed, world)


@tool("generate_strategies", "analyse",
      "Mitigation options for the next few hours, each priced by the solver, "
      "each with its drawbacks.",
      city_id="City")
async def generate_strategies(city_id: str = "pune") -> dict:
    return await strategies.generate(city_id)


# -------------------------------------------------------------------- act ---
@tool("propose_action", "act",
      "Put an action to the policy gate. Never executes on its own authority.",
      action_key="Action", action="What it is, in words", target="What it acts on",
      ward_id="Optional ward", rationale="Why", confidence="0-1", severity="1-5")
async def propose_action(action_key: str, action: str, target: str,
                         rationale: str, confidence: float = 0.8,
                         severity: int = 3, ward_id: str | None = None,
                         params: dict | None = None, city_id: str = "pune") -> dict:
    """The only tool that changes anything, and it changes it by asking.

    What comes back says which clause governed the action and whether it issued
    or is waiting for a person. The Copilot reports that verbatim; it does not
    get to characterise its own authority.
    """
    return await gate.propose(
        action_key=action_key, action=action, target=target, ward_id=ward_id,
        rationale=rationale, confidence=float(confidence), severity=int(severity),
        city_id=city_id, actor="agent:command", params=params or {},
    )


# ------------------------------------------------------------------ runner ---
@tool("get_operations", "read",
      "Every unit on a job: what it is doing, how far along, who set it, why, "
      "how often it was re-routed round a block, and any standing order on it.",
      city_id="City")
async def get_operations(city_id: str = "pune") -> dict:
    from app.ops import operations as ops

    return await ops.active(city_id)


@tool("preview_cancel", "analyse",
      "What taking a unit off its job would do: re-solves the plan without it "
      "(or with it redirected) and lists what it could usefully do instead.",
      resource_id="Unit", instead="replan | redirect | stage | hold | return_to_base")
async def preview_cancel(resource_id: str, instead: dict | None = None,
                         city_id: str = "pune") -> dict:
    from app.ops import operations as ops

    return await ops.preview(resource_id, instead or {"kind": "replan"}, city_id)


@tool("recall_memory", "read",
      "Standing orders, facts, lessons and past episodes relevant to a question. "
      "Words only; never a source of numbers.",
      query="What to recall")
async def recall_memory(query: str, city_id: str = "pune") -> list[dict]:
    from app.copilot import memory

    return await memory.recall(query, city_id=city_id)


async def call(name: str, **kwargs: Any) -> Any:
    """Invoke one tool by name, dropping arguments it does not take.

    A model that hallucinates an extra keyword should get an answer, not a 500.
    Unknown *tools* are still an error, because inventing a capability is a
    different kind of wrong from being sloppy about an argument.
    """
    entry = REGISTRY.get(name)
    if entry is None or entry.fn is None:
        raise KeyError(f"No tool called {name!r}.")
    signature = inspect.signature(entry.fn)
    accepted = {k: v for k, v in kwargs.items() if k in signature.parameters}
    dropped = sorted(set(kwargs) - set(accepted))
    if dropped:
        log.info("copilot_tool_args_dropped", tool=name, dropped=dropped)
    return await entry.fn(**accepted)


def catalogue(tier: Tier | None = None) -> list[dict]:
    return [t.as_dict() for t in REGISTRY.values() if tier is None or t.tier == tier]
