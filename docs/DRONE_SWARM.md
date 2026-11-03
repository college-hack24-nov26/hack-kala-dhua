# Drone swarm live feed → command centre

A 12-drone rescue swarm (PyBullet physics, decisions by TypeSafe Jev) runs on a
teammate's laptop and broadcasts its state at `wss://<cloudflare tunnel>/ws`
(spec: init once, frames ~12 Hz, log lines; the socket drops and `t` resets
about every 3 minutes when the scenario restarts). `backend/app/drone/swarm.py`
is a read-only listener that starts with the API.

## What it changes

| Feed event | Becomes | Effect downstream |
|---|---|---|
| `found` gains a site | `intake.receive(source='sensor', category=person_stranded)` at the site's real coordinates | trust scoring (sensor 0.88), dedup, Commander nudge, re-plan: the allocator dispatches and routes a ground unit to the survivor |
| `delivered` gains a site | `drone.payload_delivered` event on that incident | the timeline shows supplies are in; extraction still needs the ground unit |
| `AGENT … obstacle_detour` while the drone is over a road (snapped ≤ 60 m) | a `road_blocks` row, `reported_by='drone-swarm'`, 150 m | router, allocator and citizen guidance avoid it; re-plan; auto-cleared after 20 min (`road.cleared`) |
| drone rows | `mesh_nodes` kind `drone` every 5 s (state, battery, altitude, heading) | Mesh & devices map |
| `AI: Dn -> Jev: …` | parsed decisions (sector, bids with probability) | `GET /drone/swarm` |

Limits: at most 2 drone road blocks per run, none within 200 m of an active block;
a site already filed is not re-filed for 15 min (the intake's dedup window), so
the 3-minute restarts do not multiply incidents.

## Placing the sim on the map

The sim district is ±16.25 × ±12.75 m around its origin, Z-up. Its origin is
anchored to a real point and scaled (default 8 real m per sim m, so ~260 × 200 m):

1. an anchor an officer set (`POST /drone/swarm`), persisted in `mesh_state`
2. `DRONE_SWARM_ANCHOR="lat,lon"`
3. the newest open life-safety incident in `DRONE_SWARM_REGION` (default pune)
4. the riskiest ward, then the region centre

## Operating it

- New tunnel host: `POST /api/v1/drone/swarm {"url": "wss://<new-host>/ws"}` (staff). It survives redeploys.
- Off: `{"enabled": false}` or `DRONE_SWARM=0`. Re-anchor: `{"anchor_lat": .., "anchor_lon": ..}`. Scale: `{"scale": 10}`.
- Status: `GET /api/v1/drone/swarm`: connection, run, phase, drones with lat/lon, survivors with their incident ids, recent Jev decisions, and what the listener changed.
- When the host's laptop is off the tunnel answers 502; the listener backs off to one try a minute and logs nothing alarming.
