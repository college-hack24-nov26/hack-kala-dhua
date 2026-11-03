# End-to-end scenarios (MISC-04 required demonstrations)

`python -m ml.scenarios` replays five named scenarios on **held-out test storms
(2023–2026)**. In each one, routing on current status alone (the baseline the
brief asks for) is compared with predicted routing, under matched inputs:

- the same storm, the same evidence and the same fleet;
- the same shelters and the same injected events.

Code: `backend/ml/scenarios.py`. Outputs: `data/scenarios/<scenario>/`.

**Inputs are a replay, never live.**

- The weather and river drivers are the real historical ones: ERA5 rain and GloFAS discharge for that window.
- The roads, hazards, people and reports are simulated by `ml.sim`.
- Every logged row carries `t_replay`, the historical UTC time of that moment, plus `t_min` and `time_label = "REPLAY"`.
- The fleet, shelters, schools, hospitals and mutual-aid offers come from the live database, frozen on 2026-10-10 in `data/scenarios/district_snapshot.json`.

## What runs inside a scenario

The planning loop runs every 10 minutes. It does what the live system does:

1. **Evidence becomes incidents.** Reports are grouped into incidents. The trust rules then decide each incident's state:
   - *confirmed*: two or more independent reports, or a sensor or patrol agrees, or one reliable report of a fire, crash or collapse;
   - *disputed*: a dry sensor reading or a patrol report contradicts it;
   - *unverified*: anything else;
   - *expired*: unverified for 2 hours, or a "clear" report arrives later.
2. **Incidents get needs per capability.**
   - Unverified or disputed incidents get one assessor first.
   - The assessor's arrival reveals the truth: the incident is confirmed or dismissed.
3. **Assignment.** The production solver assigns units (`app.solver.allocation.allocate`, CP-SAT).
   - One job per unit, and a 45-minute reach limit.
   - A switching cost keeps committed actions in place.
   - Every need left uncovered gets a logged reason.
4. **Driving through the truth.**
   - A road that is really blocked when the unit gets there is an *invalid route*. The crew turns back, reports the road (from then on it is a binding closure) and is rerouted.
   - At every planning step, each en-route unit's remaining route is re-checked. Units are rerouted on a binding closure, or (predicted policy) on a predicted closure.
5. **Shelters and transport.**
   - Buses reserve shelter places at dispatch, so a shelter can never overflow. With no place left, the need is unmet and the reason is logged.
   - Ambulances carry patients to the nearest hospital.
   - JCBs clear the debris they were sent to, which changes the simulated world.
6. **Surge (insufficient capacity only).** The ladder from `app.surge` runs inside the replay:
   - triage first;
   - then mutual aid, which arrives as real units after its response time, or is declined;
   - then schools opened as shelters;
   - then a declaration that an officer approves.

The policies:

| Policy | What it knows about the roads |
|---|---|
| `current_status` | Closed only where the evidence says so now (B0) |
| `predicted` | B0's closures stay binding, plus the passability model's P(blocked at arrival). Each segment costs time × (1 + 4p) + 900 s × p. A segment is avoided at p ≥ 0.6, or at p ≥ 0.4 with p + 2 sd ≥ 0.6 (as live) |
| `oracle` | The truth at decision time (best possible; shown for scale only) |
| `naive_trust` | Predicted routing, but every report is believed at once (ablation of the trust rules) |

## Results

Notes on the table:

- **real / false**: incidents that were real or false according to the truth. This is used for scoring only and is never seen by the planner.
- **invalid**: legs that reached a road really blocked at that moment.
- **p50 / p90**: minutes from the first report to the first unit on scene, for real incidents.
- **unreached**: real incidents with no unit on scene by the end of the window.

| Scenario (storm, window) | Policy | real / false | dispatches | invalid routes (rate) | response p50 / p90 min | real reached / unreached | crews sent to false reports | sheltered |
|---|---|---|---|---|---|---|---|---|
| **Normal operation**<br>20230516-06 r0, 12 h, dry day | current_status | 10 / 6 | 33 | 1 (3.2%) | 14.0 / 19.0 | 10 / 0 | 2 | 0 |
| | predicted | 10 / 6 | 33 | 1 (3.2%) | 14.0 / 15.3 | 10 / 0 | 2 | 0 |
| | oracle | 10 / 6 | 31 | 0 | 11.4 / 15.2 | 10 / 0 | 2 | 0 |
| **Deteriorating access**<br>20240724-08 r0, 12 h over the river's rise | current_status | 33 / 8 | 72 | 4 (6.1%) | 16.6 / 36.1 | 30 / 3 | 0 | 90 |
| | predicted | 32 / 8 | 71 | **0** | 16.4 / 38.5 | 30 / 2 | 0 | 90 |
| | oracle | 33 / 8 | 70 | 0 | 15.3 / 26.7 | 31 / 2 | 0 | 90 |
| **Confirmed closure**<br>20250927-18 r0, 10 h; bridge closed at +3 h | current_status | 84 / 34 | 229 | 64 (28.8%) | 21.4 / 58.8 | 72 / 12 | 0 | 225 |
| | predicted | 87 / 34 | 282 | **23 (10.1%)** | 19.5 / 53.7 | **86 / 1** | 1 | 495 |
| | oracle | 85 / 34 | 240 | 0 | 15.0 / 25.3 | 84 / 1 | 0 | 270 |
| **Insufficient shelter & transport**<br>20260704-23 r1, 12 h; 40% of fleet, 25% of shelter space | current_status | 387 / 120 | 397 | 67 (35.9%) | 51.1 / 116.9 | 73 / 314 | 2 | 360 |
| | predicted | 376 / 117 | 505 | 61 (28.7%) | **39.1** / 127.6 | **98** / 278 | 6 | 270 |
| **Conflicting & stale reports**<br>20240925-00 r0, 12 h; 91 false and 171 stale reports in the run | current_status | 258 / 73 | 286 | 125 (45.8%) | 39.4 / 125.7 | 88 / 170 | 5 | 585 |
| | predicted | 255 / 74 | 385 | 69 (22.6%) | **35.2** / 104.3 | **142** / 113 | **5** | 225 |
| | naive_trust | 235 / 67 | 243 | 36 (19.5%) | 102.9 / 387.6 | 97 / 138 | **68** | 540 |

