# 26 September 2026: agentic memory, operations control, routing stability, two-way mesh

A record of every change made in this pass: what was wrong, what was done,
where the change is, and how it was checked. Written so a teammate or a judge
can follow it without the conversation that produced it.

Companion documents:

- [`AGENTIC_ARCHITECTURE.md`](AGENTIC_ARCHITECTURE.md): every agent and tool, and
  why this counts as an agentic system
- [`OFFLINE_MESH.md`](OFFLINE_MESH.md): the online/offline design, the bitchat
  gateway, sending and receiving with location
- [`OPERATIONS_AND_MEMORY.md`](OPERATIONS_AND_MEMORY.md): cancelling operations,
  "do this instead", standing orders, Copilot memory, scenarios

---

## 0. Access and findings before changing anything

| Step | Result |
|---|---|
| Supabase connector | First connected to the wrong account (only saw `RED_VS_BLUE_MASTERCARD`). After it was reconnected it saw **Indradhanu** (`wfkzdevcfancytlvezxx`, ap-northeast-2). All three migrations below were applied through it and verified by query |
| Enum check | `assignment_status` had **no `cancelled` value**, but `copilot/execute.py` writes `status = 'cancelled'` whenever a commissioner moves a unit. It had never run (0 `reallocate_unit` decisions in the DB), so the first move in a demo would have been a 500. Fixed in migration 021 |
| Live data | An existing re-route event said "crosses **32** newly blocked point(s)", with one block on the map. That is the old exposure count (vertices near a block, not blocks), confirmed in the data |
| bitchat fork | Read end to end (`VlmApiService.kt`, `VlmMessageHandler.kt`). It could only **send** (`/send/text`, `/send/image`, `/send/analysis`); nothing exposed *received* mesh messages. `/send/text` returns `400 "Rate limited"` inside its 5 s window, rather than dropping silently as the ai-surveillance code assumed |
| Machine | The linked PC's shell has no network (pip/npm blocked), but the Windows `node_modules` were usable, so the frontend was type-checked there |

---

## 1. Routing and re-routing stability (seven bugs)

The complaint was "data and re-routing are not stable". Each item below is a
cause found in code, not a tuning change.

| # | Symptom | Cause | Fix | File |
|---|---|---|---|---|
| 1 | Unit jumps along the map after a re-route | `_reroute_if_blocked` replaced the route but kept the old `progress`, so the unit teleported that fraction along the new line | `progress = 0` on re-route (the new road starts where the unit is) | `agents/replan.py` |
| 2 | Same unit re-routed on every re-plan; route flickers | Exposure counted **vertices** near blocks (every 3rd vertex), so it changed with how the router drew the road, and any exposed road was swapped for another exposed road | Exposure is now the number of **distinct blocks** within 150 m of any **segment**; a re-route happens only if the new road is **strictly less exposed**; only the road **ahead** of the unit counts | `solver/routing.py` (`point_segment_km`, `_exposed_blocks`, `remaining_path`), `agents/replan.py` |
| 3 | Every unit sent to a flooded road counted as "routed through a block" | Open `flooded_road` incidents are themselves block points, so the destination was always "blocked", and `BLOCK_RADIUS_KM = 0.35` covered every parallel street | Blocks within 250 m of either end of a trip are ignored **for that trip** (`relevant_blocks`); radius reduced to 150 m | `solver/routing.py` |
| 4 | Router never actually avoided a closure in a dense grid | Only "least exposed of the router's 2–3 alternatives" was tried | Mapbox Directions is called with `exclude=point(lon lat),…` (up to 50, nearest the corridor first); OSRM, which cannot exclude, tries two **detour waypoints** 600 m either side of the first block it crosses; the route line records `avoided` and `avoidance` (exclude / detour / alternatives) | `solver/routing.py` (`route_line`) |
| 5 | Committed units bounced between jobs | `current` mapped every unit on an incident to that incident's **first** demand id: capability-blind (a pump "committed" to an ambulance demand) and shared (two units on one demand), so staying put was priced as a switch | `map_commitments()`: each unit gets its own demand of a capability it can serve, most-progressed first. Also the simulator used incident-level ids for the solver, so its "Now" column was a different plan from the live one; fixed with `Inputs.commit` | `agents/replan.py`, `copilot/simulate.py` |
| 6 | Units re-tasked seconds after being tasked; on-scene crews moved mid-job | Progress was `elapsed / ETA`, near 0 for a fresh assignment, so switching was nearly free; `on_site` units were in the solve | Progress uses the driven `progress` column; a **60 s dwell** floors it at 0.5; `on_site` units are **locked** (kept, their demand removed from the solve) | `agents/replan.py` |
| 7 | Units stuck "en route" for ever; staged units never arrived | Arrival required ≤120 m to the incident, but the router snaps destinations to a road that can be further away; incident-less assignments (staging) were joined out | Arrival also when `progress ≥ 0.999`; staged/return-to-base assignments complete at the end of their road and the unit becomes available there | `demo/runner.py` (`_move_units`) |

