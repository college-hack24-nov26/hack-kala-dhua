/** The dispatch benchmark, as the harness printed it.
 *
 *  Produced by `backend/scripts/benchmark_strategies.py` on 12 Sep 2026 and
 *  committed rather than computed in the browser: the simulation takes minutes
 *  and says nothing about the live demo world, so pretending it is live would
 *  be a worse claim than a dated one. Re-running the harness and pasting the
 *  output here is the whole update procedure.
 *
 *  A plain module rather than a JSON import, so the shape is typed at the point
 *  of use and no tsconfig flag has to be turned on for one file.
 */
export const benchmark = {
  "generatedAt": "2026-09-12",
  "harness": "backend/scripts/benchmark_strategies.py --offline --fleet 0.3 --minutes 180 --seed <s>",
  "conditions": {
    "seeds": [
      7,
      11,
      23,
      42,
      101
    ],
    "fleetFraction": 0.3,
    "simMinutes": 180,
    "units": 25,
    "demands": 385,
    "city": "synthetic offline city (harness --offline), not the seeded Pune fleet",
    "travelTimes": "straight-line at urban speed with road blocks applied, identical for every arm",
    "solver": "CP-SAT (OR-Tools) on both optimised arms"
  },
  "arms": [
    {
      "key": "nearest",
      "label": "Nearest-first",
      "note": "Closest free unit that can do the job. No look-ahead, no revisiting.",
      "stats": {
        "unmet": 70,
        "unmet_pct": 18.2,
        "assign_p50": 2.2,
        "assign_p90": 47.3,
        "arrive_p50": 29.1,
        "arrive_p90": 71.0,
        "switches": 0,
        "engine": "nearest-first"
      }
    },
    {
      "key": "oneshot",
      "label": "Optimised once",
      "note": "Same CP-SAT model, but an assignment once made is never revisited.",
      "stats": {
        "unmet": 70,
        "unmet_pct": 18.2,
        "assign_p50": 2.5,
        "assign_p90": 51.6,
        "arrive_p50": 32.6,
        "arrive_p90": 74.7,
        "switches": 0,
        "engine": "cp-sat"
      }
    },
    {
      "key": "indradhanu",
      "label": "Indradhanu",
      "note": "Same solver, re-run as the world changes, paying a switching cost to move a committed unit.",
      "stats": {
        "unmet": 63,
        "unmet_pct": 16.4,
        "assign_p50": 2.3,
        "assign_p90": 39.8,
        "arrive_p50": 34.1,
        "arrive_p90": 107.5,
        "switches": 163,
        "engine": "cp-sat"
      }
    }
  ],
  "perSeed": [
    {
      "seed": 7,
      "demands": 91,
      "units": 25,
      "arms": {
        "nearest": {
          "unmet": 11,
          "assignP50": 2.6,
          "assignP90": 47.3,
          "arriveP50": 33.9,
          "arriveP90": 63.3,
          "switches": 0,
          "engine": "nearest-first"
        },
        "oneshot": {
          "unmet": 11,
          "assignP50": 2.5,
          "assignP90": 51.6,
          "arriveP50": 32.6,
          "arriveP90": 78.7,
          "switches": 0,
          "engine": "cp-sat"
        },
        "indradhanu": {
          "unmet": 11,
          "assignP50": 2.6,
          "assignP90": 28.0,
          "arriveP50": 35.4,
          "arriveP90": 110.6,
          "switches": 52,
          "engine": "cp-sat"
        }
      }
    },
    {
      "seed": 11,
      "demands": 89,
      "units": 25,
      "arms": {
        "nearest": {
          "unmet": 16,
          "assignP50": 2.2,
          "assignP90": 54.1,
          "arriveP50": 31.8,
          "arriveP90": 78.8,
          "switches": 0,
          "engine": "nearest-first"
        },
        "oneshot": {
          "unmet": 15,
          "assignP50": 2.5,
          "assignP90": 54.1,
          "arriveP50": 33.0,
          "arriveP90": 72.1,
          "switches": 0,
          "engine": "cp-sat"
        },
        "indradhanu": {
          "unmet": 10,
          "assignP50": 2.2,
          "assignP90": 39.8,
          "arriveP50": 35.3,
          "arriveP90": 115.9,
          "switches": 49,
          "engine": "cp-sat"
        }
      }
    },
    {
      "seed": 23,
      "demands": 58,
      "units": 25,
      "arms": {
        "nearest": {
          "unmet": 10,
          "assignP50": 2.2,
          "assignP90": 32.0,
          "arriveP50": 28.1,
          "arriveP90": 46.8,
          "switches": 0,
          "engine": "nearest-first"
        },
        "oneshot": {
          "unmet": 11,
          "assignP50": 2.0,
          "assignP90": 42.2,
          "arriveP50": 26.6,
          "arriveP90": 51.0,
          "switches": 0,
          "engine": "greedy-fallback"
        },
        "indradhanu": {
          "unmet": 10,
          "assignP50": 2.5,
          "assignP90": 23.0,
          "arriveP50": 26.6,
          "arriveP90": 58.3,
          "switches": 9,
          "engine": "cp-sat"
        }
      }
    },
    {
      "seed": 42,
      "demands": 76,
      "units": 25,
      "arms": {
        "nearest": {
          "unmet": 19,
          "assignP50": 1.8,
          "assignP90": 39.2,
          "arriveP50": 29.1,
          "arriveP90": 71.0,
          "switches": 0,
          "engine": "nearest-first"
        },
        "oneshot": {
          "unmet": 19,
          "assignP50": 1.9,
          "assignP90": 38.0,
          "arriveP50": 32.7,
          "arriveP90": 74.7,
          "switches": 0,
          "engine": "greedy-fallback"
        },
        "indradhanu": {
          "unmet": 19,
          "assignP50": 1.8,
          "assignP90": 41.8,
          "arriveP50": 34.1,
          "arriveP90": 100.2,
          "switches": 27,
          "engine": "cp-sat"
        }
      }
    },
    {
      "seed": 101,
      "demands": 71,
      "units": 25,
      "arms": {
        "nearest": {
          "unmet": 14,
          "assignP50": 2.5,
          "assignP90": 106.0,
          "arriveP50": 27.9,
          "arriveP90": 109.9,
          "switches": 0,
          "engine": "nearest-first"
        },
        "oneshot": {
          "unmet": 14,
          "assignP50": 2.5,
          "assignP90": 79.8,
          "arriveP50": 29.2,
          "arriveP90": 114.0,
          "switches": 0,
          "engine": "greedy-fallback"
        },
        "indradhanu": {
          "unmet": 13,
          "assignP50": 2.3,
          "assignP90": 67.6,
          "arriveP50": 29.2,
          "arriveP90": 107.5,
          "switches": 26,
          "engine": "cp-sat"
        }
      }
    }
  ]
}
