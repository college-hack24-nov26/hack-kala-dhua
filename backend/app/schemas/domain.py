"""Wire models.

These mirror `frontend/src/api/types.ts` exactly. When one changes the other
must change with it; that is the price of not generating one from the other,
and it is worth paying to keep both sides readable.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

LngLat = Annotated[list[float], Field(min_length=2, max_length=2)]
Severity = Annotated[int, Field(ge=1, le=5)]
UnitInterval = Annotated[float, Field(ge=0.0, le=1.0)]

# --------------------------------------------------------------- taxonomy ---
# Which hazards, incident categories, resource kinds and capabilities exist is
# reference DATA, held in tables, not a closed type in the code. Adding cyclone
# for Chennai is an INSERT plus one adapter file; no enum, no migration, no
# redeploy of the frontend. These aliases are what the wire models use, and
# `app.taxonomy` validates a value against the loaded reference tables.
CityId = Annotated[str, Field(min_length=1, max_length=40)]
HazardId = Annotated[str, Field(min_length=1, max_length=40)]
CategoryId = Annotated[str, Field(min_length=1, max_length=40)]
ResourceKindId = Annotated[str, Field(min_length=1, max_length=40)]
LifelineKindId = Annotated[str, Field(min_length=1, max_length=40)]
CapabilityId = Annotated[str, Field(min_length=1, max_length=40)]
AgencyId = Annotated[str, Field(min_length=1, max_length=40)]


class Camel(BaseModel):
    """Serialises as camelCase for the TypeScript client, parses either."""

    model_config = ConfigDict(
        alias_generator=lambda s: "".join(
            w if i == 0 else w.capitalize() for i, w in enumerate(s.split("_"))
        ),
        populate_by_name=True,
        from_attributes=True,
    )


class HazardType(StrEnum):
    """Named constants for the hazards seeded today.

    This is a convenience for readable code, NOT a constraint. Wire models are
    typed `HazardId` (a plain string checked against the `hazard_types` table),
    so a hazard that is not listed here works end to end the moment its row and
    its adapter exist. Do not narrow anything back to this enum.
    """

    FLOOD = "flood"
    HEAT = "heat"
    FIRE = "fire"
    AIR = "air"
    SEISMIC = "seismic"


class IncidentStatus(StrEnum):
    REPORTED = "reported"
    CONFIRMED = "confirmed"
    DISPATCHED = "dispatched"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"


class IncidentCategory(StrEnum):
    """Named constants for the categories seeded today. Same rule as
    `HazardType`: convenience, not a constraint. The authoritative list, along
    with each category's deduplication radius and time window, lives in the
    `incident_categories` table."""

    FLOODED_ROAD = "flooded_road"
    WATERLOGGING = "waterlogging"
    FALLEN_TREE = "fallen_tree"
    BLOCKED_DRAIN = "blocked_drain"
    STRUCTURAL_DAMAGE = "structural_damage"
    PERSON_STRANDED = "person_stranded"
    POWER_LINE = "power_line"
    HEAT_CASUALTY = "heat_casualty"


class DecisionStatus(StrEnum):
    AUTO_ISSUED = "auto_issued"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    OVERRIDDEN = "overridden"
    REJECTED = "rejected"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    ACCEPTED = "accepted"
    ON_SITE = "on_site"
    COMPLETE = "complete"
    #: Stood down by the planner or an officer (migration 021). Distinct from
    #: complete so a cancelled job is never counted as work done.
    CANCELLED = "cancelled"


# ------------------------------------------------------------- geography ---
class Ward(Camel):
    id: str
    city_id: CityId = "pune"
    number: str
    name: str
    centroid: LngLat
    boundary: list[LngLat]
    population: int
    elderly_share: float
    elevation_m: float
    area_sq_km: float


class Lifeline(Camel):
    id: str
    kind: LifelineKindId
    name: str
    location: LngLat
    ward_id: str
    capacity: int | None = None


class Shelter(Lifeline):
    kind: LifelineKindId = "shelter"
    capacity: int
    occupancy: int


# ------------------------------------------------------------------ risk ---
class RiskDriver(Camel):
    label: str
    contribution: UnitInterval
    detail: str


class WardRisk(Camel):
    ward_id: str
    hazard: HazardId
    score: UnitInterval
    severity: Severity
    lead_time_hours: float
    confidence: UnitInterval
    population_at_risk: int
    drivers: list[RiskDriver]
    projection: list[float]


class HazardRun(Camel):
    id: str
    hazard: HazardId
    started_at: datetime
    sources: list[str]
    ward_risks: list[WardRisk] = Field(default_factory=list)


# ------------------------------------------------------------- incidents ---
class CitizenReport(Camel):
    id: str
    incident_id: str | None
    ward_id: str
    category: CategoryId
    location: LngLat
    note: str
    photo_url: str | None = None
    created_at: datetime
    classified_as: CategoryId
    classification_confidence: UnitInterval
    reporter_name: str


class ReportCreate(Camel):
    ward_id: str
    category: CategoryId
    location: LngLat
    note: str = ""
    photo_url: str | None = None


class Incident(Camel):
    id: str
    title: str
    category: CategoryId
    hazard: HazardId
    ward_id: str
    location: LngLat
    severity: Severity
    status: IncidentStatus
    report_count: int
    confidence: UnitInterval
    created_at: datetime
    updated_at: datetime
    report_ids: list[str] = Field(default_factory=list)


# ------------------------------------------------------------- resources ---
class Resource(Camel):
    id: str
    kind: ResourceKindId
    label: str
    operator: str
    base_location: LngLat
    location: LngLat
    capacity: int
    status: Literal["available", "assigned", "en_route", "on_site", "offline"]
    assignment_id: str | None = None


class Assignment(Camel):
    id: str
    resource_id: str
    incident_id: str | None
    ward_id: str
    purpose: str
    eta_minutes: int
    distance_km: float
    status: Literal["proposed", "approved", "en_route", "on_site", "complete"]
    created_at: datetime


class UncoveredDemand(Camel):
    ward_id: str
    incident_id: str | None = None
    need: str
    reason: str
    shortfall: int


class SolverStats(Camel):
    engine: Literal["cp-sat", "greedy-fallback"]
    runtime_ms: int
    variables: int
    constraints: int
    coverage: UnitInterval


class AllocationPlan(Camel):
    id: str
    hazard: HazardId
    generated_at: datetime
    assignments: list[Assignment]
    uncovered: list[UncoveredDemand]
    objective: str
    solver: SolverStats


# ------------------------------------------------------------- decisions ---
class Authority(Camel):
    clause: str
    source: str
    delegated_to: str
    within_delegation: bool


class Decision(Camel):
    id: str
    created_at: datetime
    hazard: HazardId
    action: str
    target: str
    ward_id: str | None
    rationale: str
    confidence: UnitInterval
    authority: Authority
    status: DecisionStatus
    decided_by: str | None = None
    decided_at: datetime | None = None
    override_note: str | None = None
    agent_run_id: str | None = None


class DecisionAction(Camel):
    action: Literal["approve", "reject", "override"]
    note: str | None = None


# ------------------------------------------------------------ agent trace --
class AgentStep(Camel):
    id: str
    agent: str
    started_at: datetime
    duration_ms: int
    thought: str
    tool: str | None = None
    tool_input: str | None = None
    tool_output: str | None = None
    cited_clause: str | None = None
    status: Literal["ok", "fallback", "error"] = "ok"


class AgentRun(Camel):
    id: str
    hazard: HazardId
    trigger: str
    started_at: datetime
    finished_at: datetime | None
    engine: Literal["gemini", "bedrock", "fallback"]
    steps: list[AgentStep] = Field(default_factory=list)
    summary: str = ""


# ----------------------------------------------------------------- alerts --
class SafeLocation(Camel):
    name: str
    location: LngLat
    distance_km: float


class Alert(Camel):
    id: str
    ward_id: str
    hazard: HazardId
    severity: Severity
    headline: str
    action: str
    by_time: datetime
    safe_location: SafeLocation | None
    channels: list[str]
    issued_at: datetime
    decision_id: str | None
    language: str
    reach: int


# ------------------------------------------------------------ field tasks --
class FieldTask(Camel):
    id: str
    assignment_id: str | None
    operator: str
    resource_id: str
    title: str
    instruction: str
    location: LngLat
    ward_id: str
    status: TaskStatus
    accepted_at: datetime | None = None
    completed_at: datetime | None = None
    proof_note: str | None = None
    priority: Severity


class FieldTaskUpdate(Camel):
    status: TaskStatus
    proof_note: str | None = None


# ---------------------------------------------------------------- system ---
class FeedStatus(Camel):
    id: str
    label: str
    state: Literal["live", "cached", "down"]
    last_updated: datetime | None
    detail: str


class LLMStatus(Camel):
    engine: Literal["gemini", "bedrock", "fallback"]
    note: str


class SystemStatus(Camel):
    mode: Literal["live", "fallback"]
    llm: LLMStatus
    feeds: list[FeedStatus]
    simulated_time: datetime | None = None
    scenario_id: str | None = None


# --------------------------------------------------------------- citizen ---
class CitizenSituation(Camel):
    ward_id: str
    at_risk: bool
    risk: WardRisk | None
    alert: Alert | None
    route: list[LngLat] | None
    shelter: Shelter | None


class AskRequest(Camel):
    question: str
    ward_id: str


class AskResponse(Camel):
    answer: str
    engine: Literal["gemini", "bedrock", "fallback"]


# ---------------------------------------------------------- reference data --
# Everything below is taxonomy the platform reads rather than hard-codes. A new
# city, hazard, resource kind or agency is rows in these tables.
class City(Camel):
    id: CityId
    name: str
    country: str = "IN"
    timezone: str = "Asia/Kolkata"
    languages: list[str] = Field(default_factory=lambda: ["English"])
    admin_unit_singular: str = "ward"
    admin_unit_plural: str = "wards"


class HazardTypeRef(Camel):
    id: HazardId
    display_name: str
    description: str = ""
    sort_order: int = 100
    #: Filled from the adapter registry, not the table: "live" or "registered",
    #: or absent when a row exists with no adapter behind it yet.
    maturity: str | None = None


class Capability(Camel):
    id: CapabilityId
    label: str
    description: str = ""


class ResourceKindRef(Camel):
    id: ResourceKindId
    display_name: str
    default_capacity: int = 1
    capabilities: list[CapabilityId] = Field(default_factory=list)


class LifelineKindRef(Camel):
    id: LifelineKindId
    display_name: str
    shelters_people: bool = False


class IncidentCategoryRef(Camel):
    id: CategoryId
    display_name: str
    hazard_id: HazardId | None = None
    #: Clustering parameters are data, so a city with wider streets or a hazard
    #: that spreads differently is a row change rather than a code change.
    dedup_radius_m: int = 250
    dedup_window_min: int = 45
    base_severity: Severity = 3
    life_safety: bool = False
    needs: dict[CapabilityId, int] = Field(default_factory=dict)
    #: Phrases that identify this category in free text, with a tuning
    #: multiplier each. Data rather than code, so a second hazard — or a second
    #: city with its own words for the same thing — is rows and not a release.
    #: Empty means this deployment has not seeded any and the parser falls back
    #: to its built-in dictionary.
    keywords: dict[str, float] = Field(default_factory=dict)


class Agency(Camel):
    id: AgencyId
    city_id: CityId = "pune"
    name: str
    short_name: str
    kind: Literal["government", "ngo", "volunteer", "private"] = "government"
    jurisdiction: str = ""
    active: bool = True
    capabilities: list[CapabilityId] = Field(default_factory=list)


class Taxonomy(Camel):
    """One payload the frontend fetches at boot instead of hard-coding unions."""

    city: City
    hazards: list[HazardTypeRef]
    capabilities: list[Capability]
    resource_kinds: list[ResourceKindRef]
    lifeline_kinds: list[LifelineKindRef]
    incident_categories: list[IncidentCategoryRef]
    agencies: list[Agency]


# ------------------------------------------------------------- event log ----
class Event(Camel):
    id: int
    city_id: CityId
    sim_run_id: str | None = None
    occurred_at: datetime
    recorded_at: datetime
    kind: str
    actor: str
    subject_type: str
    subject_id: str
    ward_id: str | None = None
    payload: dict = Field(default_factory=dict)
    causation_id: int | None = None


class SimRun(Camel):
    id: str
    city_id: CityId = "pune"
    name: str
    mode: Literal["sim", "replay"] = "sim"
    scenario_key: str | None = None
    clock_start: datetime
    clock_now: datetime
    speed: float = 60.0
    status: Literal["paused", "running", "finished"] = "paused"
    seed: int = 1


class RunRequest(Camel):
    """Trigger a hazard run. `hazard` is any id in `hazard_types`."""

    hazard: HazardId = "flood"
    city_id: CityId = "pune"
    sim_run_id: str | None = None


class RunResult(Camel):
    run_id: str
    agent_run_id: str
    hazard: HazardId
    engine: str
    mode: str
    wards_scored: int
    decisions: int
    auto_issued: int
    awaiting_approval: int
    assignments: int
    uncovered: int
    alerts: int
    field_tasks: int
    events: int
    duration_ms: int


class WardLocation(Camel):
    """Where a real GPS fix lands.

    `inside` is the field that matters. False means the coordinate is outside
    every ward this deployment covers, and `ward` is then the nearest one rather
    than the containing one. A client that ignores the distinction will show a
    resident somebody else's flood risk.
    """

    inside: bool
    ward: Ward | None = None
    distance_km: float = 0.0
    city_id: CityId = "pune"
    #: Plain sentence the citizen portal can show without composing one itself.
    note: str = ""
