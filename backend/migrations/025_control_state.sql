-- 025 — control_state: small operational switches that must survive a restart.
--
-- First use: the emergency stop (app/ops/autonomy.py). key 'autonomy' =
-- {paused, by, reason, since}. While paused nothing issues or re-plans on its
-- own; a restart must not silently resume, so the flag lives here.

begin;

create table if not exists public.control_state (
  key         text primary key,
  value       jsonb not null,
  updated_at  timestamptz not null default now()
);
alter table public.control_state enable row level security;
revoke all on public.control_state from anon, authenticated;

commit;
