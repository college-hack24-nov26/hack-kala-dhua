# Indradhanu OS — status and what is left

Last updated: 11 Sep 2026, after the intake and re-allocation build.

---

## PS20 requirement checklist

The ten bullets from the problem statement, against what the code actually does.
"Built" means the code path exists and writes real rows; it does **not** mean it
has been exercised against the live database end to end, which is tracked
separately below.

| # | PS20 requirement | Status | Where |
|---|---|---|---|
| 1 | Incident / zone reporting interface | **Built** | `POST /reports`, `app/incidents/intake.py`. Five channels (app, field, agency, sensor, sim) through one door |
| 2 | Resource inventory management | **Built** | `resources` + `resource_kinds` + capability mapping, 23 units seeded |
| 3 | Needs-assessment agent | **Built, deterministic** | `incident_needs` written per incident from `incident_category_needs`. LLM does not touch it |
| 4 | Allocation / optimisation agent | **Built** | CP-SAT in `app/solver/allocation.py`, capability matching, greedy fallback |
| 5 | Inter-agency coordination workflow | **Built** | `agency_requests`, request → acknowledge / decline / fulfil, every transition an event |
| 6 | Dynamic re-allocation on new reports | **Built** | `app/agents/replan.py`, switching cost, returns a diff not a plan |
| 7 | Priority / severity scoring | **Built, deterministic** | Category base severity, lifted by independent corroboration, capped at 5 |
| 8 | Duplicate-effort detection | **Built, both kinds** | `clustering.py` (duplicate reports) and `duplicates.py` (duplicate deployment across agencies) |
| 9 | Coordination dashboard | **Backend ready, UI on mock** | Every endpoint exists; screens still read `src/api/mock/` |
| 10 | Activity / audit log | **Built** | `events`, append-only enforced by trigger, with causal chains |

**Backend: 10 of 10 have a code path. Frontend: 1 of 10 is wired to it.** That
asymmetry is the single biggest thing left.

---

## What exists now

### Database

| Migration | What it did |
|---|---|
| `001` | Taxonomy moved from Postgres enums to reference tables; capabilities model |
| `002` | Converted the enum columns to FKs, dropped the old types |
| `003` | Cities, agencies, sim runs, the append-only event log, RLS |
| `pune_demo_accounts` | Five real Supabase users with identities and promoted roles |
| `extend_coverage_to_alandi` | Alandi and Dighi wards, their lifelines, five resources, GiST indexes |
| `intake_trust_dedup_needs_handoff` | Report provenance and trust columns, `report_links`, `incident_needs`, `agency_requests`, `reporter_reliability` view |

### Backend modules

| Module | Does |
|---|---|
| `app/taxonomy.py` | Reference data cache. Nine queries in parallel at startup |
| `app/world/clock.py` | `WallClock` / `SimClock`. Nothing calls `datetime.now()` directly |
| `app/world/events.py` | Append, batch, audit read, causal chain, replay stream |
| `app/agents/orchestrator.py` | The hazard run: signal → score → actions → policy gate → allocation → tasks → alerts |
| `app/agents/replan.py` | Re-allocation with switching cost, returns a plan diff |
| `app/incidents/trust.py` | Six-component deterministic trust score, life-safety asymmetry |
| `app/incidents/clustering.py` | Report → incident matching. LLM only for the ambiguous band |
| `app/incidents/intake.py` | The pipeline: isolate → trust → cluster → incident → needs → events |
| `app/incidents/duplicates.py` | Duplicate deployment across agencies and overlapping incidents |
| `app/core/jwks.py` | Supabase JWT, asymmetric and legacy, per token |

### API

```
GET  /health                         GET  /api/v1/taxonomy
GET  /api/v1/auth/me                 GET  /api/v1/wards/locate?lng=&lat=
POST /api/v1/runs                    POST /api/v1/reports
GET  /api/v1/events                  GET  /api/v1/events/{id}/chain
GET  /api/v1/incidents               GET  /api/v1/incidents/{id}/reports
GET  /api/v1/duplicates              POST /api/v1/replan
POST /api/v1/simulate/reports        GET  /api/v1/agency-requests
POST /api/v1/agency-requests         POST /api/v1/agency-requests/{id}/{action}
```

---

## Verified, and how

Things that have actually been run, not just written.

| What | How it was checked | Result |
|---|---|---|
| Orchestrator SQL | Every INSERT executed against the live schema in a rolled-back transaction | All valid |
| Event log immutability | Attempted UPDATE and DELETE on `events` | Both rejected by trigger |
| Capability matching | Boat vs fire engine at identical ETA for water rescue | Boat wins on fit |
| Capability substitution | Same, with the boat removed | Fire engine used at 0.6 effectiveness |
| Switching cost | Pump 85% of the way to a job vs 5% | Left alone at 85%, redirected at 5% |
| Fallback parity | Greedy vs CP-SAT switching penalty | Now share one `_switch_multiplier` |
| Category veto | Fallen tree vs flooded road, 60 m apart, 2 min apart | 0.00, opens a separate incident |
| Deduplication bands | Four synthetic reports at varying distance and wording | Link / adjudicate / new, as intended |
| Trust routing | Five provenances of the same claim | Field 0.82 confirmed, burst 0.25 quarantined |
| Life-safety asymmetry | Identical 0.48 score, two categories | Drain held, stranded person confirmed |
| Ward location | Alandi GPS, a city ward, and Mumbai | Inside, inside, 112 km outside |

### Two bugs caught in review, both fixed

1. **Greedy fallback ignored travel progress.** CP-SAT scaled the switching
   penalty by how far a unit had travelled; greedy applied it flat. A degraded
   run would have behaved *differently*, not merely worse, which defeats the
   purpose of a fallback. Both now share one function.

