-- Indexes for the paths the console hits once a second and the planner hits on
-- every re-plan. Nothing here changes behaviour; all of it changes whether the
-- behaviour arrives in time.
--
-- What was actually wrong:
--
--   * `replan` recounts committed units per open incident inside the same
--     transaction that has just rewritten assignments. Without an index on
--     (incident_id, status) that is a sequential scan per need row, holding row
--     locks the demo tick loop then waits on, and the statement times out.
--   * `/demo/state` looks up "the current assignment for this unit" once per
--     unit, twenty-three times per poll.
--   * ward risk and the event feed are both "latest rows first" reads on every
--     poll and in the citizen guidance query.
--
-- Safe to run more than once, and safe to run against a live database: every
-- one of these is `if not exists`.

create index if not exists assignments_incident_status_idx
  on public.assignments (incident_id, status);

create index if not exists assignments_resource_status_created_idx
  on public.assignments (resource_id, status, created_at desc);

create index if not exists ward_risks_ward_created_idx
  on public.ward_risks (ward_id, created_at desc);

create index if not exists events_id_desc_idx
  on public.events (id desc);

create index if not exists incidents_city_status_idx
  on public.incidents (city_id, status);