Also fixed:

- Re-tasked and released units left their **field task open**, so the crew app
  kept a job the plan had removed. `close_tasks()` now cancels it with the
  reason. Old assignments are marked `cancelled`, not `complete` (which counted
  them as work done). `duplicates.py` now excludes cancelled assignments.
- The demo's road block was dropped within ~170 m of the incident, inside the
  destination clearance, so no route could ever avoid it. It is now placed on
  the **road the unit is about to drive**, half-way to the job and ≥300 m from
  it (verified on live data: points 357–664 m from the incident).
- A commissioner's move (`reallocate_unit`) routed with **no** blocks, wrote no
  crew task, and was undone by the next re-plan. It now routes round blocks,
  writes the crew task, closes the old one, and pins the unit (section 2).
- Staging and return-to-base routes now appear on the map (`_ROUTES_SQL` left
  join).

Verification: `backend/tests/test_offline_core.py`, 26 tests, all passing
(geometry, exposure, detours, exclusion ordering, commitments, envelope,
cancel parsing, memory shaping). SQL for block placement, operations and ward
lookup was run against the live database.

---

## 2. Operations: see, cancel, "do this instead"

New module `app/ops/operations.py`, API `app/api/v1/ops.py`, migration 021.

- `GET /api/v1/ops`: every live operation (unit, job, status, % driven, minutes
  left, re-route count, who set it, **why**) and every standing order.
- `POST /api/v1/ops/preview`: re-solves the real plan on a copy with the unit
  withdrawn (or redirected), and lists **what it could usefully do instead**
  (open incidents with an unmet need this unit's kind can serve, by severity
  and distance).
- `POST /api/v1/ops/cancel` {resourceId, reason, instead}: goes through the
  policy gate as `cancel_assignment`. `instead` is one of `replan`, `redirect`,
  `stage`, `hold`, `return_to_base`.
- `POST /api/v1/ops/overrides/{id}/lift`: withdraw a standing order early.

Why a cancel is more than a status change: the next re-plan would send the
unit straight back. So a cancel writes a **forbid** override (this unit, not
that incident, 45 min), a redirect writes a **pin** (60 min), a hold writes a
**hold**. `replan.load_overrides()` reads them on every solve: pinned and
on-scene units are locked, held units are out of the pool, forbidden pairs are
priced out of reach. They expire on their own.

Migration 021 (applied): `task_status += cancelled`, `assignment_status +=
cancelled`, table `operator_overrides`, and `cancel_assignment`, `hold_unit`,
`pin_unit` added to clause **pol-2**, the clause that already governs moving a
municipal unit. No new authority was invented.

UI: **Who is on what** (`/admin/dispatch`) has a *Cancel…* button per unit (a
dialog with the five options, live preview table, reason), `pinned` and
`re-routed ×N` badges, and a **Standing orders** card with *Lift*.

---

## 3. Copilot: memory in Supabase, and it can cancel

