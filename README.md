# DisruptionOps

**An agentic coordinator for the first four hours of an urban disaster.**

Built for **PS20 — Agentic Disaster Relief & Emergency Resource Coordinator**,
Kurukshetra 2.0 Hackfest, MIT ACSC Alandi, by team **Kala Dhua** (KH131).

- **Live demo:** https://distro-ruddy.vercel.app/ — `/citizen` needs no login
- **API:** https://disruptionops.onrender.com/api/v1
- **Repository:** https://github.com/officialayush23/DisruptionOps

---

## The problem, as we read it

Every city already knows the flood is coming. IMD forecasts it, NDMA publishes
the guidelines, the ward officer has the plan on the desk. **The forecast is not
what fails.**

What fails is the next four hours. Five zones need help at once, twenty-three
units are free, and nobody can hold the whole picture — so it is coordinated on
phone calls. Two boats go to the same street while a third zone gets nothing. A
truck is sent down a road a crew reported blocked twenty minutes ago. Afterwards
nobody can reconstruct why any of it happened.

DisruptionOps is the coordinator for those four hours.

---

## What it does

```
report → understand → score the zone → check authority → solve → dispatch
                              ↑                                      │
                              └──────── and the moment anything ─────┘
                                        changes, solve again
```

1. **Ingest** from six channels through one door — citizen app, field crew,
   partner agency API, store-and-forward when there is no signal, sensor feeds,
   and the simulator. They differ in exactly one field, `source`, which feeds the
   trust score; nothing downstream can tell them apart. That property is what
   makes the simulation honest: a report typed by a human during a running
   simulation goes through the same function and can change the outcome.
2. **Understand** — three-tier classification into the taxonomy, a six-component
   trust score, and clustering so ten calls about one street become one incident.
3. **Model the zone** — required capability × quantity per incident category,
   summed per ward, against what is actually free.
4. **Check authority** — an exact-match delegation clause decides whether the
   action issues itself or waits for a named officer.
5. **Allocate** — OR-Tools CP-SAT over real travel times, with roads crews have
   reported blocked removed from the graph.
6. **Act** — three interfaces reading one shared state, plus inter-agency
   request / accept / decline recorded on both sides.

Everything writes to an append-only event log carrying the id of the event that
caused it.

### New on 26 Sep 2026

- **Incident Commander**: an agent woken by severe incidents, camera alerts and
  blocked crews. It chooses its own read and simulate calls (at most 6), then
  proposes one action to the policy gate or says why nothing is needed. Shown
  step by step in the Copilot. [`docs/AGENTIC_ARCHITECTURE.md`](docs/AGENTIC_ARCHITECTURE.md)
- **Operations control**: cancel any unit's job and say what it should do
  instead (let the planner cover it, redirect, stage, hold, return to base),
  from the dispatch screen or by asking the Copilot. The planner obeys these
  standing orders on every solve. [`docs/OPERATIONS_AND_MEMORY.md`](docs/OPERATIONS_AND_MEMORY.md)
- **Copilot memory in Supabase**: conversation (so "cancel it" works) and
  long-term standing orders, facts, lessons and episodes. Words, never numbers.
- **Stable re-routing**: seven causes of re-route flicker and job thrash fixed,
  Mapbox point exclusion and OSRM detours round closures. [`docs/CHANGELOG_2026-09-26.md`](docs/CHANGELOG_2026-09-26.md)
- **Two-way offline mesh**: when there is no signal, reports (with GPS) and
  camera detections travel phone to phone over bitchat, and alerts, dispatches
  and road closures come back the same way. [`docs/OFFLINE_MESH.md`](docs/OFFLINE_MESH.md)
- **Scenario library**: eight scripted cases that run the real pipeline and
  check the outcome (`POST /api/v1/demo/scenarios/{name}/run`).

---

## The two design rules everything else follows

### 1. No model ever produces a number

| Decision | Produced by |
|---|---|
| What a report is about | the taxonomy — a model may only *choose from* it |
| What a zone needs | `incident_category_needs`, a table |
| How much to trust a report | six-component arithmetic, shown in full |
| Which unit goes where | OR-Tools CP-SAT |
| Whether an action may issue | a cited delegation clause |
| Prose, summaries, explanations | an LLM |

