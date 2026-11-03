# Offline: online app first, bitchat mesh when there is no signal

## The design in one paragraph

With internet, nothing changes: the citizen app, the field app and the console
use the deployed API, with live routing, re-routing and maps. Without internet,
phones fall back to **bitchat**, which carries short text packets phone to phone
over Bluetooth. A packet reaches the deployed API as soon as **any** phone in
the chain (or a laptop beside one) has signal, and the control room's alerts,
dispatches and road closures travel back the same way. Every packet carries a
location, so each phone shows only what concerns where it is.

## Paths

```mermaid
sequenceDiagram
  participant P as Citizen phone (no signal)
  participant B as bitchat on that phone :8765
  participant M as Other phones (BLE, ≤7 hops)
  participant G as Gateway phone / laptop bridge (has signal)
  participant A as Indradhanu API (Render)
  P->>B: POST /send/text  "IDX1|R|{lat,lon,text}"
  B->>M: mesh broadcast
  M->>G: relayed
  G->>A: POST /api/v1/mesh/inbound  (X-Mesh-Gateway-Key)
  A->>A: verify · dedup by packet id · intake.receive(source=mesh) · re-plan
  A-->>G: GET /api/v1/mesh/outbox  → alerts, dispatches, closures
  G->>M: POST /send/text into the mesh
  M->>B: delivered
  P->>B: GET /inbox?since=N  → filter by distance, show
```

## Packet: IDX1

`<human text> IDX1|<type>|<compact json>|<hmac16 or ->`. Human text first so a
person reading bitchat understands it; the rest for programs. Full spec in
`backend/app/mesh/envelope.py`.

| Type | Direction | Body keys |
|---|---|---|
| R report | in | id, n (device), k (category, optional), la, lo, x (text), t |
| S sensor | in | id, n (camera), k (fire/smoke/fall/…), c (confidence), f (frames), v (VLM agreed), la, lo, x (caption), t |
| F field status | in | id, u (unit), k (route_blocked/on_site/…), la, lo, x |
| H heartbeat, K ack | in | id, n / i (outbox id) |
| A alert | out | id, w (ward), la, lo, r (radius m), s (severity), x (instruction) |
| D dispatch / re-route | out | id, u, i, la, lo, m (ETA), x |
| C cancel | out | id, u, k (instead), x (reason) |
| B road closed | out | id, la, lo, r (150), x |

Signed with `MESH_HMAC_KEY` by the camera node and the bridge. Citizen packets
from the web app are unsigned (a key in a public web page is not a secret) and
are scored as `mesh_unsigned` (0.45 credibility), so they can open an incident
but never auto-dispatch on their own.

## How each existing feature behaves

| Feature | Online | Offline (mesh) |
|---|---|---|
| Citizen report | `/citizen/report`, photo, voice | text + GPS as IDX1 R; no photo, no voice |
| Where to go | live route from the guidance agent, re-routed round blocks | route saved while online, checked against closures heard on the mesh; else compass heading + distance to nearest cached shelter, labelled as a heading |
| Alerts | from the API, per ward | IDX1 A from the mesh, filtered by distance to this phone |
| Road closures | map layer | IDX1 B list |
| Crew dispatch | field app | IDX1 D / C in bitchat `#field` |
| Crew "road blocked" | field app button | IDX1 F (from the bridge; field-app mesh mode is a next step) |
| Map tiles | Mapbox | last cached state; tiles only if the browser cached them |
| Re-plan, CP-SAT, gate | cloud | still cloud: runs when packets arrive |
| Camera node | `/ingest/sensor` over HTTPS | IDX1 S via the bitchat phone on its Wi-Fi |

## The bitchat phone app (teammate's build + command-centre link)

The field app is `kurukshetra_hackathon/src/bitchat-android`: roles (Civilian, Ambulance,
Fire, Gov, Command) on every announce and broadcast, a shelter registry gossiped over
the mesh, SOS with GPS, an osmdroid map, the VLM API, and the link to this API
(`vlm/IndradhanuGateway.kt`, configured in *About → Command Centre*). While a phone has
validated internet it:

