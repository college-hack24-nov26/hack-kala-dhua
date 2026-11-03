# Training and re-learning, step by step (MISC-04)

Last updated 2026-10-10. Everything below was run end to end on these files;
numbers are from that run unless marked otherwise.

## 0. Database (your side)

| Migration | What it adds | Status |
|---|---|---|
| 029_pcmc_pccoe_zone | 14 PCMC wards, 32 units, 16 lifelines round PCCOE | **applied** (you, SQL editor; verified 14/32/16) |
| 030_hazard_sector_agent_log | hazard + sector tags on every event, `agent_log` view, sector tables | **applied** (verified: trigger `events_fill_tags`, view works) |
| 031_relearning_loop | `model_registry`, `nav_predictions`, `nav_outcomes`, `trip_outcomes`, `nav_examples` view, trigger from `road_blocks` | **to apply** |

To apply 031:

1. Supabase dashboard → project **Indradhanu** → SQL Editor → New query.
2. Paste all of `backend/migrations/031_relearning_loop.sql` → Run. It is additive (new tables, one trigger on `road_blocks`, one view); it parses clean (22 statements).
3. Check: `select count(*) from model_registry;` returns 0, and
   `select tgname from pg_trigger where tgname = 'road_blocks_outcomes';` returns one row.
4. Optional, so `ml.relearn` mirrors the registry into the database: put
   `SUPABASE_DIRECT_CONNECTION_STRING=postgresql://...` in `backend/.env`
   (Dashboard → Connect → Direct connection). Without it the loop keeps its
   registry in `models/registry.json` only.

## 1. Set up (once)

```
cd DisruptionOps
python -m venv .venv && .venv\Scripts\activate          # Windows
pip install -r backend/requirements.txt -r backend/ml/requirements.txt
cd backend
```

The raw inputs are in the repo already (OSM, ERA5, GloFAS, processed graph and
terrain). Only the 44 MB DEM tile is not; you need it only to rebuild terrain:
`python -m ml.fetch.dem`.

## 2. Train, step by step

| # | Command (from `backend/`) | Time* | Output |
|---|---|---|---|
| 1 | `python -m ml.sim.static` | 5 s | per-segment features, wards, sectors, adjacency |
| 2 | `python -m ml.sim.storms` | 10 s | 44 real storm windows + drivers |
| 3 | `python -m ml.sim.run` | 3 min | 132 simulated runs (truth + what was seen), ~300 MB |
| 4 | `python -m ml.sim.check` | 10 s | plausibility checks (`data/sim/check.json`) |
| 5 | `python -m ml.samples` | 5 min | 1.42 M training rows (`data/train/passability.parquet`) |
| 6 | `python -m ml.train_passability` | 25 min | model in `models/passability/pass-*`, metrics + test predictions in `data/eval/` |
| 7 | `python -m ml.relearn register --task passability --version pass-XXXX --champion` | 1 s | marks it the live model |
| 8 | `python -m ml.route_eval --splits test --realizations 0 1` | 40 min | invalid routes, delays vs current-status routing |
| 9 | `python -m ml.route_eval --splits train tune --realizations 0` | 45 min | trips the ETA model learns from |
| 10 | `python -m ml.train_eta` | 1 min | ETA model in `models/eta/eta-*`, `data/eval/eta_metrics.json` |
| 11 | `python -m ml.relearn register --task eta --version eta-XXXX --champion` | 1 s | live ETA model |
| 12 | `pytest` | 1 min | all tests (169) |

\*on a 2-core machine. Every step is seeded: the same commands give the same numbers.

### What the model learns

* **Question.** "If this unit leaves now, will this road be blocked for it when
  it gets there, 30/60/90 minutes from now?" for an ambulance (0.3 m water
  limit), a fire tender (0.6 m) and a person on foot (0.2 m).
* **Label.** Blocked at arrival by the router's own rules
  (`app/nav/hazards.py`; `ml/labels.py` is the same rules over arrays, tested equal).
* **Inputs.** Only what was known at decision time (50 features): terrain
  (height above river, low spots, underpass, bridge), rain so far and forecast,
  river gauge level and rise, citizen reports near the road (reliability-
  weighted, depth said, how old, "clear now"), hazard reports, patrol
  sightings, official closures, water sensors, live traffic (and "no data"),
  plus what the current-status rule says now. Reports' hidden truth is
  dropped before features are built (tested).
* **Model.** 5 XGBoost models averaged (their spread = uncertainty),
  monotone where physics is one-way, calibrated (isotonic) on 2022.