Migration 020 (applied): `copilot_turns` (short-term, per browser session),
`agent_memory` (standing orders, facts, lessons, episodes, preferences; full
text search, importance, expiry, soft delete, `embedding vector(768)` column
ready but unused), and the `recall_memory()` ranking function. Verified with a
rolled-back insert and recall.

`app/copilot/memory.py`; wired into `agent.ask()`:

- The last 6 turns and the ids each answer was about are given to the router,
  so "cancel it", "apply the second one" and "same for Baner" resolve.
- Relevant memories (always standing orders) go to the router and the narrator,
  with the rule: **memory is words, never numbers**.
- Every answer returns `memory` (what it was reminded of); the Copilot shows it
  as "remembered · …" chips.

New intents, with the keyword router behind the model:

| Say | Does |
|---|---|
| "who is on what", "operations" | table of live operations, and the standing orders |
| "cancel Pump 3 and send it to the Baner flooding" | preview, then *Put to the policy gate* option cards: what you asked for, plus the top 2 useful alternatives, plus "hold 30 min" |
| "stop Ambulance 4, hold it for 20 minutes because the crew is exhausted" | same, with `hold`, 20 min, and the reason |
| "remember that the Aundh underpass floods first" | long-term fact |
| "remember: keep Boat 2 in Kothrud until 6am" | standing order, plus a gated action to make it binding |
| "what do you remember" / "forget …" | list / soft-withdraw |

New tools in the catalogue: `get_operations` (read), `preview_cancel`
(analyse), `recall_memory` (read). Executors: `cancel_assignment`, `hold_unit`,
`pin_unit`. API: `GET/POST /copilot/memory`, `DELETE /copilot/memory/{id}`,
`sessionId` on `/copilot/ask`.

---

## 3b. The Incident Commander (the autonomous agent)

`app/agents/commander.py`. This is what makes the system agentic in the strict
sense: it is woken by the world, not by a person, and chooses its own sequence
of tool calls from what each one returns.

- **Wakes on:** a severity 4+ incident (`runner._gate_for_incident`), a road
  block reported by a crew (`runner._report_road_block`), a confident camera
  detection arriving over HTTPS or the mesh (`mesh.service._sensor`), or an
  officer pressing *Wake it*. Debounced per ward/unit/node (60 s); one episode
  at a time.
- **Loop:** JSON action protocol `{"thought", "tool", "args"}` (works with
  Gemini and Bedrock alike), at most 6 steps and 45 s. It may call only the
  read and analyse tools. It must look before proposing, and simulate before
  moving a unit. It ends in `propose_action` (policy gate → executor) or
  `no_action` with a reason.
- **Memory:** standing orders and lessons are recalled into its first prompt;
  each episode is saved as an `episode` memory.
- **Trace:** `agent.episode_started`, `agent.step`, `agent.episode_finished`
  events with `caused_by`, so the Agent log shows the chain.
- **No model, no episode.** The deterministic orchestrator and re-planner have
  already acted; the Commander is the layer that looks further.
- **UI:** *Incident Commander* panel in the Copilot's left column: each
  episode's trigger, thoughts, tool calls, results, and outcome.
- **API:** `GET /copilot/commander`, `POST /copilot/commander/wake`.

---

## 4. Two-way offline mesh

Migration 022 (applied): `mesh_messages` (every packet heard, unique per
packet id, so three gateways = one report), `mesh_outbox`, `mesh_nodes`,
`mesh_state`.

Backend:

- `app/mesh/envelope.py`: the IDX1 packet, `human text IDX1|T|{json}|hmac16`.
  Types R report, S sensor, F field status, H heartbeat, K ack (inbound);
  A alert, D dispatch, C cancel, B road block (outbound).
- `app/mesh/service.py`: inbound → `intake.receive(source='mesh' | 'sensor')`
  (free text classified by the same parser as the app), road blocks,
  task status, acks. Outbound: `sync_outbox()` reads the **event log** from a
  cursor and turns alerts, dispatches, cancels, re-routes and road blocks into
  packets, so no planner code knows the mesh exists.
