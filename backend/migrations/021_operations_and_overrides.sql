-- 021 — cancelling an operation, and making the planner respect a person.
--
-- The gap: an officer could move a unit (the Copilot's `reallocate_unit`), and
-- six seconds later the re-planner, which had never been told, priced that unit
-- as free, found a "better" job for it and sent it there. The order was undone
-- by arithmetic and nobody was told. There was also no way to *stop* something:
-- no cancel, no "hold that unit here", no "not that one, send the other".
--
-- `operator_overrides` is the instruction, in a form the planner reads on every
-- solve:
--
--   pin     keep this unit on its current job; the planner may not move it
--   hold    this unit is out of the plan until expiry (resting, reserved)
--   forbid  this unit must not be sent to this incident again
--
-- Every override names who set it, why, the decision that authorised it, and
-- when it lapses. They expire on their own, because a "hold" nobody remembers
-- setting is how a city ends up with a pump idle through the worst hour.

-- Stood-down tasks were being marked `complete`, which counted them as done
-- work. A value of its own. Outside the transaction: a new enum value cannot
-- be used in the transaction that adds it.
alter type public.task_status add value if not exists 'cancelled';
-- Found while writing this: `assignment_status` never had `cancelled` either,
-- and `copilot/execute.py` has been writing it since the Copilot pass. It had
-- simply never run (no `reallocate_unit` decision exists in the database), so
-- the first commissioner move in a demo would have been a 500.
alter type public.assignment_status add value if not exists 'cancelled';

begin;

create table if not exists public.operator_overrides (
  id           uuid        primary key default gen_random_uuid(),
  city_id      text        not null default 'pune',
  kind         text        not null check (kind in ('pin', 'hold', 'forbid')),
  resource_id  text        not null references public.resources(id) on delete cascade,
  incident_id  uuid        references public.incidents(id) on delete cascade,
  reason       text        not null check (length(reason) between 3 and 600),
  created_by   text        not null,
  decision_id  uuid,
  created_at   timestamptz not null default now(),
  expires_at   timestamptz,
  active       boolean     not null default true,
  lifted_by    text,
  lifted_at    timestamptz,
  constraint forbid_needs_incident check (kind <> 'forbid' or incident_id is not null)
);

create index if not exists operator_overrides_live_idx
  on public.operator_overrides (city_id, active, expires_at);
create index if not exists operator_overrides_resource_idx
  on public.operator_overrides (resource_id) where active;

alter table public.operator_overrides enable row level security;
-- Read through the API only, like every other table since 014.
revoke all on public.operator_overrides from anon, authenticated;

-- Cancelling a municipal unit's job, holding it, or pinning it is the same
-- delegation as moving it (pol-2 already governs reallocate_unit), so it goes
-- under the same clause rather than a new one of our own invention.
update public.policy_clauses
   set authorises = array(select distinct unnest(
         authorises || array['cancel_assignment', 'hold_unit', 'pin_unit']))
 where id = 'pol-2';

commit;
