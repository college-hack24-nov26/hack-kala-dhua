# Operations, "do this instead", Copilot memory, and scenarios

## Seeing what is going on

- **Who is on what** (`/admin/dispatch`): every committed unit, its job, status,
  % driven, ETA, who assigned it and why. New: `pinned` and `re-routed ×N`
  badges, a *Cancel…* button per unit, and a **Standing orders** card.
- **Copilot**: "who is on what right now" gives the same table plus the
  standing orders.
- **Incident Commander** panel (Copilot, left column): what woke it, each
  thought and tool call, and how it ended.
- API: `GET /api/v1/ops`.

## Cancelling, and saying what to do instead

| Instead | What happens | Standing order written |
|---|---|---|
| `replan` (default) | unit freed; the next solve sends whoever is best | **forbid** this unit ↔ that incident, 45 min |
| `redirect` | unit routed (round blocks) to the chosen incident; crew task written | forbid (old job) + **pin** (new job), 60 min |
| `stage` | unit drives to the ward centre and becomes available there | forbid; optional **hold** |
| `hold` | unit stays where it is, out of the plan | forbid + hold, N minutes |
| `return_to_base` | unit drives to its `base_location` | forbid + hold 20 min |

Every cancel:

1. goes through the policy gate as `cancel_assignment` (clause **pol-2**, the
   same one that governs moving a municipal unit);
2. closes the crew's task (`field_tasks.status = cancelled`) with the reason;
3. writes `assignment.cancelled` with who, why and what instead, which the mesh
   outbox turns into an IDX1 C packet for crews offline;
4. is remembered in words (`agent_memory`, `standing_order`);
5. triggers a re-plan, through the demo runner's lock when it is running.

The planner reads `operator_overrides` on every solve: pinned and on-scene
units are locked, held units are out of the pool, forbidden pairs are priced
out of reach. Overrides lapse on their own; *Lift* ends one early.

Preview before committing: `POST /api/v1/ops/preview` re-solves the real plan
on a copy and lists the useful alternatives (open incidents with an unmet need
this unit's kind can serve, by severity then distance). The dialog and the
Copilot both show it.

### From the Copilot

```
cancel Pump 3 and send it to the Baner flooding
stop Ambulance 4, hold it for 20 minutes because the crew is exhausted
call off Boat 2 and stage it in Kothrud
recall Tender 1 back to base
cancel it                       ← "it" = the unit the last answer was about
```

The answer shows the unit's current job, the re-solved comparison, and option
cards (what you asked for, the two most useful alternatives, and hold 30 min),
each with *Put to the policy gate*.

## Memory (Supabase, migration 020)

| Table | Holds | Used for |
|---|---|---|
| `copilot_turns` | question, answer, intent, the ids the answer was about, per browser session | follow-ups: "it", "the second one", "same for Baner" |
| `agent_memory` | `standing_order`, `fact`, `lesson`, `episode`, `preference`; importance, expiry, soft delete, full-text index; `embedding vector(768)` ready | recalled into the Copilot router and narrator, and into each Commander episode |
| `recall_memory()` | SQL ranking: text match × 4 + importance × 0.3 + ward match + standing orders always + recency | one ranking for every caller |

The rule that keeps it safe: **memory is words, never numbers.** Every figure
in an answer comes from a tool call made at answer time. Memory is best-effort;
if the tables are missing the Copilot answers as before and says
`memory: unavailable`.

Say: "remember that the Aundh underpass floods first", "remember: keep Boat 2
in Kothrud until 6am" (a standing order, offered as a gated action to make it
binding), "what do you remember", "forget the Boat 2 instruction".
API: `GET/POST /api/v1/copilot/memory`, `DELETE /api/v1/copilot/memory/{id}`.

## Scenarios

`POST /api/v1/demo/scenarios/{name}/run`, then watch the console beats or
`GET /api/v1/demo/scenarios/last` for PASS/FAIL checks.

| Name | Shows | Checks |
|---|---|---|
| `flood_cascade` | five reports of one road | ≤2 incidents; covered without duplicate dispatch |
| `block_on_approach` | closure on the road a unit is driving | re-routed at most once over two re-plans; kept its job |
| `unit_breakdown` | unit fails mid-route | another unit took the job |
| `officer_redirect` | cancel + send elsewhere | still there after three re-plans; not sent back |
| `officer_hold` | cancel + hold | not tasked while held |
| `camera_fire_mesh` | camera fire in a ward with no internet | packet → report; something queued back to the mesh |
| `mesh_blackout_sos` | two reports over the mesh | both arrive; second gateway = duplicate; tampered = refused (with HMAC key) |
| `multi_hazard_surge` | five hazards at once | plan runs; shortfall recorded |
