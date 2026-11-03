# DisruptionOps — System Architecture (to draw)

> Updated 26 Sep 2026: the agents, tools, Incident Commander, memory, operations
> control and the two-way offline mesh are described in
> [`AGENTIC_ARCHITECTURE.md`](AGENTIC_ARCHITECTURE.md) and
> [`OFFLINE_MESH.md`](OFFLINE_MESH.md). The bands below are still correct; add a
> mesh gateway box to intake and an Incident Commander box beside the decide band.

PS20: *multi-agent disaster-response coordination — ingest reports from affected
zones, model resource availability and needs, allocate and re-allocate as
conditions change, coordinate across agencies.*

Draw it as **six horizontal bands**, top to bottom, with one arrow down between
each. Every band is a row of boxes.

---

## BAND 1 — INTAKE  ·  "reports arrive, from anywhere"

Five boxes in a row, all feeding one box below them:

| Box | Label |
|---|---|
| 1 | Citizen app — text, voice or photo, EN / HI / MR |
| 2 | Field crew app |
| 3 | Partner agency API |
| 4 | Sensor & gauge feed |
| 5 | Simulation / replay |

All six arrow into a single wide box: **`intake.receive()` — one ingest path**

> Caption to put under it: *Five channels, one door. Every report is normalised
> here, so nothing downstream needs to know where it came from.*

---

## BAND 2 — UNDERSTAND THE REPORT

Three boxes, left to right, chained with arrows:

1. **Parser** — 3 tiers, cheapest first
   - keyword match → XLM-RoBERTa zero-shot (100 languages) → LLM last resort
   - *outputs: category, from the taxonomy only — it cannot invent one*
2. **Trust scorer** — 6 named components, deterministic
   - reporter history · channel · corroboration · specificity · timing · photo
   - *outputs: 0–1 score + the reason for each component*
3. **Duplicate clusterer** — distance + time + category + text
   - *ten calls about one street become one incident*

Side box hanging off the trust scorer:
**Vision service (Qwen2.5-VL)** — a photo may raise or lower trust, and may
never set category, severity or dispatch.

---

## BAND 3 — UNDERSTAND THE ZONE

Two boxes side by side, both reading from PostGIS:

- **Needs assessment** — incident category → required capability × quantity
  (`incident_category_needs`), summed per ward
- **Severity & priority scoring** — category base severity, lifted by
  independent corroboration, capped at 5; × exposed population

Below them, one wide box: **Ward state** — for each of N zones:
`severity 0–5 · people exposed · capability required · capability met · shortfall`

> This band is the "model resource availability and needs" requirement.

---

## BAND 4 — DECIDE  (the gate)

Two boxes side by side:

- **Policy / delegation matrix** — NDMA SOPs, municipal DM plan, delegation of
  authority. Exact match on `action_key`, not fuzzy similarity.
- **Decision gate** —
  - inside cited authority **→ auto-issue**, clause shown
  - outside authority, or low confidence **→ officer approval queue**

> Caption: *No action issues because a model was confident. It issues because a
> clause delegates it.*

---

## BAND 5 — ALLOCATE  (the solver)

One wide box containing three inner boxes left→right:

1. **Travel-time matrix** — Mapbox Directions over real roads, OSRM fallback;
   **blocked roads removed from the graph** as crews report them
2. **OR-Tools CP-SAT** — assign units to incidents under
   `capability match · capacity · travel time · one unit per job`
3. **Re-plan with switching cost** — a committed crew is only turned around when
   the gain beats the disruption

Two feedback arrows curving back up into this band:

- **new urgent report** (from Band 1) → re-plan
- **unit status change** — puncture, full, road blocked (from Band 6) → re-plan

Side box: **Duplicate-effort detector** — two agencies heading to one incident is
flagged before both arrive.

Side box: **Forecast-driven prepositioning** — Gamma-Poisson recurrence per ward
projects which capability runs short; spare units are *proposed* toward the
likeliest zone through the same gate.

---

## BAND 6 — ACT  ·  three interfaces

Three boxes in a row:

| Interface | Who | What they get |
|---|---|---|
| **Citizen PWA** | residents | where to go, which road to avoid, by when — voice, 3 languages, works offline |
| **Command console** | ward officer / commissioner | risk board, incident queue, allocation planner, decision gate, agent trace |
| **Field crew app** | fire, ambulance, NDRF, drainage | accept → on site → close, with proof |

Plus one box to the side: **Inter-agency handoff** — request what you do not
have; accept or decline; recorded both sides.

---

## THE SPINE (draw as a vertical bar down the right edge, touching every band)

**Append-only event log (Postgres)**

- every step writes an event carrying `causation_id` — the id of the event that
  caused it
- a database trigger **refuses DELETE and UPDATE**
- so "why did that boat go there?" is answered by walking a chain, not guessing

Label the bar: *Audit log — enforced by the database, not by convention.*

---

## THE FOUNDATION (draw as a bar across the bottom)

**Supabase Postgres + PostGIS** — wards, resources, incidents, assignments,
lifelines, events. Row-level security scopes staff reads by city.
**Taxonomy is rows, not enums** — hazards, categories, capabilities, resource
kinds. A new hazard or a new city is configuration.

---

## Arrows to get right

- Band 1 → 2 → 3 → 4 → 5 → 6, straight down.
- **Two feedback arrows** from Band 6 back up to Band 5 (new report, status
  change). These are the "dynamic re-allocation" requirement and the most
  important arrows on the page — draw them thicker.
- The event-log spine touches every band with a small arrow *into* it.

## One-line summary to put at the bottom

> Report → understand → score the zone → check authority → solve → dispatch →
> and the moment anything changes, solve again.
