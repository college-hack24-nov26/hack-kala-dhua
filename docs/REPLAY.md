# Replay

`/admin/replay`. The run as it happened, at any point in it.

## What it is

`events` is append-only, so everything that happened is already recorded. What
did not exist was a way to stand at a moment and look around. Replay folds the
log back into the world it described: which incidents were open, which unit was
committed to what, which alerts were in force, what the last solve produced, and
what it could not cover — at any of 180 points across a session.

Beside the map runs the ticker, and the ticker is the half that carries the
argument. The most valuable text this system produces is written at the moment
the solver moves a unit:

> Moved from "Fallen tree — Warje–Malwadi" (6% of the way there) because
> "Structural damage — Sinhagad Road" is severity 4 with 8,900 exposed.

There are 958 of those in the log. Until now every one of them went into a jsonb
column and stayed there.

## What it is not

**It is not a comparison.** There is one recorded stream and the CP-SAT
allocator produced it. Showing what nearest-first would have done means
re-solving, which is `scripts/benchmark_strategies.py`. The earlier design note
claimed replay would let a judge "watch the same stream under nearest-first" —
it will not, and no amount of scrubbing changes that.

The claim replay supports is **auditability**: why did this unit move, what
caused it, who authorised this alert. For a municipal deployment that is
arguably the stronger claim anyway, and no other screen makes it.

## Three things the data forced

These are the corrections that shaped the implementation. Each one contradicts
something in the original design notes.

**There is no `world.reset` event.** The notes say reset writes one so a run has
a defined beginning and end. The kind has never been written — it does not
appear in `events` at all. Sessions are therefore derived from gaps in the
stream: a quiet stretch longer than 15 minutes ends one. Weaker, but honest, and
it works on data that already exists. On the current log it finds six sessions,
the longest 83 minutes and 1,358 events.

**The events carry no geometry.** `incident.opened` records category, severity
and needs — no coordinates. So the fold decides *which* things were open and
*who* was committed to what, and positions come from the entity tables. That is
correct anyway: an incident does not move.

**Unit tracks were never recorded.** There is no history of where a vehicle was
at 14:32. What exists is `progress_pct` on `assignment.changed`, so a unit's
replay position is interpolated from its base toward the incident it was
travelling to. This is a reconstruction, and the API says so in its own payload
(`positionsAre: "reconstructed"`) rather than only in the UI, so nothing else
consuming the endpoint can mistake an interpolation for a GPS trace.

## API

```
GET /api/v1/replay/sessions?cityId=pune
    -> [{ id, startedAt, endedAt, events, durationMinutes }]

GET /api/v1/replay/frames?from=&to=&steps=180&cityId=pune
    -> { incidents: {id: {...}},      # catalogue, sent once
         resources: {id: {...}},      # catalogue, sent once
         frames:   [{ at, eventId, open[], assigned{}, alerts[], plan, uncovered[] }],
         events:   [{ id, at, kind, text, causationId }],
         positionsAre, positionsNote }
```

Both are staff-only. The catalogue is sent once rather than inside all 180
frames — the difference between a 200 KB response and a 6 MB one.

## The fold

In `app/api/v1/replay.py`, server-side, because it needs to know what each event
kind *means* and that is domain knowledge.

| Kind | Effect on state |
|---|---|
| `incident.opened` | add to open set |
| `incident.resolved` | remove; free any unit committed to it; clear its gap |
| `assignment.created` | `subject_id` is the assignment, unit is `payload.resource_id` |
| `assignment.changed` | `subject_id` **is the unit**, both ends in the payload |
| `assignment.cancelled` | `subject_id` is the unit; drop it |
| `alert.issued` | append |
| `plan.generated` | replace the plan in force |
| `demand.uncovered` | record the gap against its incident |

The asymmetry between `assignment.created` (subject = assignment) and
`assignment.changed` (subject = resource) is the single easiest thing to get
wrong here, and nothing in the schema warns you about it.

`report.received` and the decision kinds are returned in the ticker but not
folded: they explain *why* something happened and move nothing on the map.

## Using it in a demo

Pick the longest session, not the newest — a 30-second session is a restart.
The screen defaults to the longest for that reason. Scrub to any
`assignment.changed` in the ticker (amber) and the map shows the world at the
moment the solver made that call, with the incident it chose and the one it gave
up both visible.
