# Flood and live-hazard simulator (MISC-04, W2)

One district (PCMC, Pawana corridor, Pune), one hazard (flood) plus the live
hazards that come with it, on the bounded road graph built by `ml.graph`
(43,564 segments, 3,184 km drivable). Inputs are real; the road-level outcome
is simulated, because no public road-level record of PCMC flooding exists.

```
python -m ml.fetch.osm          # on a machine that can reach Overpass
python -m ml.fetch.meteo        # ERA5 (archive-api.open-meteo.com) + GloFAS (flood-api)
python -m ml.fetch.dem          # Copernicus GLO-30 tile (AWS open data)
python -m ml.graph              # segments, junctions, signals, bridges, underpasses
python -m ml.terrain            # elevation, slope, HAND, sink, distance to river
python -m ml.sim.static         # per-segment features, wards, sectors, adjacency
python -m ml.sim.storms         # storm catalogue + 30-min drivers
python -m ml.sim.run            # 44 storms x 3 realizations -> data/sim/runs (~300 MB, regenerable)
python -m ml.sim.check          # plausibility checks -> data/sim/check.json
pytest tests/test_sim_world.py
```

## What is real and what is simulated

| Input | Source | Real / simulated |
|---|---|---|
| Roads, bridges, underpasses, signals, land use, hospitals, schools | OpenStreetMap (Overpass, 2026-10-10) | real |
| Terrain (elevation, slope, height above river, sinks) | Copernicus GLO-30 DEM | real (surface model: roofs and trees bias it; see below) |
| Hourly rain, wind, gusts, cloud 2015-2026 | ERA5 via Open-Meteo, two points (district, Maval upstream) | real, 25 km |
| Daily river discharge 2015-2026 | GloFAS v4 via Open-Meteo, 4 cells | real reanalysis to Jul 2022, forecast-based after (flagged per storm) |
| Storm dates and strengths | the wettest real spells in those records | real |
| Water depth on each road, every 30 min | `world.py` | **simulated** |
| Trees, wires, collapses, landslides, fires, gas, bridge closures | `world.py` | **simulated** |
| Traffic and crowds | `world.py` | **simulated** |
| Reports, patrols, sensors, gauges, traffic feed | `observe.py` | **simulated** from the simulated truth |

## Storms and splits

44 windows of 72 h (26 major, 18 moderate). A day is wet if district rain
≥ 50 mm, upstream rain ≥ 90 mm, or any river cell ≥ 0.8 × its median annual
peak; a spell gives up to three non-overlapping windows. Split by year, never
inside a storm: **train 2015-2021 (28 storms), tune 2022 (3), test 2023-2026
(13)**. The 25 Jul 2024 storm (`20240724-08`) is in test.

Each storm is run as 3 realizations (seeded by storm id and realization):
different rain field, blocked drains, failed pumps, trees, reports.

## The world (`world.py`, every coefficient in `PARAMS`)

* **River.** Stage from discharge: inside the banks `bank·√(Q/Q_bf)`, above
  bankfull an overbank depth rising to `h_top` (3 m ± 20%) at the record peak.
  `Q_bf` = median annual peak of the cell, `bank` = 10th percentile of HAND
  within 100 m of the river. Depth on a road = stage − HAND (± 0.5 m DEM error),
  faded out 150-900 m from the river. Causeways go under at 60% of bank height
  and are closed at 50%; main river bridges close near a record flood.
* **Rain ponding.** A bucket per segment: rain × catchment in, effective drain
  capacity out (main 45, minor 35, local 30 mm/h; underpass pumps 40, 20% fail
  to 6). 10-25% of drains blocked (+10% in June). Low spots (`pond_m`, sink
  beyond 1.5 m) and underpasses collect more and stand deeper, capped. ERA5
  hourly rain is split unevenly inside the hour, intense hours boosted ×1-1.6,
  and spread by a smooth random field (sd 0.3).
