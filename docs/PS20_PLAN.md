# Indradhanu OS — PS20 Build Plan

**Status:** locked, ready to build
**Repo:** `D:\GITHUB\DisruptionOps` (Indradhanu backend + frontend copied in)
**DB:** existing Supabase project `Indradhanu` (`wfkzdevcfancytlvezxx`) — we build on it, no new project
**Name:** Indradhanu OS. No rename. `INDRADHANU_ENV`, package paths, API title all stay.

---

## 0. What actually exists today (verified, not assumed)

Read from the repo and the live database on 11 Sep 2026.

### Real and good — keep all of it

| Layer | State |
|---|---|
| `app/core/` | config (typed pydantic settings), logging, errors, middleware, security w/ `StaffPrincipal`/`CurrentPrincipal` — solid |
| `app/db/` | asyncpg pool + explicit SQL read models, PostGIS handled correctly (`ST_AsGeoJSON`, real geography distances) |
| `app/hazards/` | adapter contract + flood, heat, air, fire, seismic registered |
| `app/ingest/` | Open-Meteo client with cache + fallback |
| `app/solver/allocation.py` | CP-SAT assignment, population-weighted arrival time, greedy fallback, honest `engine` reporting |
| `app/solver/routing.py` | OSRM travel matrix |
| `app/agents/policy.py` | policy gate: clause lookup by `action_key`, delegation check, confidence floor, plain-language reason |
| `app/api/v1/` | citizen / geography / operations / risk / system |
| Frontend | React + Vite + shadcn, 3 personas (admin / citizen / field), map, 8 admin screens already built |

### Database — reference data seeded, operations empty

| Table | Rows |
|---|---|
| `wards` | 14 |
| `resources` | 18 |
| `lifelines` | 20 |
| `policy_clauses` | 7 |
| `hazard_runs`, `ward_risks`, `incidents`, `citizen_reports`, `allocation_plans`, `assignments`, `decisions`, `agent_runs`, `agent_steps`, `alerts`, `field_tasks` | **0** |

### The three real gaps

1. **Nothing has ever run end to end.** Every operational table is empty. The schema is right; the pipeline that fills it does not exist.
2. **There is no orchestrator.** `app/agents/` is two files — `llm.py` (Gemini/Bedrock + deterministic fallback) and `policy.py`. No agent loop, no LangGraph, nothing writes `agent_runs`/`agent_steps`.
3. **The "simulation" is a frontend script.** `src/scenario/steps.ts` is 24KB of hardcoded narrative driving `src/api/mock/world.ts`. It cannot react to a real report because no real report reaches it.

Also missing, and required by PS20: report→incident deduplication (`citizen_reports.incident_id` is nullable and never set), any event/audit log, agencies as first-class entities, needs assessment, and dynamic reallocation (`allocate()` is stateless — it has no idea what is already assigned).

**Conclusion: do not rewrite the backend. The separation of concerns is already what we want. The work is filling it in and adding one new spine.**

---

## 1. The single structural idea: one event log, three clocks

This is the decision everything else hangs off.

### 1.1 Append-only `events` table

Every state change in the system is written as an event before (or as) it mutates a table. One table satisfies four separate PS20 requirements at once:

- **Activity / audit log** — it *is* the audit log
- **Dynamic reallocation** — the reactive loop is triggered by events, not polling
- **Replay** — replay is the event log fed back in
- **Simulation** — simulation is an event log written by a generator instead of the world

```sql
create table events (
  id           bigserial primary key,
  sim_run_id   uuid null references sim_runs(id),  -- null = live
  occurred_at  timestamptz not null,               -- WORLD time (sim clock)
  recorded_at  timestamptz not null default now(), -- WALL time
  kind         event_kind not null,
  actor        text not null,        -- 'citizen:uuid' | 'agent:triage' | 'officer:name' | 'sim' | 'feed:open-meteo'
  subject_type text not null,        -- 'incident' | 'resource' | 'ward' | 'road' | 'decision'
  subject_id   text not null,
  payload      jsonb not null default '{}',
  causation_id bigint null references events(id)  -- what event caused this one
);
create index on events (sim_run_id, id);
create index on events (subject_type, subject_id, id);
```

