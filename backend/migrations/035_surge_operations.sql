-- 035: the rest of the surge plan - evacuation, transport, shelter options,
-- crews, one public channel, recovery.
--
--   incident categories  evacuation (a ward's people moved to shelter by bus
--                        convoy) and traffic_corridor (police keep a road clear
--                        for ambulances and relief trucks)
--   helicopter           a resource kind (air: straight line at cruise speed,
--                        grounded above its rain limit) offered by the IAF and
--                        the state government at level 4
--   hotel, host_family   lifeline kinds for requisitioned rooms and registered
--                        host families; planning pools, closed until opened
--   surge_actions        every surge step the ladder takes: evacuation zones,
--                        convoys and their fixed routes, priority corridors,
--                        shelter-in-place advisories, transfers to shelters in
--                        unaffected areas, crew rests, demobilisation, recovery
--   resource_shifts      how long each crew has been on duty and when it rests
--   public_notices       the single public channel: every instruction the
--                        public gets, in order, with what it supersedes
--
-- Additive. RLS on; the API reads and writes server-side. Capacities and
-- response times of the planning pools are assumptions for the drill.

begin;

insert into incident_categories (id, display_name, hazard_id, dedup_radius_m, dedup_window_min, base_severity, life_safety)
values ('evacuation', 'Evacuation (convoy to shelter)', 'flood', 50, 600, 4, true),
       ('traffic_corridor', 'Priority corridor (keep road clear)', 'flood', 50, 600, 3, false)
on conflict (id) do nothing;

insert into resource_kinds (id, display_name, default_capacity) values ('helicopter', 'Helicopter', 6)
on conflict (id) do nothing;
insert into resource_kind_capabilities (kind_id, capability_id, effectiveness) values
  ('helicopter', 'water_rescue', 0.9), ('helicopter', 'search_rescue', 0.8),
  ('helicopter', 'medical_transport', 0.7), ('helicopter', 'supply_delivery', 0.5)
on conflict do nothing;

insert into agencies (id, city_id, name, short_name, kind, jurisdiction, contact, active) values
  ('iaf', 'pune', 'Indian Air Force (HADR, via state request)', 'IAF', 'government', 'national', '{}', true),
  ('stateaviation', 'pune', 'Maharashtra Government aviation (state helicopter)', 'State heli', 'government', 'state', '{}', true),
  ('hoteliers', 'pune', 'Hotel and lodge owners (rooms requisitioned by the Collector)', 'Hotels', 'private', 'district', '{}', true)
on conflict (id) do nothing;

insert into aid_offers (id, region, agency_id, kind, quantity, response_minutes, accept_p, min_level, stage_lng, stage_lat, label) values
  ('pun-state-heli', 'pune', 'stateaviation', 'helicopter', 1, 90, 0.8, 4, 73.9197, 18.5821, 'State government helicopter (Lohegaon)'),
  ('pun-iaf-heli',   'pune', 'iaf',           'helicopter', 2, 150, 0.9, 4, 73.9197, 18.5821, 'IAF helicopters (Lohegaon AFS)')
on conflict (id) do nothing;

insert into lifeline_kinds (id, display_name, shelters_people, serves_public) values
  ('hotel', 'Hotel / lodge rooms (requisitioned)', true, true),
  ('host_family', 'Host families (registered pool)', true, true)
on conflict (id) do nothing;

-- Planning pools, closed until the ladder opens them. Names describe the pool,
-- not a business; capacities are assumptions to be replaced by the Collector's
-- requisition list and the volunteer registry.
insert into lifelines (id, kind, name, ward_id, location, capacity, occupancy, city_id, accepts_casualties,
                       status, specialities, supplies, supplies_baseline, people_served_per_hour,
                       occupancy_baseline, last_reported_at)
select v.id, v.kind, v.name,
       (select w.id from wards w where w.id like 'w-pc-%'
         order by extensions.ST_Distance(w.centroid,
                  extensions.ST_SetSRID(extensions.ST_MakePoint(v.lng, v.lat), 4326)::extensions.geography) limit 1),
       extensions.ST_SetSRID(extensions.ST_MakePoint(v.lng, v.lat), 4326)::extensions.geography,
       v.cap, 0, 'pune', false, 'closed', '{}', '{}'::jsonb, '{}'::jsonb, null, 0, now()
  from (values
    ('lf-pc-ht1', 'hotel', 'Hotel and lodge rooms, Pimpri station road (requisition pool)', 73.8007, 18.6279, 150),
    ('lf-pc-ht2', 'hotel', 'Hotel and lodge rooms, Wakad-Hinjewadi road (requisition pool)', 73.7627, 18.5994, 200),
    ('lf-pc-ht3', 'hotel', 'Hotel and lodge rooms, Nigdi-Akurdi (requisition pool)', 73.7681, 18.6526, 120),
    ('lf-pc-hf1', 'host_family', 'Registered host families, Pradhikaran-Nigdi', 73.7700, 18.6600, 80),
    ('lf-pc-hf2', 'host_family', 'Registered host families, Thergaon-Wakad', 73.7700, 18.6060, 60),
    ('lf-pc-hf3', 'host_family', 'Registered host families, Chikhli-Talawade', 73.8050, 18.6800, 60)
  ) as v(id, kind, name, lng, lat, cap)
on conflict (id) do nothing;

create table if not exists public.surge_actions (
  id          bigint generated always as identity primary key,
  region      text not null check (region in ('pune', 'ncr')),
  kind        text not null check (kind in ('evacuation_zone', 'convoy', 'priority_corridor', 'shelter_in_place',
                                            'transfer', 'shelter_opened', 'crew_rest', 'volunteers',
                                            'demobilise', 'recovery', 'escalation')),
  status      text not null default 'active' check (status in ('active', 'done', 'cancelled')),
  title       text not null,
  detail      jsonb not null default '{}',
  ward_id     text,
  ref         text,
  created_at  timestamptz not null default now(),
  closed_at   timestamptz
);
create index if not exists surge_actions_region_idx on public.surge_actions (region, status, kind);

create table if not exists public.resource_shifts (
  resource_id    text primary key references public.resources(id) on delete cascade,
  on_duty_since  timestamptz,
  rest_until     timestamptz,
  shifts         integer not null default 0,
  updated_at     timestamptz not null default now()
);

create table if not exists public.public_notices (
  id             bigint generated always as identity primary key,
  region         text not null check (region in ('pune', 'ncr')),
  level          smallint not null default 0,
  kind           text not null,
  headline       text not null,
  body           text not null,
  ward_ids       text[] not null default '{}',
  channels       text[] not null default '{app,sms,mesh,radio}',
  ref            text,
  issued_at      timestamptz not null default now(),
  superseded_at  timestamptz
);
create index if not exists public_notices_region_idx on public.public_notices (region, superseded_at, issued_at desc);

alter table public.surge_actions enable row level security;
alter table public.resource_shifts enable row level security;
alter table public.public_notices enable row level security;
revoke all on public.surge_actions, public.resource_shifts, public.public_notices from anon, authenticated;

commit;
