-- 034: surge operations - what happens when units and shelters run out.
--
-- The escalation ladder (app/surge/service.py), per region (pune | ncr):
--   0 normal       routine planning
--   1 strained     fleet >= 80% busy, shelters >= 80% full, or needs unmet:
--                  triage (life-safety first, vulnerable wards next), rationing,
--                  stock redistributed from low-need centres
--   2 mutual aid   needs still unmet: requests to NGOs, neighbouring districts and
--                  SDRF; accepted aid arrives as real units after its response time
--   3 surge shelters  shelters >= 90% full or aid not enough: schools and halls
--                  open as shelters, evacuation staged by risk zone with buses
--   4 declaration  life-safety needs still unmet: state/national declaration,
--                  NDRF and the Army requested (an officer must approve)
--
--   surge_state    the level per region, why, since when, the metrics behind it
--   aid_offers     who can be asked for what: agency, unit kind, how many, how
--                  long they take, how likely they say yes, where they stage, and
--                  the lowest level at which they are asked
--   surge_aid      each request the ladder made: offer, agency_request, when it
--                  is due, and what happened
--   surge_drill    a scarcity drill's backup, so capacities can be restored
--
-- Additive (new tables, new agencies, closed school/hall candidates). RLS on.

begin;

create table if not exists public.surge_state (
  region      text primary key check (region in ('pune', 'ncr')),
  level       smallint not null default 0 check (level between 0 and 4),
  reasons     jsonb not null default '[]',
  metrics     jsonb not null default '{}',
  counters    jsonb not null default '{}',
  since       timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);
insert into surge_state (region) values ('pune'), ('ncr') on conflict do nothing;

insert into agencies (id, city_id, name, short_name, kind, jurisdiction, contact, active) values
  ('redcross', 'pune', 'Indian Red Cross Society, Pune branch', 'Red Cross', 'ngo', 'district', '{}', true),
  ('sevasahyog', 'pune', 'Volunteer network (Seva Sahayog / civil society)', 'Volunteers', 'volunteer', 'city', '{}', true),
  ('army', 'pune', 'Indian Army, Southern Command (aid to civil authority)', 'Army', 'government', 'national', '{}', true),
  ('pvttransport', 'pune', 'Private bus and tanker operators (requisitioned)', 'Private fleet', 'private', 'district', '{}', true)
on conflict (id) do nothing;

create table if not exists public.aid_offers (
  id                text primary key,
  region            text not null check (region in ('pune', 'ncr')),
  agency_id         text not null references public.agencies(id),
  kind              text not null references public.resource_kinds(id),
  quantity          integer not null check (quantity > 0),
  response_minutes  integer not null,
  accept_p          real not null check (accept_p between 0 and 1),
  min_level         smallint not null default 2,
  stage_lng         double precision not null,
  stage_lat         double precision not null,
  label             text not null
);

-- Planning figures, not commitments: response times and how often a request is
-- met are assumptions for the drill. NDRF 5th Bn is based at Sudumbare (Maval).
insert into aid_offers (id, region, agency_id, kind, quantity, response_minutes, accept_p, min_level, stage_lng, stage_lat, label) values
  ('pun-redcross-amb',   'pune', 'redcross',     'ambulance',    3, 25, 0.85, 2, 73.8567, 18.5204, 'Red Cross ambulances (Pune)'),
  ('pun-redcross-team',  'pune', 'redcross',     'rescue_team',  2, 35, 0.80, 2, 73.8567, 18.5204, 'Red Cross first-aid teams'),
  ('pun-vol-team',       'pune', 'sevasahyog',   'rescue_team',  3, 30, 0.75, 2, 73.7800, 18.6440, 'Volunteer search teams'),
  ('pun-pmc-fire',       'pune', 'fire',         'fire_engine',  2, 30, 0.70, 2, 73.8553, 18.5196, 'PMC fire tenders (neighbouring corporation)'),
  ('pun-pmc-pump',       'pune', 'pmc',          'pump',         2, 40, 0.70, 2, 73.8553, 18.5196, 'PMC dewatering pumps'),
  ('pun-sdrf-boat',      'pune', 'sdrf',         'boat',         2, 60, 0.90, 2, 73.8040, 18.6440, 'SDRF boats'),
  ('pun-sdrf-team',      'pune', 'sdrf',         'rescue_team',  2, 60, 0.90, 2, 73.8040, 18.6440, 'SDRF rescue teams'),
  ('pun-pvt-bus',        'pune', 'pvttransport', 'bus',          4, 40, 0.80, 3, 73.7706, 18.6653, 'Requisitioned buses (evacuation)'),
  ('pun-pvt-tanker',     'pune', 'pvttransport', 'water_tanker', 3, 45, 0.80, 3, 73.8020, 18.6230, 'Private water tankers'),
  ('pun-ndrf-team',      'pune', 'ndrf',         'rescue_team',  3, 90, 0.95, 4, 73.6820, 18.7140, 'NDRF 5th Bn rescue teams (Sudumbare)'),
  ('pun-ndrf-boat',      'pune', 'ndrf',         'boat',         3, 90, 0.95, 4, 73.6820, 18.7140, 'NDRF 5th Bn boats (Sudumbare)'),
  ('pun-army-boat',      'pune', 'army',         'boat',         4, 120, 0.95, 4, 73.8890, 18.5110, 'Army boats and columns'),
  ('pun-army-truck',     'pune', 'army',         'supply_truck', 3, 120, 0.95, 4, 73.8890, 18.5110, 'Army trucks (relief)'),
  ('ncr-redcross-amb',   'ncr',  'redcross',     'ambulance',    3, 30, 0.80, 2, 77.3090, 28.6280, 'Red Cross ambulances (Delhi NCR)'),
  ('ncr-sdrf-team',      'ncr',  'sdrf',         'rescue_team',  2, 60, 0.85, 2, 77.4380, 28.6690, 'SDRF Uttar Pradesh teams'),
  ('ncr-pvt-bus',        'ncr',  'pvttransport', 'bus',          4, 45, 0.80, 3, 77.4200, 28.6700, 'Requisitioned buses'),
  ('ncr-ndrf-team',      'ncr',  'ndrf8',        'rescue_team',  3, 60, 0.95, 4, 77.4400, 28.6600, 'NDRF 8th Bn (Ghaziabad)')