- `app/api/v1/mesh.py`: `POST /mesh/inbound`, `GET /mesh/outbox`,
  `POST /mesh/outbox/ack` (header `X-Mesh-Gateway-Key`; disabled when
  `MESH_GATEWAY_KEY` is unset), `POST /ingest/sensor` (camera over HTTPS,
  same handling and dedup as its mesh packet), `GET /mesh/status` (staff).
- Trust: `SOURCE_CREDIBILITY` gains `mesh` 0.66 and `mesh_unsigned` 0.45
  (`sensor` 0.88 already existed).
- `scripts/mesh_bridge.py`: laptop bridge (phone inbox ↔ API, respects the
  phone's 5 s window) and `sim` mode (no phone: hop delay, loss, a duplicate
  via a second gateway).

bitchat fork (`D:\GITHUB\bitchat-android`): `IndradhanuGateway.kt` (inbox ring
buffer, disk-persisted store-and-forward queue, outbox pull and broadcast),
`GET /inbox`, `GET/POST /gateway`, private-network CORS header, hooks in
`UnifiedMeshService`, `VlmMessageHandler`, `MeshForegroundService`. Documented
in that repo's `docs/INDRADHANU_GATEWAY.md`. **Not compiled here**; build in
Android Studio.

Citizen app: `src/lib/mesh.ts` + `components/common/MeshPanel.tsx`. Shown only
when the API is unreachable: finds bitchat on `127.0.0.1:8765`, sends the typed
report with GPS as an R packet, shows alerts and road closures heard on the
mesh filtered to this location, checks the route saved while online against
closures, and falls back to a compass heading to the nearest known shelter,
labelled as a heading, not a route. The last route is kept in `localStorage`.

ai-surveillance (`D:\GITHUB\ai-surveillance`): `core/indradhanu_bridge.py`
sends confirmed hazards (fire, smoke, fall, fight, gathering, object left;
never identity, phone or smoking) over HTTPS, falling back to the mesh, with
threshold and cooldown. `disaster_mode` switches off face, ReID, identity
fusion, phone, smoking and PAR before anything loads. Hooked into
`pipeline/main_loop.py`; configured under `indradhanu:` in
`configs/pipeline.yaml` (disabled by default).

---

## 5. Scenario library

`app/demo/scenarios.py`; `GET /demo/scenarios`,
`POST /demo/scenarios/{name}/run`, `GET /demo/scenarios/last`. Eight scripted
cases that use the real pipeline and then **check** the outcome:
`flood_cascade`, `block_on_approach`, `unit_breakdown`, `officer_redirect`,
`officer_hold`, `camera_fire_mesh`, `mesh_blackout_sos`, `multi_hazard_surge`.
Progress appears as console beats; checks are PASS/FAIL lines.

---

## 6. Configuration added

| Variable | Where | Meaning |
|---|---|---|
| `MESH_GATEWAY_KEY` | API (Render), gateway phones, bridge, camera | Shared secret for the gateway endpoints. Unset = mesh endpoints return 403 |
| `MESH_HMAC_KEY` | API, camera node, bridge | Signs/verifies IDX1 packets. Citizen packets are unsigned by design (a key in a web page is not secret) |

---

## 7. Not verified here, and why

- **Backend not run end to end.** No Python packages can be installed in either
  environment this session had (PyPI blocked). Pure logic is unit-tested;
  every new SQL statement that reads was run against the live DB; imports and
  names are checked with ruff (no new F-class findings).
- **Kotlin not compiled.** No Android SDK. Build in Android Studio.
- **Frontend**: `tsc -p tsconfig.app.json --noEmit` passes on the dev machine.
  `vite build` was not run (native binaries are Windows builds).
- Nothing has been committed to git. Review the diff, then commit.

## 8. Run this next

```bash
cd backend
python -m unittest discover -s tests -v          # 26 tests
uvicorn app.main:app --reload
# console → start the demo → POST /api/v1/demo/scenarios/block_on_approach/run
python scripts/mesh_bridge.py sim --api http://localhost:8000 --key $MESH_GATEWAY_KEY
```
