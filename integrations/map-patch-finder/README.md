# map-patch-finder → Indradhanu

The image localizer (github.com/tusharsingh-spring/map_matching-, deployed at
https://map-patch-finder.onrender.com) stays its own Render service. Two links:

1. **Their page → our log.** `app.py` here is theirs plus an optional webhook:
   after every match (upload or random demo) it POSTs a short summary and a
   240 px thumbnail to the command centre. Off unless the URL is set.
   Copy `app.py` over theirs (or `git apply indradhanu-webhook.patch`), push, and
   set on the map-patch-finder Render service:

   | Key | Value |
   |---|---|
   | `INDRADHANU_WEBHOOK_URL` | `https://disruptionops.onrender.com/api/v1/drone/localized` |
   | `INDRADHANU_WEBHOOK_KEY` | same value as `MESH_GATEWAY_KEY` on our API |
   | `DRONE_ID` | optional, e.g. `drone-1` |

2. **Our console → their API.** Live feed → "Upload a frame" sends the image to
   our API (`POST /api/v1/drone/localize`), which forwards it to the finder and
   records the answer. Set `MAP_FINDER_URL` on our API only if the finder moves.

Each result is written to the event log (`drone.localized`), shown in the Live
feed under "Drones" and narrated in the console. The same result arriving both
ways is recorded once (by `request_id`).
