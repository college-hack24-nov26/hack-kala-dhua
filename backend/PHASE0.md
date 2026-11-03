# Phase 0 — first real end-to-end run

The point of this phase is narrow and it is the only thing that matters right
now: **every operational table in the database has to hold rows that the real
code path produced.** Before this work, `hazard_runs`, `ward_risks`,
`incidents`, `allocation_plans`, `assignments`, `decisions`, `agent_runs`,
`agent_steps`, `alerts` and `field_tasks` were all empty, and the frontend was
running off `src/api/mock/`. The schema was right and the algorithms were right;
nothing joined them up.

## What changed

### Database (migrations 001 to 003, already applied)

| Change | Why |
|---|---|
| hazard types, incident categories, resource kinds, lifeline kinds moved from Postgres enums to reference tables | adding cyclone, or a water tanker, or a city that calls its wards zones, is now an INSERT rather than `ALTER TYPE` plus a Python enum edit plus a TypeScript union edit plus a redeploy |
| `capabilities` + `resource_kind_capabilities` | a demand asks for *water rescue*, not for *a boat*. Boats, rescue teams and fire engines can all answer it at different effectiveness. New equipment needs no solver change |
| `incident_categories.dedup_radius_m` / `dedup_window_min` | clustering parameters are per-category data, so tuning them for a new city is a row edit |
| `cities`, `city_id` on wards / lifelines / resources / policy clauses | multi-city is a scope column rather than tenant machinery |
| `agencies` + `agency_capabilities`, `resources.agency_id` | inter-agency coordination needs agencies to exist as entities, not as a free-text `operator` string |
| `sim_runs` + `sim_run_id` on every operational table | a simulation writes to the same tables as live and can be deleted whole. `null` means live |
| `events`, append-only, enforced by trigger | the PS20 activity log, the replan trigger, the replay tape and the causal chain, in one table |

Lifecycle enums (`incident_status`, `assignment_status`, `task_status`,
`decision_status`, `resource_status`, `app_role`) were deliberately left as
enums. Those are genuinely closed sets that the code branches on.

### Backend

| File | Status | What it is |
|---|---|---|
| `app/taxonomy.py` | new | reads the reference tables once at startup; `kind_can()` and `effectiveness()` are what the solver matches on |
| `app/world/clock.py` | new | `WallClock` and `SimClock`. Nothing calls `datetime.now()` directly any more |
| `app/world/events.py` | new | append, batch append, audit read, causal chain walk, replay stream |
| `app/agents/orchestrator.py` | new | **the run.** signal, score, impact, actions, policy gate, demands, travel matrix, allocation, assignments, field tasks, alerts, events for all of it |
| `app/api/v1/runs.py` | new | `POST /runs`, `GET /taxonomy`, `GET /events`, `GET /events/{id}/chain` |
| `app/solver/allocation.py` | changed | matches on capability instead of a kind string; incremental re-planning with a switching cost shared by the solver and the greedy fallback |
| `app/hazards/{base,flood,heat}.py` | changed | `resource_need` is keyed by capability, so an adapter never names a vehicle |
| `app/schemas/domain.py` | changed | portable id types; `HazardType` and `IncidentCategory` remain as named constants but constrain nothing |
| `app/db/repositories/queries.py` | changed | city-scoped, accepts plain hazard ids |
| `app/main.py`, `app/api/v1/router.py` | changed | load the taxonomy at startup, mount the new router |

## Running it (Windows / PowerShell)

### 1. Python 3.11 or newer

The current `.venv` is Python **3.10.11**, and this code cannot run on it.
`datetime.UTC` and `enum.StrEnum` are both 3.11 additions and are used across
ten modules, including ones from the original repo. The compiled `.pyc` files in
the tree are `cpython-311`, so 3.11 is what this was written against; the venv
drifted down.

Do not backport. Move the interpreter up:

```powershell
winget install Python.Python.3.12       # skip if you already have 3.11+
py -0p                                   # list installed interpreters
```

Then rebuild the environment:

```powershell
cd D:\GITHUB\DisruptionOps\backend
Remove-Item -Recurse -Force .venv
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -c "import sys; print(sys.version)"   # must say 3.11+ before continuing
pip install -r requirements-dev.txt
```

If `Activate.ps1` is blocked by execution policy:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

`app/__init__.py` now refuses to import on anything below 3.11 with a message
that says what to do, rather than failing forty frames deep inside an import.

### 2. A staff identity

`POST /runs` requires a staff principal, and `profiles` has zero rows, so there
is no staff user to authenticate as yet. Chicken and egg. Two ways out.

**For local development**, add this to `backend/.env`:

```
DEV_AUTH_ROLE=ward_officer
```

An unauthenticated request is then treated as a ward officer. It is gated twice:
the setting must be non-empty *and* `INDRADHANU_ENV` must be `development`.
`Settings` refuses to load at all if it is set in staging or production, because
a convenience that can be left switched on by accident is not a convenience, it
is an unauthenticated admin endpoint. Every request that uses it logs a warning.

