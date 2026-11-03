-- 032: units that move, logs per unit, and teams of different units on one incident.
--
--   unit_positions        every position a unit reports (field app GPS, the
--                         simulator's movement), with how far off its route it
--                         was; the trail on the map and the unit's log.
--   assignments.capability_id
--                         which need of the incident this unit covers (fire
--                         suppression, medical transport, traffic control...),
--                         so "needs met" is counted per capability, not per
--                         incident (a fire with two units no longer shows every
--                         need as met twice).
--   police + traffic_control
--                         a police unit kind with traffic control and field
--                         assessment; fires and collapses need a cordon, so a
--                         fire now gets a fire tender, an ambulance and police.
--   PCMC police units     four, at Pimpri Chinchwad police stations (OSM).
--
-- Additive. RLS on the new table; writes are server-side.

begin;

create table if not exists public.unit_positions (
  id            bigint generated always as identity primary key,
  city_id       text not null default 'pune' references public.cities(id),
  sim_run_id    uuid,
  resource_id   text not null references public.resources(id) on delete cascade,
  recorded_at   timestamptz not null default now(),
  location      extensions.geography(Point, 4326) not null,
  speed_kmh     real,
  heading_deg   real,
  accuracy_m    real,
  source        text not null default 'gps' check (source in ('gps', 'sim', 'manual')),
  assignment_id uuid references public.assignments(id) on delete set null,
  off_route_m   real,
  progress      real
);
create index if not exists unit_positions_unit_time_idx on public.unit_positions (resource_id, recorded_at desc);
alter table public.unit_positions enable row level security;
revoke all on public.unit_positions from anon, authenticated;

alter table public.assignments add column if not exists capability_id text references public.capabilities(id);

insert into capabilities (id, label, description) values
  ('traffic_control', 'Traffic control', 'Cordons, diversions and keeping a scene reachable for the other units')
on conflict (id) do nothing;

insert into resource_kinds (id, display_name, default_capacity) values ('police', 'Police vehicle', 4)
on conflict (id) do nothing;

insert into resource_kind_capabilities (kind_id, capability_id, effectiveness) values
  ('police', 'traffic_control', 1.0),
  ('police', 'field_assessment', 0.8)
on conflict do nothing;

-- A fire needs a cordon as well as a tender and an ambulance; so does a collapse.
insert into incident_category_needs (category_id, capability_id, qty_per_incident) values
  ('fire', 'traffic_control', 1),
  ('structural_damage', 'traffic_control', 1)
on conflict do nothing;

insert into agencies (id, city_id, name, short_name, kind, jurisdiction, contact, active)
values ('pcpolice', 'pune', 'Pimpri Chinchwad Police', 'PCPC', 'government', 'city', '{}', true)
on conflict (id) do nothing;

insert into agency_capabilities (agency_id, capability_id) values ('pcpolice', 'traffic_control'),
  ('pcpolice', 'field_assessment')
on conflict do nothing;

insert into resources (id, kind, label, operator, base_location, location, capacity, status,
                       city_id, agency_id, crew_available, fuel_pct, last_reported_at, updated_at)
select id, 'police', label, 'Pimpri Chinchwad Police',
       extensions.ST_SetSRID(extensions.ST_MakePoint(lng, lat), 4326)::extensions.geography,
       extensions.ST_SetSRID(extensions.ST_MakePoint(lng, lat), 4326)::extensions.geography,
       4, 'available', 'pune', 'pcpolice', true, 90, now(), now()
  from (values
    ('PC-POL-01', 'Police 01 (Pimpri police station)',    73.8016, 18.6262),
    ('PC-POL-02', 'Police 02 (Chinchwad police station)', 73.7856, 18.6339),
    ('PC-POL-03', 'Police 03 (Nigdi police station)',     73.7715, 18.6542),
    ('PC-POL-04', 'Police 04 (Wakad police station)',     73.7639, 18.5989)
  ) as v(id, label, lng, lat)
on conflict (id) do nothing;

commit;