Remove every model from this system and it still coordinates a flood — slightly
worse at reading Marathi free text, and blind to photographs. Remove the solver
and you have a chatbot.

### 2. Nothing issues because a model was confident

It issues because a **delegation clause** permits it. Matched on exact
`action_key`, never fuzzy similarity — pgvector is installed and deliberately not
used for this, because *"close enough to a clause that permits this"* is not a
sentence anybody should be able to say about requisitioning NDRF.

Inside the cited authority the action auto-issues and the clause is displayed.
Outside it, or below the confidence bar, it stops and asks.

---

## The mathematics

Every constant below is the one in the running code.

### Trust — six components, five weighted, one that discounts the rest

| Component | Weight | How |
|---|---|---|
| Source credibility | 0.22 | lookup: field 0.95 · agency 0.92 · sensor 0.88 · phone 0.70 · app 0.62 |
| Reporter history | 0.18 | Wilson lower bound, 95% |
| Location plausibility | 0.18 | `0.35 + 0.65 × ward_risk`, × 0.6 if the category does not match the active hazard |
| Independent corroboration | 0.22 | `1 − 1/(1 + 0.9n)`, saturating |
| Evidence | 0.10 | `clamp(0.65 + 0.30 × photo_agreement, 0.15, 0.95)` |
| Anomaly | — | a multiplicative discount, below |

**Reporter history — Wilson score interval, lower bound at 95%:**

```
          c + 1.9208            √( c·w / n + 0.9604 )
r  =  ───────────────────  −  1.96 · ───────────────────
           n + 3.8416                     n + 3.8416
```

Not `c/n`. One correct report out of one is not a perfect reporter, and a raw
ratio says 1.0 — which is the hole a fabricated account walks through. Wilson
starts everybody near 0.5 and moves only as evidence accumulates. Reports nobody
has ruled on drop out of the denominator, so nobody is punished for being recent.

**Corroboration saturates on purpose.** n=1 → 0.47, n=2 → 0.64, n=3 → 0.73,
n=10 → 0.90. Counting linearly is exactly what lets a burst look like truth. `n`
counts distinct devices and accounts, not reports: reports from one source do not
corroborate each other.

**Anomaly is a discount, not a subtraction:**

```
anomaly = min(0.5,  0.12 × (reports_from_source_in_15min − 3))   if > 3
        + min(0.35, 0.15 × near_identical_texts)
        + 0.35                              if implied travel > 180 km/h

final   = raw × (1 − 0.70 × anomaly)
```

We tried subtracting a fixed amount. A nine-report burst from one device with
duplicated text still landed in "needs corroboration" — the most obvious
fabrication pattern there is, waved through. A discount scales with how strong
the rest of the evidence looked, which is the right shape: a confident-looking
report from a suspicious source should lose the most.

**Routing:**

```
≥ 0.72  auto_confirmed        may commit a unit on its own
≥ 0.35  needs_corroboration   visible, but will not spend a vehicle
< 0.35  quarantined           recorded, held for a human, never clusters
```

**One asymmetry, held deliberately.** For a life-safety category
(`person_stranded`, medical), anything above the quarantine floor is confirmed
rather than held. Sending a boat to nobody wastes a boat; not sending one drowns
somebody. The two mistakes are not comparable and the thresholds do not pretend
they are.

### Allocation — OR-Tools CP-SAT

```
demand weight = severity² × (1 + population_at_risk / 10 000)

minimise   Σ  weight × eta × 10 / capability_fit
         + Σ  10 000 × weight × (1 − served)
```

Severity squared so it dominates; population breaks ties *within* a severity
band. The 10 000 coverage penalty makes coverage lexicographically first —
leaving a demand uncovered costs more than any achievable arrival time.

Constraints: capability match · unit capacity · one unit per job ·
`MAX_ETA_MINUTES = 45` reachability · travel times over the current road graph.

**Switching cost** — what makes re-planning safe rather than thrashing:

```
multiplier = 1 + (1.6 − 1) × (0.25 + 0.75 × progress)
```