### What each scenario shows

- **Normal operation.**
  - On a dry day the model has nothing to add: current-status and predicted routing make identical decisions.
  - The planner sends the right mix per incident: crash → ambulance and police; tree → JCB or rescue team; unverified → assessor.
  - 5 false or stale reports were dismissed by an assessor; 2 crews still reached reports that turned out false.
- **Deteriorating access.**
  - Roads open at departure flood before the unit arrives.
  - Current-status routing hit 4 such roads.
  - Predicted routing hit none, with the same response times.
- **Confirmed closure.**
  - The bridge closure is binding in every policy (official notice 10 minutes after it closes). From then on, no route in any policy tried to cross it: no invalid route on that bridge.
  - Committed units kept their task unless a road ahead of them closed; re-tasking only happened when a higher-weight need paid the switching cost (`unit.reassigned` in the log, with the reason).
  - Predicted routing cut invalid routes from 28.8% to 10.1%.
  - It reached 86 real incidents against 72, leaving 1 unreached against 12.
- **Insufficient capacity.**
  - The ladder climbed to level 4: the declaration was approved after 20 minutes.
  - 22 mutual-aid units arrived; the scenario logs show which offers were declined.
  - One school opened as a surge shelter; the other candidate schools were in wards with confirmed flooding, so they were skipped.
  - **No shelter overflowed.** Unmet needs carry reasons; the top one is "no mass-transport unit could reach this ward within 45 minutes".
  - Predicted routing reached 98 real incidents against 73, and the median response fell from 51 to 39 minutes.
  - Most real incidents stay unreached in both policies. That is the honest result of a 40% fleet in the largest held-out flood.
- **Conflicting and stale reports.**
  - The trust rules keep crews away from false reports: 5 crews went to false reports, against 68 when every report is believed.
  - Without the rules, crews were busy elsewhere, so the median response time to *real* incidents rose from 35 to 103 minutes.
  - On the road, predicted routing halved invalid routes against current status (69 against 125).

### Checks (all 14 runs)

- Units assigned to two jobs at once: **0**.
- Shelters filled beyond capacity: **0**.

These are counted in `summary.json → checks` and visible row by row in `resource_ledger_*.csv`.

### Predictions on the routes actually used

`predictions_predicted.parquet` stores every P(blocked) ≥ 0.05 the predicted policy used on a dispatched route. Each prediction is labelled with whether that road was really blocked for that unit class at arrival:

| Scenario | AUC |
|---|---|
| Conflicting reports | 0.77 |
| Insufficient capacity | 0.68 |
| Confirmed closure | 1.0 (few positives) |

These AUCs are lower than on the full test set (0.98), and that is expected. Routes are chosen to *avoid* likely-blocked roads, so the roads left on routes are the hard cases.

## Files per scenario and policy

| File | Contents |
|---|---|
| `events_<policy>.jsonl` | The replay log: every plan, incident change, dispatch, reroute, invalid route, arrival, closure, dispute, aid request or arrival, and shelter opening |
| `dispatch_log_<policy>.csv` | Unit, need, capability, planned ETA, solver engine, max P(blocked) on the route, and why |
| `route_checks_<policy>.csv` | Every leg: planned vs actual minutes, invalid, reroutes, arrived. Includes the secondary legs to a hospital or shelter |
| `resource_ledger_<policy>.csv` | Every unit status change, and every shelter reserve, release, arrival or opening, with capacity and occupancy |
| `unmet_<policy>.csv` | Each decision's uncovered needs, with the reason |
| `predictions_<policy>.parquet` | Predictions used, with truth labels |
| `summary.json` | Data provenance, window, injected events, and matched outcomes per policy |

## Limitations

- **One realization per scenario.** These are demonstrations of behaviour. The aggregate route evaluation over 1,576 test trips is in `TRAINING_AND_RELEARNING.md` §7.
- **Fixed assumptions.** Service times, people per flooded place, reach radii, and boats and teams travelling by truck are assumptions, fixed before the test storms were run (see the module docstring).
- **Planning cadence.** Response times include the planning cadence (10 minutes); the live system replans on every event.
- **Incident counts differ slightly between policies.** Incidents are built from reports, and crews' actions (clearing debris, dismissing reports) change what is reported afterwards.