* **Split.** Train 2015-2021, tune 2022, test 2023-2026, by storm. Never
  inside a storm.

## 3. Results on the held-out storms (2023-2026)

Road level, 429,592 test rows (11.2% blocked at arrival):

| | AUC | Misses a closure | Blocks an open road | Calibration error |
|---|---|---|---|---|
| **Model** | **0.978** | **6.5%** | 2.7% | **0.001** |
| Logistic regression (same inputs) | 0.940 | 4.5% | 82% | 0.027 |
| Current-status-only | 0.617 | 88.5% | 0.8% | 0.012 |

Operating point chosen on 2022 only: catch at least 90% of closures.

Roads that **change** before the unit arrives (the reason to predict):

| | Model | Current-status-only |
|---|---|---|
| Open now, closed on arrival: caught | **83%** (AUC 0.887) | 6% (AUC 0.513) |
| Blocked now, open on arrival: ranked | AUC 0.747 | - |

Trip level, 1,516 trips from real PCMC unit bases on the test storms, each
driven through the simulated truth (`data/eval/routes_test.json`):

| | Invalid route (hit a block) | Ended on foot | Arrival P50 | Arrival P90 | Delay vs best possible (mean / P90) |
|---|---|---|---|---|---|
| Current-status routing | 23.4% | 18.3% | 18.3 min | 68.9 min | 10.2 / 31.9 min |
| **Predicted routing** | **8.7%** | **9.5%** | **17.6 min** | **30.4 min** | **1.3 / 2.1 min** |
| Oracle (knows the truth) | 0.1% | 7.4% | 17.1 min | 28.6 min | - |

Prediction saves 8.7 min per trip on average; 15.5% of trips are faster by
more than half a minute and 6.7% slower (detours that turned out unnecessary).


## 4. ETA (Zomato/Swiggy-style, tuned for response)

Target: the whole response, mobilisation + driving, including turn-backs and
replans on roads that turned out blocked and the last stretch on foot.
XGBoost quantile model (P50 and P90) on the planned route (length, free-flow
time, signals, underpasses, bridges), live traffic and "no data" share, flood
risk on the route (sum and max of P(blocked)), rain, river level, hour and unit
type. P90 is conformally corrected on 2022. Trained on 1,493 trips (2015-21),
tested on 1,404 trips (2023-26).

| | Mean error | Within ±5 min | P90 covers |
|---|---|---|---|
| **Model (P50 / P90)** | **2.8 min** | **95.5%** | **91.1%** |
| Traffic-aware ETA (knows traffic, not floods) | 3.3 min | 94.3% | - |
| Distance rule (crow-fly × 1.35 at 18 km/h) | 11.2 min | 28.3% | - |

To flooded places only: 3.2 min vs 3.7 min. The gain over a traffic ETA is
small *because* the routes already avoid predicted floods; the P90 is what is
new: a dispatcher gets a likely time and a 90% bound. Biggest drivers (SHAP):
free-flow route time, share of the route with no traffic data (closed roads
show none), route length, river level, flood risk on the route.

## 5. The re-learning loop

The model starts from simulation. In operation it keeps learning from what
crews actually find. Pieces (all in the repo):

1. **Log predictions.** When the router scores roads for a trip it calls
   `app/nav/passability.py::log_predictions` → `nav_predictions` (segment,
   unit, horizon, p, model version, exact inputs). A sample of roads not on
   any route is logged as `shadow`, so labels are not only "roads we chose".
2. **Collect outcomes.** `nav_outcomes` gets a row whenever a road is seen:
   * crew or drone pins in `road_blocks` → trigger (blocked; cleared → open);
   * roads a unit actually drove → `log_driven` (open, source `trip`);
   * patrol and official status, sensors (same table, source column).
   Rule for the writers (the `road_blocks` trigger already follows it with
   0.95 for a crew pin; patrol/sensor writers are still to be wired): a
   single-source "blocked" gets confidence 0.6, a corroborated one (depth
   ≥ 25 cm said, or a second source within 100 m / 3 h) 0.95; only ≥ 0.8 is
   used (`ml/relearn.py::corroborated_label` is the same rule). Why: closed roads are ~1 in 100, so even a 97%-right patrol produces
   more false "blocked" than true ones (seen in the demo before this rule).
