# MISC-04 evidence map

Where each requirement of the brief is met, and how to reproduce it.

| Requirement | Where | Reproduce / see |
|---|---|---|
| One district, one hazard, bounded road graph | PCMC Pawana corridor, flood; 43,564 OSM segments | `ml/graph.py`, `data/processed/graph/summary.json` |
| Settlements and shelters | 14 PCMC wards (029), shelters/relief centres, 8 surge schools (034) | Supabase `wards`, `lifelines` |
| A1: assign teams, transport, shelter by priority, travel time, requirements, road status | CP-SAT allocation over capability needs; ETA from routes avoiding confirmed + predicted blocks | `app/solver/allocation.py`, `app/agents/replan.py` |
| A1: no conflicting unit use, no shelter overflow | one job per unit (AddAtMostOne), shelter_full incidents, guidance skips full sites | allocation constraints; `app/demo/runner.py::_move_people` |
| A1: unmet needs and why | `allocation_plans.uncovered` with reasons; Surge page | `/admin/surge`, `/admin/allocation` |
| A1: replan after closure / hazard change / resource loss; keep committed work | event router triggers; switching cost and dwell | `app/agents/event_router.py`, `replan.py` |
| A1: capacity exhausted -> escalation | surge ladder: triage, mutual aid, surge shelters, declaration | `app/surge/service.py`, `/admin/surge`, scarcity drill |
| A2: P(road usable at arrival) from time-stamped evidence | passability model | `ml/features.py`, `ml/train_passability.py` |
| A2: predictive uncertainty in route choice | 3-5 member ensemble, p + 2 sd used for avoidance | `app/nav/live.py::predicted_blocks` |
| A2: confirmed closures binding | p = 1 for active closures until a reopen | `app/nav/passability.py::Scorer.score` |
| A2: vs current-status-only on identical held-out sequences | same trips, same evidence, both planners | `ml/route_eval.py`, `data/eval/routes_test.json` |
| A2: accessibility quality, invalid routes, delays | AUC, Brier, ECE, misses; invalid-route rate; delay vs oracle | `data/eval/passability_metrics.json`, `routes_test.json` |
| Honest evaluation: split by time, grouped by storm, tuned on 2022 only | train 2015-21, tune 2022, test 2023-26 | `ml/sim/storms.py` |
| Predictors never see the truth | truth columns dropped at load; tests | `tests/test_ml_features.py` |
| Labels, counts, class balance, units, thresholds | metrics JSON + docs | `docs/TRAINING_AND_RELEARNING.md` |
| Real / replayed / simulated declared | real inputs, simulated road outcomes | `backend/ml/sim/README.md` |
| Saved predictions with outcomes | test predictions per row and per trip | `data/eval/*_test_predictions.parquet`, `routes_test.parquet` |
| Model identity and version | registry + model.json (params, features, threshold) | `models/registry.json`, `model_registry` table |
| Dispatch logs, resource ledgers | events (hazard x sector tagged), per-unit logs, exports | `/admin/units` (CSV/JSON), `GET /units/log/export`, `agent_log` |
| Road/event replays | replay frames; simulator runs per storm | `/admin/replay`, `data/sim/runs` |
| >= 4 scenarios | storms (normal/moderate/major/everyday) + console scenarios + scarcity drill | `app/demo/scenarios.py`, `/admin/surge` |
| A limitation / failed case | listed | `docs/TRAINING_AND_RELEARNING.md` §6, sim README |
