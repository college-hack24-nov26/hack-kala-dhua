-- City-scoped row level security. Applied to the live database.
-- Public data (incidents, alerts, ward_risks) is deliberately not scoped this
-- way: an anonymous resident has no profile and so no city, and scoping those
-- would lock every citizen out of the public map. What this protects is
-- staff-only operational data, which is the boundary a municipality cares about.
alter table public.profiles
  add column if not exists city_id text not null default 'pune'
  references public.cities(id);

create or replace function app.my_city()
returns text language sql stable security definer
set search_path = public, pg_temp
as $$ select city_id from public.profiles where id = auth.uid() $$;

revoke all on function app.my_city() from public;
grant execute on function app.my_city() to authenticated;

drop policy if exists resources_staff_read on public.resources;
create policy resources_staff_read on public.resources
  for select to authenticated
  using (app.is_staff() and city_id = app.my_city());

drop policy if exists agency_requests_staff on public.agency_requests;
create policy agency_requests_staff on public.agency_requests
  for select to authenticated
  using (app.is_staff() and city_id = app.my_city());

drop policy if exists events_read on public.events;
create policy events_read on public.events
  for select to authenticated
  using (city_id = app.my_city());
