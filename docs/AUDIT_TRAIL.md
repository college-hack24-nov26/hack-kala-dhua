# Audit trail: every agent decision, and what it was based on

Console: **Intelligence → Audit trail** (`/admin/audit`). Each incident's evidence card also has an
**Audit trail** button, which opens the list filtered to that incident.

## What you see

The list shows every agent decision, newest first, grouped by day. You can filter it by:

- planner;
- belief;
- triage;
- satellite;
- drones;
- cameras;
- approvals;
- agencies.

There is also a text search. Click any row and a drawer opens with four tabs.

| Tab | Contents |
|---|---|
| **Evidence** | The actual media, each labelled with its sensor and processing level. See the media list below. Click any item to enlarge it. |
| **Basis** | For each incident the decision touched, see the basis list below. |
| **Chain** | The causal chain from the root event (usually the first report) to this decision, then what this decision caused. |
| **Raw** | The decision's recorded payload as JSON. |

The Evidence tab shows:

- edge-camera frames, with the VLM's words and the hazard it named;
- the 6-second **clip** the camera kept from just before the alarm;
- citizen and crew photos;
- drone frames;
- satellite images:
  - Sentinel-1 VV radar backscatter;
  - the GFM flood mask, flood likelihood and exclusion mask;
  - the pixel values read from each.

The Basis tab shows, for each incident:

- **Belief at the time of the decision and now:**
  - per-modality weights;
  - every witness and its weight;
  - the reasons;
  - the next step.
- **Satellite passes:**
  - acquisition time and age;
  - status (flooded / dry / not observable / no pass);
  - extracted values;
  - the scene ids.
- **Reports:** modality, reporter, text, trust and outcome.
- **Edge-camera frames within 600 m:** from 3 h before the decision to 30 min after.
- **Drone swarm:** events and map-matched positions.
- **LoRa sensor readings:** raw channels (`mq2`, `mq135`, `temp_c`, `tilt_deg`, `gyro_dps`, `vib_g`, `mic`, `piezo`, `pir` and the scores) from nodes within 600 m.
- **Units:** needs, the units assigned, ETA, route engine, route length, and the planner's stated reason for each unit.

## API (staff)

    GET /api/v1/audit/decisions?group=&incident_id=&q=&before=&limit=
    GET /api/v1/audit/decisions/{event_id}
    GET /api/v1/audit/media/{media_id}      raw bytes (image/* or video/*)
    GET /api/v1/audit/satellite?lat=&lng=   live GFM / Sentinel-1 reading at a point

The read-only demo key works for the two GET list/basis endpoints. To use it, send the `X-Demo-Read-Key` header.

The code is in `backend/app/audit/basis.py`. It reads only: the events log, incidents, reports, `feed_media`, `sensor_readings` and assignments.

## Where the material comes from

- **Camera frames and clips.** These come from `POST /ingest/camera` with `imageBase64` and `clipBase64`/`clipMime`. vlm-s keeps a rolling buffer of 320 px frames at about 4 fps (`core/command_centre.py`). When a description names a hazard, it encodes the last 6 s:
  - H.264 MP4 if the laptop has the codec;
  - otherwise VP8 WebM (plays in Chrome and Firefox);
  - `--no-clip` turns this off.
- **Satellite.** `app/eo/gfm.py` provides two sources, with no account needed:
  - Copernicus GFM through the EODC STAC and TiTiler;
  - Sentinel-1 RTC VV through Planetary Computer.

  Each check is written as `satellite.observed`, and its PNGs are stored in `feed_media`.
- **Migration.** Apply `migrations/041_evidence_media.sql` first. It lets `feed_media` hold video and satellite or sensor media up to 4 MB. Until it is applied:
  - clips and satellite PNGs are not stored;
  - satellite images then show from their live TiTiler URL instead.