3. **Join.** `nav_examples` pairs each prediction with the outcome nearest
   `made_at + horizon` (±15 min, within the outcome's radius).
4. **Check.** `python -m ml.relearn check --source db`: drift (PSI of 10 main
   inputs vs training) and calibration on recent real outcomes.
   Ranking fine, probabilities off → `recalibrate` (minutes).
   Ranking worse or enough new data → `run`.
5. **Retrain.** `python -m ml.relearn run --source db`: same training code,
   on simulated storms + real examples (real ×5, half-life 180 days).
6. **Gate.** Champion vs challenger on (a) frozen simulated test storms and
   (b) the newest 30% of real examples, which the challenger never saw.
   Promote only if better on recent real (Brier −2%), recall not lower,
   underpasses not worse, frozen AUC/ECE within tolerance. Decision written to
   `data/relearn/decision_*.json` and `model_registry`.
7. **Serve.** The API loads the champion named in the registry; the old one
   stays on disk for rollback (`ml.relearn register --version <old> --champion`).

When to run: `check` after every storm day and weekly in monsoon; `run` when
`check` says so or after each major storm. A scheduled task can call both.

Inside a storm, learning is already immediate without retraining: every new
report, sighting and sensor reading changes the features of the next
prediction, a confirmed closure is binding (p = 1), and a crew's "open"
sighting caps p at 0.05 for 30 min.

### Demo of the loop (simulated outcomes, labelled as such)

`python -m ml.relearn demo` re-simulates three test storms in a **shifted
world** (drains far more blocked than the simulator assumed), lets the
champion predict as the router would, and takes the patrols' later,
corroborated sightings as outcomes:

| | Champion (sim-trained) | Challenger (sim + 8,834 real-like examples) |
|---|---|---|
| Check on 12,620 outcomes | drift flagged: river level, sensors (PSI 1.05, 0.83) → retrain | - |
| Recent hold-out (3,786 examples, 21 closed): Brier | 0.0026 | **0.0024** |
| Recent hold-out: calibration error | 0.0026 | **0.0015** |
| Recent hold-out: AUC / recall | 0.979 / 86% | **0.986** / 86% |
| Frozen test storms: AUC | 0.978 | 0.977 (within tolerance) |
| Decision | - | **promoted** (all 5 gates passed) |

The demo's challenger is kept as `demo-only` in the registry; the live
champion stays the simulation-trained model until real outcomes exist.

## 6. Limitations

* All labels are simulated until real outcomes arrive; the simulator is a
  physically-motivated proxy (see `ml/sim/README.md`).
* Precision at the 90%-recall operating point is low (15% on the natural mix):
  the router uses p as a cost, not a ban, and avoids outright only at p ≥ 0.6.
* The router in the API does not call the scorer yet (W5); the trip results
  above come from `ml/route_eval.py` on our own road graph.
* The relearn demo's recent hold-out had only 21 positives; in production the
  gate should wait for more.

## 7. Update: everyday disruptions, live serving (2026-10-10, evening)

**Everyday disruptions.** The simulator now runs disruptions on every day, rain
or not: old trees falling, water-main bursts flooding a road and its
neighbours, overflowing drains, old walls giving way, and crashes/breakdowns
(a quarter block the road). 36 dry-day `everyday` windows were added (80
windows, 240 runs). Confirmed everyday incidents in the live system (fallen
tree, waterlogging, collapse, downed line) are written as learning outcomes by
migration 033.

**New champion `pass-20261010-1049`** (3 members, trained in 6 min), on the
2023-26 test windows including everyday days (762,534 rows, 6.9% blocked):

| | AUC | Misses a closure | Blocks an open road | Calibration error |
|---|---|---|---|---|
| **Model** | **0.983** | **8.9%** | 1.1% | **0.0006** |
| Logistic regression | 0.954 | 6.4% | 81% | 0.016 |
| Current status only | 0.620 | 90.2% | 0.5% | 0.008 |

Roads open now that close before arrival: caught 68% (AUC 0.86) vs 4%.
Trips (1,576, realization 0, storms + everyday days): routes that hit a block
12.1% → **3.7%**; mean delay vs best possible 5.6 → **1.1 min**; P90 arrival
32.4 → 29.5 min (oracle 28.9).

**Live.** `app/nav/live.py` scores PCMC roads from the live database every 5
minutes (reports, crew sightings, closures, resolved incidents, Open-Meteo
rain, GloFAS river stage) and the router avoids roads with p ≥ 0.6 (or whose
ensemble upper bound crosses it). `GET /api/v1/nav/risk` serves the layer the
console map draws. The API image carries a 29 MB serving bundle
(`python -m ml.export_serving` after each new champion). `NAV_PREDICTIVE=0`
switches back to current-status routing.