on conflict (id) do nothing;

create table if not exists public.surge_aid (
  id            bigint generated always as identity primary key,
  region        text not null,
  offer_id      text not null references public.aid_offers(id),
  request_id    uuid,
  capability_id text,
  quantity      integer not null,
  requested_at  timestamptz not null default now(),
  due_at        timestamptz not null,
  outcome       text check (outcome in ('arrived', 'declined', 'cancelled')),
  decided_at    timestamptz,
  units         text[] not null default '{}'
);

create table if not exists public.surge_drill (
  id          bigint generated always as identity primary key,
  region      text not null,
  subject     text not null check (subject in ('resource', 'lifeline')),
  subject_id  text not null,
  before      jsonb not null,
  created_at  timestamptz not null default now(),
  restored_at timestamptz
);

-- Schools and halls the ladder may open as surge shelters (closed until then).
insert into lifelines (id, kind, name, ward_id, location, capacity, occupancy, city_id, accepts_casualties,
                       status, specialities, supplies, supplies_baseline, people_served_per_hour,
                       occupancy_baseline, last_reported_at)
select v.id, 'school', v.name,
       (select w.id from wards w where w.id like 'w-pc-%'
         order by extensions.ST_Distance(w.centroid,
                  extensions.ST_SetSRID(extensions.ST_MakePoint(v.lng, v.lat), 4326)::extensions.geography) limit 1),
       extensions.ST_SetSRID(extensions.ST_MakePoint(v.lng, v.lat), 4326)::extensions.geography,
       v.cap, 0, 'pune', false, 'closed', '{}', '{}'::jsonb, '{}'::jsonb, null, 0, now()
  from (values
    ('lf-pc-sc1', 'Kendriya Vidyalaya No. 2, Dehu Road',   73.74593, 18.67305, 500),
    ('lf-pc-sc2', 'Kirti Vidyalaya High School, Nigdi',    73.76634, 18.65716, 350),
    ('lf-pc-sc3', 'Podar International School, Chinchwad', 73.78654, 18.62474, 400),
    ('lf-pc-sc4', 'Thergaon High School',                  73.77916, 18.61465, 350),
    ('lf-pc-sc5', 'Rangabhumi Hall, Tathawade',            73.74948, 18.61962, 300),
    ('lf-pc-sc6', 'Hindustan Antibiotics School, Pimpri',  73.81150, 18.62532, 450),
    ('lf-pc-sc7', 'Infant Jesus School, Wakad',            73.77084, 18.60310, 300),
    ('lf-pc-sc8', 'St Ann''s School, Chikhli',             73.78824, 18.66700, 350)
  ) as v(id, name, lng, lat, cap)
on conflict (id) do nothing;

alter table public.surge_state enable row level security;
alter table public.aid_offers enable row level security;
alter table public.surge_aid enable row level security;
alter table public.surge_drill enable row level security;
revoke all on public.surge_state, public.aid_offers, public.surge_aid, public.surge_drill from anon, authenticated;

commit;
