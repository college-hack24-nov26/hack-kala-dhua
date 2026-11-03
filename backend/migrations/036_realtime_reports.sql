-- 036: faster console, and a change log for every resource site.
--
-- 1. Push new reports and assignment changes to the console the moment they
--    are written, instead of waiting for the next poll. Realtime applies the
--    same row-level security as a query: staff receive every report and
--    assignment; a resident only their own reports; a crew nothing from
--    `assignments` (their tasks arrive via `field_tasks`).
-- 2. lifeline_log: every change to a shelter, relief centre, kitchen, water
--    point or hospital (occupancy, stock, status, capacity), whichever code path
--    made it, written by a trigger - viewable and exportable per site
--    (GET /api/v1/units/sites/{id}/log, CSV or JSON).
do $$
begin
  if not exists (select 1 from pg_publication_tables where pubname = 'supabase_realtime' and tablename = 'citizen_reports') then
    alter publication supabase_realtime add table public.citizen_reports;
  end if;
  if not exists (select 1 from pg_publication_tables where pubname = 'supabase_realtime' and tablename = 'assignments') then
    alter publication supabase_realtime add table public.assignments;
  end if;
end $$;

begin;

create table if not exists public.lifeline_log (
  id            bigint generated always as identity primary key,
  lifeline_id   text not null,
  changed_at    timestamptz not null default now(),
  status_from   text,
  status_to     text,
  capacity_from integer,
  capacity_to   integer,
  occupancy_from integer,
  occupancy_to  integer,
  supplies_from jsonb,
  supplies_to   jsonb
);
create index if not exists lifeline_log_site_idx on public.lifeline_log (lifeline_id, changed_at desc);
alter table public.lifeline_log enable row level security;
revoke all on public.lifeline_log from anon, authenticated;

create or replace function public.log_lifeline_change()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  if new.status is distinct from old.status or new.capacity is distinct from old.capacity
     or new.occupancy is distinct from old.occupancy or new.supplies is distinct from old.supplies then
    insert into lifeline_log (lifeline_id, status_from, status_to, capacity_from, capacity_to,
                              occupancy_from, occupancy_to, supplies_from, supplies_to)
    values (new.id, old.status, new.status, old.capacity, new.capacity, old.occupancy, new.occupancy,
            case when new.supplies is distinct from old.supplies then old.supplies end,
            case when new.supplies is distinct from old.supplies then new.supplies end);
  end if;
  return new;
end $$;
revoke all on function public.log_lifeline_change() from public, anon, authenticated;

drop trigger if exists lifelines_log on public.lifelines;
create trigger lifelines_log after update on public.lifelines
  for each row execute function public.log_lifeline_change();

commit;