2. **Category veto did not fire between grouped categories.** A fallen tree and
   a flooded road both belong to hazard `flood`, so the same-hazard fallback
   scored them 0.67 and sent them to the model to be asked about. Two categories
   that are each in a group, and in different groups, is a positive statement
   that they differ. Now returns 0.00.

3. **Anomaly penalty was too weak.** Nine reports from one device with duplicate
   text still reached "needs corroboration" rather than being held. The penalty
   was a fixed subtraction capped at 0.2; it is now a discount that scales with
   how strong the rest of the evidence looked. That burst now scores 0.25 and is
   quarantined.

---

## Not verified

The build container cannot reach pypi (403) or Postgres over TCP, so nothing
below has run against the live database:

- The hazard run itself, end to end
- CP-SAT specifically. Only the greedy fallback was exercisable here, and it
  shares the matching and switching logic
- Open-Meteo and OSRM calls
- `events.append_many`, written but not used by any caller yet
- The intake pipeline against real PostGIS clustering. The scoring functions
  were tested in isolation with the real category parameters

All of it needs `uvicorn` locally. Expect breakage on first contact.

---

## What is left, in order

### Now — close Phase 0

- [ ] Add `DEV_AUTH_ROLE=ward_officer` to `backend/.env`, or sign in as
      `officer@pune.indradhanu.local`. Remote tools cannot write `.env` files
- [ ] `POST /api/v1/runs` with `{"hazard":"flood","cityId":"pune"}`. Expect
      `wardsScored: 16`
- [ ] Confirm every operational table has rows (query in `PHASE0.md`)
- [ ] `POST /api/v1/simulate/reports` with `{"count":12,"duplicateRatio":0.5,"adversarialRatio":0.2}`
- [ ] Walk one `assignment.created` event back through `/events/{id}/chain`

### Next — the frontend is the bottleneck

Every PS20 requirement has a working endpoint and nine of ten screens still read
the mock. This is now the largest gap between what the system does and what a
judge can see.

- [ ] `RiskBoard` → real `/wards`, `/risk`, live ward risk for 16 wards
- [ ] `IncidentQueue` → real `/incidents`, with the report count and a
      "3 reports merged" badge per incident
- [ ] Incident detail → `/incidents/{id}/reports`, showing the four rows, four
      link scores and four rationales behind one incident. **This is the screen
      that makes deduplication believable**
- [ ] `AllocationPlanner` → `/replan` diff view: kept, reassigned with reason,
      newly assigned, released, uncovered
- [ ] `DecisionGate` → real `/decisions`, with the auto-issued vs awaiting-officer
      counter and the clause that blocked each one
- [ ] `AgentTrace` → real `/events` and `/events/{id}/chain`
- [ ] Duplicate-effort panel → `/duplicates`
- [ ] Agency handoff → `/agency-requests`, two-click acknowledge
- [ ] Citizen portal → `useMyWard` for real GPS, real `/situation`, real
      `POST /reports`
- [ ] Delete `src/scenario/steps.ts` and `src/api/mock/` once nothing imports them

### Then — the simulation engine

The schema is ready (`sim_runs`, `sim_run_id` on every operational table) and
the clock exists. What is missing is the tick loop.

- [ ] `POST /sim/runs` to create and open a run, `/tick`, `/pause`, `/speed`
- [ ] Scenario generator: rainfall series, report arrivals as a Poisson process
      weighted by ward risk and population, road closures, unit breakdowns
- [ ] Reports typed into the citizen portal during a running sim, stamped with
      sim time. **The demo moment**: an officer files a report and watches a
      boat re-task ten seconds later
- [ ] Replay mode, faithful and counterfactual
- [ ] Metrics harness: the same event stream through nearest-available,
      one-shot optimisation, and the reactive system. Median and p90 report to
      assignment, unmet demand, duplicate deployments avoided, travel km,
      reassignment churn

### Then — resilience

- [ ] Offline-first field PWA: cached assignments, local event log, sync

### Deliberately not building

Twenty agents. A generic chatbot. Blockchain. Multiple LLMs for a slide. Fully
autonomous evacuation. Kubernetes as decoration. Adapters for every hazard
beyond flood and heat.

**Mesh networking, removed rather than deferred.** The intake used to accept
`source: "mesh"` with a `mesh_hops` count and score it down per relay hop. It
was never wired to a transport, no report ever arrived that way — 420 reports,
`mesh_hops` null on every one — and a scoring rule for a channel that does not
exist is not groundwork, it is a claim. The code and the column are gone. If an
offline transport is ever built, it arrives as a source with its own credibility
weight, which is a one-line change to `SOURCE_CREDIBILITY`.

---

## Known gaps in what was just built

Honest list, so none of it is discovered by a judge first.

- `POST /replan` is synchronous and unbounded. Fine at this scale, needs a
  debounce and a queue before it is event-triggered rather than called
- `events.append_many` uses an `unnest` form that has never run
- `_agency_for` maps an officer to an agency by string match on
  `profiles.operator`. It should be a foreign key to `agencies`
- The simulator's `adversarial_ratio` produces one recognisable pattern. A real
  red-team generator would vary the shape
- `reporter_reliability` is a view over `verification_status`, which is set at
  intake and never revised when an officer confirms or rejects an incident
  afterwards. The feedback loop is not closed yet
- Incident resolution is not implemented. Nothing sets `status = 'resolved'`, so
  incidents accumulate and `duplicates.detect` will get noisier over a long run
- Ward boundaries, Alandi's included, are ten-point approximations