A committed crew costs 1.15× to turn around at the start of its journey and 1.6×
when nearly there. Falls back to a greedy heuristic honouring the same switching
cost if CP-SAT cannot solve in time, so a degraded solve is still coherent.

### Forecast — Gamma-Poisson conjugate recurrence, per ward per category

```
prior      Gamma(α₀, β₀),  β₀ = 12 h,  α₀ = city_rate × β₀
posterior  λ = (α₀ + k) / (β₀ + t)
expected   λ × horizon_hours
P(≥ 1)     1 − e^(−expected)
evidence   t / (β₀ + t)
```

Chosen for one property: **it starts as the city-wide prior and becomes the
ward's own record, with a number saying how far along it is.** `evidence` is the
honesty column — a forecast resting on the prior says so, in words, in the
rationale, because an officer moving a pump at 2 a.m. must be told the difference
between *"this ward floods every monsoon"* and *"we have no idea, so we guessed
from the city average"*.

### Prepositioning — and how it learns

```
confidence = 0.30 + 0.40 × evidence + 0.30 × hit_rate
hit_rate   = (hits + 1) / (resolved + 2)        Laplace-smoothed
```

Laplace so a capability with no history returns exactly 0.5 — *no opinion* —
rather than looking certain on one data point. Every proposal is recorded and
settled later against whether a named unit was actually tasked in that ward
inside the horizon. A capability that keeps being proposed and never used drifts
down until a human must agree.

Three limits: it proposes and never moves; it never touches a committed unit;
below a 1.0-unit projected shortfall it says nothing.

---

## Models, and why each one

| Model | Used for | Why this one |
|---|---|---|
| **XLM-RoBERTa-large-XNLI** (`joeddav/xlm-roberta-large-xnli`) | Report classification, tier 2 | Zero-shot across 100 languages. We have no labelled disaster-report corpus for Pune and will not have one before deployment; a model that needs none is the only honest option. Classifies *into* the taxonomy — it cannot invent a category. |
| **Qwen2.5-VL** (self-hosted) | Photo assessment | Open weights, runs on a laptop GPU, answers a fixed JSON contract. May raise or lower trust; **may never set category, severity or dispatch.** |
| **Sarvam AI** `saarika:v2.5` / `saaras:v2.5` | Speech to text | Trained on code-mixed Indian speech. General multilingual recognisers drop exactly the English loan words that carry the location. |
| **Gemini** | Prose and explanation only | Never produces an operational number. |
| **OR-Tools CP-SAT** | Allocation | Constraint programming, not a heuristic: it proves optimality under the stated constraints, and the constraints are the thing an officer can argue with. |

Tier 1 of classification is a keyword match in English, Hindi and Marathi — it
handles most reports at zero cost and zero latency, and the model is only reached
when it is not confident.

---

## Architecture

```
INTAKE      citizen app · field crew · agency API · store-and-forward · sensors · simulator
                                    ↓  intake.receive()  — one door
UNDERSTAND  parser (3 tiers) → trust scorer (6 components) → duplicate clusterer
                                    ↑ vision service (may move trust, nothing else)
ZONE        needs assessment (capability × quantity) · severity & priority scoring
DECIDE      delegation matrix (exact action_key) → auto-issue with clause, or officer queue
ALLOCATE    travel-time matrix (Mapbox → OSRM → straight line) → CP-SAT → re-plan w/ switching cost
ACT         citizen PWA · command console + Copilot · field crew app · inter-agency handoff

SPINE       append-only events table, causation_id on every row, DELETE and UPDATE
            refused by database trigger
FOUNDATION  Supabase Postgres + PostGIS · RLS scoped by city · taxonomy as rows, not enums
```

Full drawing spec: [`docs/SYSTEM_ARCHITECTURE.md`](docs/SYSTEM_ARCHITECTURE.md).

---

## Stack

**Backend** — FastAPI · asyncpg · pydantic-settings · OR-Tools CP-SAT · structlog
**Frontend** — React 19 · Vite · TypeScript · shadcn/ui · Tailwind · Mapbox GL ·
PWA with service worker and IndexedDB outbox
**Data** — Supabase Postgres 17 + PostGIS, row-level security, append-only event
log enforced by triggers
**Hosting** — Render (API) · Vercel (web) · Supabase (database)