**For a real user**, create one in the Supabase dashboard (Authentication →
Users), then promote it:

```sql
insert into profiles (id, role, full_name, ward_id)
select id, 'ward_officer', 'Ayush Pathak', (select id from wards order by number::int limit 1)
from auth.users where email = 'you@example.com'
on conflict (id) do update set role = excluded.role;
```

Sign in from the frontend and use that session's access token as the bearer.

### 3. Start it

```powershell
uvicorn app.main:app --reload --port 8000
```

Note `app.main:app`, not `app.main:main`. `app` is the FastAPI instance;
`main` does not exist and produces a confusing import error.

Startup should log `taxonomy_loaded` with the counts, then `startup`.

### 4. Fire the run

PowerShell aliases `curl` to `Invoke-WebRequest`, which does not accept `-X`,
`-H` or `-d`, and backslash line continuations are a bash thing. Use
`Invoke-RestMethod`:

```powershell
$body = @{ hazard = 'flood'; cityId = 'pune' } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/v1/runs `
  -ContentType 'application/json' -Body $body | ConvertTo-Json -Depth 5
```

With a real token instead of `DEV_AUTH_ROLE`:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/v1/runs `
  -Headers @{ Authorization = "Bearer $env:TOKEN" } `
  -ContentType 'application/json' -Body $body | ConvertTo-Json -Depth 5
```

If you would rather use real curl, `curl.exe` is the actual binary and takes the
bash flags: `curl.exe -X POST ...`.

Worth checking first, since it needs no auth and proves the taxonomy loaded:

```powershell
Invoke-RestMethod http://localhost:8000/api/v1/taxonomy | ConvertTo-Json -Depth 4
Invoke-RestMethod http://localhost:8000/health
```

The migrations are already applied to the hosted project, so there is nothing to
run against the database first. For a fresh database, run `migrations/001` to
`003` in order after the original schema.

## Exit criteria

Not "it returns 201". The run is only done when this query stops returning
zeros:

```sql
select 'hazard_runs' t, count(*) from hazard_runs
union all select 'ward_risks',       count(*) from ward_risks
union all select 'decisions',        count(*) from decisions
union all select 'allocation_plans', count(*) from allocation_plans
union all select 'assignments',      count(*) from assignments
union all select 'field_tasks',      count(*) from field_tasks
union all select 'alerts',           count(*) from alerts
union all select 'agent_runs',       count(*) from agent_runs
union all select 'agent_steps',      count(*) from agent_steps
union all select 'events',           count(*) from events;
```

Then check the part that is actually interesting:

```sql
-- Why did this assignment happen? Walk the recorded causal chain.
select id, kind, actor, payload
from events
where subject_type = 'assignment'
order by id desc limit 1;
-- take that id, then GET /api/v1/events/{id}/chain
```

If the chain runs assignment → plan → run, the event spine is doing its job and
Phase 1 can build the reactive loop on top of it.

## What is verified and what is not

Verified here:

- Every INSERT the orchestrator issues was executed against the live schema
  inside a transaction and rolled back cleanly. The SQL matches the tables.
- The append-only trigger rejects both UPDATE and DELETE on `events`.
- Capability matching: a boat beats a fire engine for water rescue at identical
  ETA; when the boat is gone the fire engine is used at 0.6 effectiveness; a
  demand nothing can serve produces an uncovered entry with a readable reason.
- The switching cost: a pump 85% of the way to a job is left alone and a second
  pump is sent instead, costing 3 ETA-minutes on paper and saving a wasted round
  trip. The same pump 5% of the way is redirected, because it is cheap to.
- All 48 backend modules compile.

Not verified here, because this container cannot reach pypi (403 from the egress
policy) or Postgres directly (TCP blocked):

- The run has not been executed against the live database. CP-SAT itself could
  not be exercised, only the greedy fallback, which shares the matching and
  switching logic.
- Open-Meteo and OSRM were not called.

Those need `uvicorn` on a machine with the dependencies installed, which is the
command above. Expect to fix one or two things on first run; that is what first
runs are for.

## Known gaps, deliberately left

- `events.append_many` is written but not yet used by the orchestrator. The
  `unnest` form has not been exercised against Postgres.
- `POST /runs` is synchronous. Fine at a couple of seconds; it moves behind the
  event loop when the reactive planner lands in Phase 3.
- `registry.load_adapters()` is still an explicit list. A hazard row with no
  adapter behind it is reported as `maturity: null` by `GET /taxonomy` rather
  than failing, which is the honest behaviour, but the list is the one place
  adding a hazard still touches code.
- The frontend still reads `src/api/mock/`. Flipping `RiskBoard`,
  `AllocationPlanner`, `DecisionGate` and `AgentTrace` to the real client is the
  last step of Phase 0 and needs the run above to have produced rows first.