| Direction | Endpoint | What |
|---|---|---|
| push | `POST /api/v1/mesh/inbound` | IDX1 packets heard on the mesh (store-and-forward queue) |
| push | `POST /api/v1/mesh/civ-sync` | `bitchat.civ.sync/v1` bundle: SOS, geotagged posts, VLM briefs, shelters. Each incident becomes an R packet (an S packet for a VLM fire/smoke/flood brief), signed with `MESH_HMAC_KEY`, and goes through `receive()` like any mesh packet, so it is deduplicated with the IDX1 path by id |
| listen | `GET /api/v1/mesh/outbox` + `/ack` | alerts, dispatches, cancellations, blocks, broadcast on the mesh tagged **Command** with a `geo:` tag so every phone pins them on its map |
| cache | `GET /api/v1/lifelines`, `/shelters` | shelters, relief centres, hospitals, medical camps, food and water points into the phone's shelter registry (persisted, gossiped to the mesh), plus walking routes to the nearest few for offline navigation |

## Setting it up

1. **API** (Render): set `MESH_GATEWAY_KEY` and `MESH_HMAC_KEY` (long random
   strings). Migration 022 is already applied.
2. **bitchat fork** (`bitchat-android`): build in Android Studio, install, turn
   on Settings → VLM API. On phones that will have signal, enable gateway mode:
   `POST http://<phone>:8765/gateway {"enabled":true,"api_url":"https://…","gateway_key":"…"}`.
   See `docs/INDRADHANU_GATEWAY.md` in that repo.
3. **Or a laptop bridge** (no app rebuild, phone has no data but the laptop
   does): `python backend/scripts/mesh_bridge.py --api https://… --key … --phone <phone-ip>`.
4. **Stage demo without phones:**
   `python backend/scripts/mesh_bridge.py sim --api … --key … --hmac … --report flooded_road 18.52 73.85 "Water knee deep"`,
   or `POST /api/v1/demo/scenarios/mesh_blackout_sos/run`.
5. **Camera node** (`ai-surveillance/configs/pipeline.yaml`): set
   `indradhanu.enabled: true`, `api`, `gateway_key`, `hmac_key`, `node_id`,
   `lat`/`lon`; keep `disaster_mode: true`.
6. **Phone browser**: the first time mesh mode calls bitchat, Chrome asks to
   allow access to devices on the local network. Allow it.

## LoRa link between two meshes (radio-only build)

Bluetooth covers tens of metres per hop. Two Unos with SX1278 radios join two
mesh areas, or an offline area and the command centre, over hundreds of
metres to kilometres, carrying the same IDX1 packets unchanged.

- Firmware: `hardware/lora/modem/modem.ino`, the same on both Unos. Radio only:
  7 jumpers, 10k in series on NSS/SCK/MOSI, RST unconnected, SF7 by default.
- Laptop script: `hardware/lora/lora_mesh_link.py`, one per Uno.
  - `--phone <ip>`: a bitchat phone with the VLM API on.
    Inbox → LoRa; LoRa → `/send/text`.
  - `--api … --key …`: LoRa and phone packets → `/mesh/inbound`;
    `/mesh/outbox` → LoRa and phone; acked.
  - Typing `sos <text>` (with `--lat/--lon/--hmac`) sends a signed R packet.
- Messages over 232 bytes are split into frames and reassembled. Each
  message is sent twice, and everything is de-duplicated by content, so
  nothing loops.

```
python lora_mesh_link.py --name field-A --phone 192.168.43.20 --lat 18.52 --lon 73.85 --hmac $MESH_HMAC_KEY
python lora_mesh_link.py --name hq-B --api https://disruptionops.onrender.com --key $MESH_GATEWAY_KEY
```

## Limits

- Bluetooth mesh: tens of metres per hop, 7 hops (TTL), needs phones in range.
  A last-mile path, not a city network. LoRa (e.g. Meshtastic) is the answer for
  kilometres, and fits this packet format unchanged.
- Latency: seconds to minutes, depending on when a gateway gets signal.
- Text only. Images stay on the phone until it is online.
- iPhones: the fork is Android. Upstream bitchat on iOS relays the same mesh
  but has no local API, so an iPhone can relay packets but cannot run mesh mode.
- `/inbox` on the phone is open while the VLM API is on; it only returns what
  was already broadcast in the clear on the mesh.