`event_kind` enum, initial set:
`report_received`, `report_linked`, `report_rejected`, `incident_opened`, `incident_severity_changed`, `incident_resolved`, `risk_updated`, `road_blocked`, `road_cleared`, `resource_status_changed`, `resource_unavailable`, `plan_generated`, `assignment_created`, `assignment_changed`, `assignment_cancelled`, `decision_proposed`, `decision_gated`, `decision_acted`, `alert_issued`, `task_updated`, `needs_updated`, `feed_degraded`.

`causation_id` is what makes the agent trace real: "this reallocation happened *because* of report #204, which arrived *because* of the sim tick at T+22."

### 1.2 The clock

Nothing in the codebase calls `datetime.now(UTC)` directly any more. Everything takes a `Clock`:

```python
class Clock(Protocol):
    def now(self) -> datetime: ...

class WallClock:   # live
class SimClock:    # virtual time, speed multiplier, pause/step
```

Injected via FastAPI dependency, resolved from the active `sim_run_id` on the request (header `X-Sim-Run` or the user's active run).

### 1.3 Three sources, one engine

```
 EVENT SOURCE                    ENGINE                       SINK
 ───────────                     ──────                       ────
 live feeds + real reports  ─┐
 simulator (generator)      ─┼──▶  reactive loop  ──▶  incidents · plans · decisions · alerts
 replay (recorded log)      ─┘      (identical)          + new events appended
```

The engine does not know which source it is reading. That is the whole point, and it is what makes the answer to *"does your simulation actually work?"* be **"it is the production code path."**

### 1.4 Scoping

Add `sim_run_id uuid null` to `citizen_reports`, `incidents`, `allocation_plans`, `assignments`, `decisions`, `alerts`, `field_tasks`, `agent_runs`, `events`. `null` = live. Every read model filters by the active scope. Sim data never pollutes live data, and a sim run can be thrown away with one `delete`.

---

## 2. The simulation engine (answering: "it needs to adapt to other reports flowing inside it")

The current scenario bar is a slideshow. Replace it with an engine that has no script.

### 2.1 `sim_runs`

```sql
create table sim_runs (
  id            uuid primary key default gen_random_uuid(),
  name          text not null,
  mode          text not null check (mode in ('sim','replay')),
  scenario_key  text null,          -- 'pune_flood_2019' etc.
  clock_start   timestamptz not null,
  clock_now     timestamptz not null,
  speed         numeric not null default 60,  -- sim-seconds per wall-second
  status        text not null default 'paused',  -- paused|running|finished
  seed          bigint not null,
  created_at    timestamptz default now()
);
```

### 2.2 How a tick works

```
tick(sim_run, dt):
  1. advance clock_now by dt * speed
  2. GENERATOR emits any scheduled world events due in (t, t+dt]:
       - rainfall/gauge series advancing → risk_updated
       - synthetic citizen reports (Poisson, intensity ∝ ward risk × population)
       - road closures, resource breakdowns, shelter filling
       - (optionally) adversarial injections — see §5
  3. append them to `events` with sim_run_id + occurred_at = clock_now
  4. REACTIVE LOOP wakes on the new events
  5. whatever it decides is appended as more events, causation_id set
```

### 2.3 **The part that matters: human reports mid-run**

A real report submitted from the citizen portal while a sim is running goes through **`POST /api/v1/reports` — the exact same endpoint**, tagged with the active `sim_run_id`, timestamped with `clock_now` instead of wall time. It becomes a `report_received` event indistinguishable from a generated one.

Consequences, all of which fall out for free:

- It is deduplicated against generated reports by the same clustering code.
- If it corroborates an existing cluster → incident `confidence` and `report_count` rise → severity may rise → the reactive loop fires → **the allocation actually changes on screen.**
- If it opens a *new* incident in a ward with no free units → it shows up in `uncovered` with a reason.
- If it is a fake/contradictory report, the trust layer catches it and it does **not** move resources.

That is the demo: an officer typing a report into the phone view and watching a boat get re-tasked ten seconds later, with the causation chain visible in the trace. No script can fake that, and no script is needed.

### 2.4 Replay

Same engine. `mode='replay'`, event source = a previously recorded `events` stream (a past sim run, or a real incident's log). Supports:
- **Faithful replay** — re-emit exactly, show what the system knew at each point
- **Counterfactual replay** — re-emit the *world* events (reports, closures, breakdowns) but let the engine re-decide. Compare against what actually happened.

Counterfactual replay is where the evaluation numbers come from (§6).

---

## 3. Incremental allocation — the fix `allocation.py` needs

Current `allocate()` takes demands + units and solves from scratch. If we call it on every new report, the whole city reshuffles every few seconds. That looks unserious and is operationally wrong — a boat halfway to a call does not turn around for free.

Change the signature to take **current commitments** and a **switching cost**:

```python
def reallocate(
    demands, units, matrix,
    current: Mapping[str, str],      # unit_id -> demand_id already committed
    progress: Mapping[str, float],   # unit_id -> fraction of the way there
    *, switch_penalty_scale: float = 1.0,
) -> AllocationResult
```

In the CP-SAT model, for every unit already committed to demand *d*, add a cost term on **not** keeping it, scaled by how far along it is and by the severity it is currently serving. Result: units only get pulled when the new demand is meaningfully more important than the one they are on — and the plan diff explains it.

Output becomes a **diff**, not a full plan: `{kept, newly_assigned, reassigned (with reason), released, uncovered}`. The console renders the diff. `assignment_changed` events carry the reason string. This is the visible face of "dynamic reallocation" and it is three days of work, not three weeks.

Also: the greedy fallback needs the same treatment so degradation stays honest.

---

## 4. Incident formation: dedup and trust

Both deterministic. The LLM never invents a score.

### 4.1 Report → incident clustering

Deterministic candidate generation, cheap and explainable:

```
candidates for report R = existing incidents where
    ST_DWithin(incident.location, R.location, radius(category))   -- PostGIS, already available
AND R.created_at - incident.updated_at < window(category)          -- 45 min flooded_road, 15 min person_stranded
AND category_compatible(incident.category, R.category)             -- small static matrix
```

Score each candidate:

```
link_score =  0.35 · spatial_proximity      (decaying with distance)
            + 0.25 · temporal_proximity
            + 0.20 · category_match
            + 0.20 · text_similarity        (pgvector — the extension is ALREADY enabled
                                             and in use on policy_clauses.embedding)
```

- `> 0.75` → link automatically, `report_linked` event, bump `report_count`, recompute `confidence`
- `0.45 – 0.75` → **one** LLM call to adjudicate the single best pair ("are these the same event? yes/no + one sentence"), result stored with the link
- `< 0.45` → open a new incident, `incident_opened`

Store the graph so it is inspectable and reversible:

```sql
create table report_links (
  report_id    uuid references citizen_reports(id),
  incident_id  uuid references incidents(id),
  link_score   numeric not null,
  decided_by   text not null,      -- 'auto' | 'llm' | 'officer:name'
  rationale    text,
  created_at   timestamptz default now(),
  primary key (report_id, incident_id)
);
```

**Duplicate-effort detection** — the PS20 requirement — is then two things, both now trivially available:
1. *Duplicate reports* → the clustering above
2. *Duplicate deployment* → two assignments from different agencies inside the same incident cluster (or within N metres and a time window). Detected by a query over `assignments` joined through the incident graph; surfaced as a warning card in the console with a "this is deliberate / merge these" action.

Point 2 is the one judges will remember, because it is the actual failure mode in real disasters.

### 4.2 Trust score

Per report, deterministic, all six components computable from data we already have or can cheaply add:

| Component | Source |
|---|---|
| `source_credibility` | authenticated citizen / verified volunteer / field operator / agency / anonymous / mesh-origin — a static base per channel |
| `reporter_history` | Wilson score over that reporter's past reports confirmed vs. rejected |
| `location_plausibility` | is the report inside a ward whose current hazard risk is non-trivial; is it inside a flood-plausible elevation band |
| `corroboration` | independent reports in the cluster, discounted for same-reporter and same-device |
| `evidence` | photo present; later, vision-model agreement with the claimed category |
| `anomaly` (negative) | burst rate from one source, near-identical text across reports, impossible movement speed between a reporter's consecutive reports |

Routing by trust × impact:

| | Low impact | High impact |
|---|---|---|
| **High trust** | auto into pipeline | auto into pipeline |
| **Medium trust** | auto, flagged | request corroboration first |
| **Low trust** | queued, no resources | **human approval, always** |

Note this composes with the *existing* `policy.gate()` rather than replacing it. `gate()` already refuses to auto-issue below `CONFIDENCE_FLOOR = 0.70` and outside delegation. Trust feeds the confidence that gate consumes. Nothing about the authority model changes — which is good, because it is the strongest thing in the codebase.

### 4.3 Prompt-injection isolation

Untrusted report text never reaches an agent as instructions. Pipeline:

```
raw text ──▶ structured extraction (typed Pydantic, no free-form passthrough)
         ──▶ injection detector (pattern + classifier)
         ──▶ Evidence object: {category, location, claims[], quoted_text}
         ──▶ agent sees Evidence, and a system prompt that says quoted_text is DATA
```

Detected injection = an `anomaly` hit on the trust score and a flagged report, not a crash. Cheap to build, very strong to demo.

---

## 5. Agents — four, not eight

The agent count is a liability, not an asset. `agent_steps.agent` is already a Postgres enum (`hazard_analyst`, `impact_exposure`, `allocation_planner`, `guidance_agent`, `policy_retriever`), so extending it is `ALTER TYPE ... ADD VALUE`.

**Four LLM agents. Everything numeric stays deterministic.**

| Agent | Question | LLM does | Deterministic engine does |
|---|---|---|---|
| **Triage** | What is happening, and can we trust it? | adjudicates ambiguous report links; writes the human-readable incident title and summary | clustering, trust score, severity |
| **Needs** | What does this zone need? | maps incident type + exposure to a resource basket, explains it | population/exposure arithmetic, capacity lookup |
| **Coordination** | Who should act, and what's the strategy? | picks the agency, proposes the strategy, drafts the rationale | CP-SAT allocation, OSRM routing, policy gate |
| **Communication** | Who needs to know what? | drafts citizen alert / field instruction / agency request, English + Hindi + Marathi | channel selection, reach calculation, template constraints |

Severity, priority, risk, allocation, ETA, trust — **never** from the model. The README already states this principle; we are extending it, not inventing it.

Every agent step writes to `agent_steps` with `thought`, `tool`, `tool_input`, `tool_output`, `cited_clause`, `status`, so the existing `AgentTrace.tsx` screen becomes real instead of mocked.

**Cut for now:** vision model, voice, RL dispatch, cross-city federation, Sybil graph detection. Each is a slide, none is a system.

---

## 6. Evaluation — the numbers that win the room

Counterfactual replay (§2.4) gives us a defensible comparison. Run the same event stream through three policies:

1. **Baseline** — nearest-available, first-come-first-served, no dedup (what a radio-and-whiteboard room does)
2. **Static optimisation** — CP-SAT once at T0, no reallocation
3. **Indradhanu OS** — reactive, deduplicated, trust-filtered

Metrics, all computable from `events`:

- median and p90 time from `report_received` → `assignment_created`
- time from `incident_opened` → `incident_resolved`
- unmet demand (count and population-weighted)
- **duplicate deployments avoided**
- **resource-minutes saved by not dispatching to unverified reports**
- total travel km
- reassignment churn (proves the switching cost works)
- coverage at each severity band

Plus a **load test** — 100 / 1k / 10k / 100k synthetic reports through ingest → dedup → allocation, measuring ingest latency, dedup latency, solver time, API p95. Real numbers, not a Kubernetes diagram.

---

## 7. PS20 requirement → implementation map

| PS20 requirement | Where it lives | Status |
|---|---|---|
| Incident reporting | `POST /reports`, citizen + field + agency channels | endpoint exists, needs channels + trust |
| Resource inventory | `resources` table, 18 rows seeded | ✅ exists, needs live status + agency FK |
| Needs assessment | Needs agent + `needs` table | new |
| Severity scoring | deterministic severity engine | new (deterministic) |
| Allocation | `solver/allocation.py` CP-SAT | ✅ exists, needs incremental mode |
| Routing | `solver/routing.py` OSRM | ✅ exists, needs road-closure awareness |
| Inter-agency coordination | `agencies` table + Coordination agent | new |
| **Dynamic reallocation** | reactive loop + `reallocate()` diff | new — core |
| **Duplicate-effort detection** | report clustering + duplicate-deployment query | new — flagship |
| Priority scoring | deterministic, LLM explains | new |
| Coordination dashboard | admin console, 8 screens built | ✅ exists on mock, needs real wiring |
| **Activity / audit log** | `events` table | new — free from §1 |

---

## 8. Phases

Sequenced by dependency. BitChat is last, as agreed.

### Phase 0 — make one thing real *(highest priority; unblocks everything)*
The database has never held an operational row. Before any new feature:
- Wire `app/agents/orchestrator.py`: a single flood run that calls hazard adapters → writes `hazard_runs` + `ward_risks` → proposes actions → `policy.authority_for` + `gate` → writes `decisions` → allocates → writes `allocation_plans` + `assignments` → writes `agent_runs` + `agent_steps`.
- `POST /api/v1/runs/flood` triggers it.
- Frontend: flip `RiskBoard`, `AllocationPlanner`, `DecisionGate`, `AgentTrace` from `api/mock` to the real client for this one hazard.
- **Exit criteria:** every currently-empty table has real rows, produced by the real code path, visible in the UI.

### Phase 1 — the spine
- `events` table + `event_kind` enum + append helper
- `Clock` abstraction, remove every direct `datetime.now(UTC)`
- `sim_run_id` scoping columns + migration
- Audit log screen reading `events` (PS20 requirement, done early and cheaply)

### Phase 2 — incident core
- Report channels + trust scoring + injection isolation
- PostGIS clustering + `report_links` + Triage agent adjudication
- Severity engine, `needs` table, Needs agent
- Duplicate-deployment detection query + console warning

### Phase 3 — reactive coordination
- Reactive loop: debounced, event-triggered replan
- `reallocate()` with switching cost, plan-diff output
- `agencies` table + Coordination agent
- Road-closure-aware routing (closure events invalidate matrix entries)

### Phase 4 — simulation + replay
- `sim_runs`, tick loop, scenario generators
- Citizen-portal reports flowing into a live sim (§2.3) — **the demo moment**
- Replay mode, faithful + counterfactual
- Metrics + comparison harness (§6), load test

### Phase 5 — communication + offline field
- Communication agent, EN/HI/MR alerts
- Offline-first field PWA: cached assignments, local event log, sync + conflict resolution

### Phase 6 — BitChat (last)
**Scope: envelope + gateway only.** No BLE radio work.
- Signed, compact offline envelope format for reports and task updates (CBOR or compact JSON + Ed25519 signature, device key, monotonic counter)
- Store-and-forward queue on the field device
- `POST /api/v1/gateway/envelopes` — accepts a batch of envelopes, verifies signatures, **enters them into the identical trust → clustering → policy → coordination pipeline** at a lower base trust (`source_credibility` = mesh-origin) and with `occurred_at` from the envelope, not arrival time
- Sync + conflict resolution: envelopes are events, events are append-only, so "conflict resolution" is mostly ordering by device counter and letting the trust layer handle contradictions — this is the payoff of §1
- Demo transport: local relay / LAN. **State plainly that BLE mesh is the intended transport and is not implemented.** Never claim emergency-certification.

### Deliberately not building
20 agents · generic chatbot · blockchain · multiple LLMs for a slide · vision model before the incident pipeline is solid · autonomous evacuation · real BLE mesh · Kubernetes as decoration · forecasting models for every hazard.

---

## 9. Multi-city — one column, zero machinery

Add `city_id text not null default 'pune'` to `wards`, `resources`, `lifelines`, `agencies`, `policy_clauses`. That is the whole multi-tenancy story for now. It costs one migration and makes the platform claim honest without building tenant infrastructure nobody will exercise. Do it in Phase 1 while the tables are still empty — it is free now and expensive later.

---

## 10. Positioning

> **Disasters change faster than organisations can coordinate. Indradhanu OS turns uncertain, contested, incomplete information into verified, optimised and accountable action — and keeps doing it as the situation changes underneath it.**

Short form: **Predict. Verify. Coordinate. Adapt.**

Resilience form: **When the information is unreliable and the infrastructure fails, the response still has to work.**

The claim that makes it defensible, and which the code already honours: *the model reasons and explains; it never produces a number. Risk comes from adapters, allocation from CP-SAT, authority from the policy corpus. Remove the model and every one of those still works.*