* **Live hazards** (Poisson, as events with start/end/place): trees from gusts
  above 20 km/h and rain; 25% of tree falls bring a wire down; wall collapses
  from 48 h rain on dense local roads near nallahs; landslides on slopes ≥ 15%;
  fires from downed wires and waterlogged industrial units; gas leaks from
  flooded industrial land; bridge closures by the authority rule.
* **Traffic and crowds.** Time-of-day congestion by road class, +rain, spill-over
  to roads one and two junctions from every blocked road. Crowds at hospitals,
  at shelters while nearby areas flood, onlookers on bridges in high water.
* **Air** (kept for the drone swarm, which is paused): wind, gusts, rain,
  plumes per step; no-fly over military land.

Calibration (assumption, not measurement): drain capacities were set so that a
25-50 mm day ponds almost nowhere and the 25 Jul 2024 storm floods a few hundred
segments; `h_top` so that the record discharge wets several hundred riverside
segments.

## What the command centre sees (`observe.py`)

citizen reports (late, mis-placed, depth class often off, rumours, hoaxes, stale
forwards, "clear now" reports), patrols (few, quick, 97% right, steered to
recent reports), official closure/reopen/cleared notices (right but 15-60 min
late), water-level sensors at 12 underpasses + 4 riverside roads (noise,
drop-outs, one stuck sensor in half the runs), river gauges, and a traffic
feed in which a closed road usually shows *no data*. Drones are not a source
while paused. Every report keeps its hidden truth (`truth_seg`, `is_false`,
`is_stale`) for scoring only; predictors must not read those columns.

## Outputs per run: `data/sim/runs/<storm>/r<k>/`

| File | Content |
|---|---|
| `truth.npz` | `depth`, `fluvial` (float16 m), `flags` (bitmask: flowing, debris, wire, collapse, landslide, bridge closed), `jam`, `crowd` (uint8 /250), `rain` (mm/h per step), `field` (per segment), `stage` (per river cell) — arrays are [step, segment] |
| `events.parquet` | kind, seg, x, y, lon, lat, t0, t1 (steps), radius_m, cause |
| `air.parquet` | per step wind, gusts, rain, plumes |
| `obs/reports.parquet` | obs_id, source, kind, t_true_min, t_obs_min, seg_rep, x, y, depth_class, depth_est_m, blocked_est, reliability, truth_seg, is_false, is_stale, stale_of |
| `obs/sensors.parquet` | source (sensor/gauge), site, seg, t_min, value_m, faulty |
| `obs/traffic.npz` | `level` [step, road] 0-3 or 255 = no data; `segs` |
| `meta.json` | seed, params, split, peak counts |

## Plausibility check (`check.py`, data/sim/check.json)

* 25 Jul 2024 sitrep: the Vallabhnagar underpass (Yeshwantrao Chavan Rd, by the
  ST stand) is under ≥ 30 cm on the 25th in 3/3 realizations. Pimple Nilakh is
  mostly south of the district's box; riverside Mula roads inside it flood in
  2/3 realizations.
* The worst simulated storms are Aug 2020, Sep 2017, Sep 2016, Jul 2026 and
  Sep 2024; moderate days rank at the bottom (median rank 35.5 of 44 vs 13.5
  for major).

## Known limitations

* Not a hydraulic model: no flow routing, no dam operations, no backwater at
  the Mula-Pawana confluence. GloFAS daily means smooth the peak.
* GLO-30 is a surface model; HAND and sinks are biased by buildings and trees
  (partly absorbed by the bank percentile and the 1.5 m sink threshold).
* Over a tunnel the DEM sees the deck above, so underpasses are treated by rule.
* The Vallabhnagar underpass stays flooded for the whole of 25 Jul in every run:
  the bucket drains slowly when rain is steady, which likely overstates duration.
* The Aug 2019 Pawana flood (the biggest in local memory) ranks only 7th:
  GloFAS puts its discharge below Aug 2020's.