---

## Running it

```bash
# ---- database ----
# apply backend/migrations/*.sql in order against a Postgres with PostGIS

# ---- backend ----
cd backend
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                             # fill in the values below
uvicorn app.main:app --reload --port 8000

# ---- frontend ----
cd frontend/indradhanu
npm install
npm run dev
```

### Environment

| Variable | Required | Notes |
|---|---|---|
| `DATABASE_URL` | yes | Postgres connection string |
| `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` | yes | storage and auth admin only; queries go through asyncpg |
| `CORS_ORIGINS` | yes | comma-separated, exact scheme and host, no trailing slash |
| `MAPBOX_TOKEN` | no | routing; falls back to OSRM, then straight line |
| `HF_API_TOKEN` | no | tier-2 classification; keyword tier works without it |
| `SARVAM_API_KEY` | no | speech to text |
| `VLM_URL` | no | vision service — **the bare root is fine**, the client finds the path |
| `VLM_MODEL` | no | exact model id, e.g. `Qwen/Qwen2.5-VL-7B-Instruct` |
| `GEMINI_API_KEY` | no | prose, the Copilot router and the Incident Commander. Without it the Commander does not run; everything else does |
| `MESH_GATEWAY_KEY` | no | enables `/mesh/*` and `/ingest/sensor` for gateway phones, the bridge script and camera nodes. Unset = those endpoints answer 403 |
| `MESH_HMAC_KEY` | no | signs and verifies IDX1 mesh packets (camera node, bridge). Unsigned packets are accepted and scored lower |

Frontend: `VITE_API_URL` must include the `/api/v1` suffix.

Vision service setup: [`docs/vlm-laptop-setup.md`](docs/vlm-laptop-setup.md).

---

## Data sources — all open access or keyless

| Source | Used for | Link |
|---|---|---|
| **NDMA, Guidelines on Management of Urban Flooding (2010)** | Delegation clauses, response doctrine | https://ndma.gov.in |
| **Pune Municipal Corporation Disaster Management Plan** | Ward-level delegation of authority, e.g. PMC DMP 2023 cl. 6.1 | https://pmc.gov.in |
| **Open-Meteo** | Forecast, flood and air-quality APIs — keyless | https://open-meteo.com |
| **GloFAS v4** (via Open-Meteo) | River discharge / flood forcing | https://global-flood.emergency.copernicus.eu |
| **OpenStreetMap / OSRM** | Road network, routing fallback | https://project-osrm.org |
| **Mapbox Directions** | Primary routing and basemap | https://docs.mapbox.com |
| **Census of India / PMC ward data** | Ward population and exposure | https://censusindia.gov.in |
| **NASA FIRMS** | Fire detections, where a key is present | https://firms.modaps.eosdis.nasa.gov |

Nothing here depends on a licence we could lose.

## Methods and libraries cited

| Thing | Reference |
|---|---|
| Wilson score interval | Wilson, E. B. (1927), *Probable Inference, the Law of Succession, and Statistical Inference*, JASA 22(158) |
| Gamma-Poisson conjugate prior | Gelman et al., *Bayesian Data Analysis*, 3rd ed., ch. 2 |
| Laplace / additive smoothing | Manning, Raghavan & Schütze, *Introduction to Information Retrieval*, ch. 13 |
| CP-SAT constraint solver | Google OR-Tools — https://developers.google.com/optimization |
| XLM-RoBERTa | Conneau et al. (2020), *Unsupervised Cross-lingual Representation Learning at Scale* |
| Zero-shot classification via NLI | Yin, Hay & Roth (2019), *Benchmarking Zero-shot Text Classification* |
| Qwen2.5-VL | Qwen team, Alibaba — https://github.com/QwenLM/Qwen2.5-VL |
| PostGIS | https://postgis.net |

---

## Benchmarks

Against a nearest-first baseline — five seeds (7, 11, 23, 42, 101), one third of
the fleet, 385 demands over 180 sim minutes each, the same incident stream
replayed into every arm:

| Metric | Nearest-first | Indradhanu | |
|---|---|---|---|
| Demands left uncovered | 70 of 385 (18.2%) | 63 (16.4%) | **10% fewer** |
| Time to commit a unit, p90 | 47.3 min | 39.8 min | **−16%** |
| Time to commit a unit, median | 2.2 min | 2.3 min | no difference |
| Arrival p90 | 71.0 min | 107.5 min | **+51%** — what re-tasking costs |
| Committed units re-tasked | 0 | 163 | |

```
python scripts/benchmark_strategies.py --offline --fleet 0.3 --minutes 180 --seed 11
```

Three things worth saying out loud.

**The last two rows are the price.** The better plan is not free: we pay 51% on
the slowest arrivals to leave 10% fewer demands uncovered, and 163 crews were
turned around to do it. That trade is a product decision, it is measured, and
anyone can re-run the harness above.

**The gain is the re-planning, not the optimiser.** The middle arm — the same
CP-SAT model, solved once and never revisited — left 70 demands uncovered, which
is exactly what nearest-first left. Optimising a snapshot buys nothing here. Only
revisiting does.

**These are the `--offline` synthetic city, not Pune.** Arrivals are synthetic
and service times are fixed. It is a fair test of dispatch policy and it is not
evidence about any real city, which is why the conditions are printed next to the
numbers on the After-Action screen rather than left in a README.

---

## Repository layout

```
backend/
  app/
    agents/        orchestrator · forecast · prepositioning · gate · replan · policy · commander
    api/v1/        personas (citizen & field) · demo (console) · reports · config · copilot · runs
    copilot/       22 tools in three tiers — read, analyse, act · memory (Supabase)
    ops/           operations: cancel, do-instead, standing orders
    mesh/          IDX1 envelope · inbound/outbound mesh service
    core/          config · security · caching · rate limiting · errors · logging
    db/            asyncpg pool, transactions, query repository
    hazards/       per-hazard adapters — flood, heat, fire, air, seismic
    incidents/     intake · parsing · trust · clustering · duplicates · vision · speech
    solver/        CP-SAT allocation · routing
    world/         clock (wall and simulated) · append-only events
  migrations/      numbered SQL, applied in order (001–022)
  scripts/         mesh_bridge.py (phone↔API bridge and simulator) · benchmarks
  tests/           unit tests: python -m unittest discover -s tests
frontend/indradhanu/
  src/routes/      citizen · field · admin console · demo
  src/components/  map (Mapbox GL) · copilot · ui (shadcn)
  src/api/         HTTP client — also where success/failure toasts live
docs/              architecture spec · pitch brief · VLM setup · deck
```

---

## What is deliberately not here

- **LangChain / LangGraph.** The orchestrator is deterministic on purpose: a
  fixed order, one transaction, every step writing an event that names its cause.
  A graph runtime would add a scheduler we would then have to explain to an
  auditor. These packages were once declared in `requirements.txt` and imported
  nowhere; they have been removed, with the reason recorded in that file.
- **Vector similarity for delegation.** pgvector is installed. Authority matching
  uses exact `action_key` equality, for the reason given above.
- **A trained trust classifier.** No labelled ground truth exists, and an officer
  must be able to audit a decision that cost a vehicle. Six components and their
  reasons, in words, beats a number nobody can argue with.

## Known gaps

Kept honest and current in [`docs/`](docs/):

- Ward boundary drawing in the configuration UI still submits `boundary: null`.
- No automated tests on the intake path. Routing geometry, the mesh envelope,
  commitment mapping, Copilot cancel parsing and the Commander's guards have
  unit tests (`backend/tests`); the scenario library checks behaviour against a
  live database.
- The bitchat gateway (Kotlin) and the Incident Commander have not yet been run
  end to end; see the changelog for what was and was not verified.
- `/api/v1/health` is not implemented; Render health-checks another route.
- 19 of 420 reports have never clustered into an incident — expected behaviour,
  but the reason has not been reviewed case by case.

---

## Team

**Kala Dhua** · KH131 · MIT ACSC Alandi, Pune

## Licence

Source released for evaluation. All third-party data sources listed above remain
under their own licences.
