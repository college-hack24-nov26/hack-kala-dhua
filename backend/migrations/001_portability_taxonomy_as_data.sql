-- ============================================================================
-- 001  Portability foundation.
--
-- Closed Postgres enums for the *taxonomy* (which hazards exist, which incident
-- categories, which resource kinds) are what stop this system running a new
-- disaster in a new city without a migration and a redeploy. They become
-- reference tables. Lifecycle enums (status columns) stay enums, because those
-- genuinely are closed sets that application code branches on.
-- ============================================================================

-- ---------------------------------------------------------------- cities ---
create table if not exists cities (
  id            text primary key,
  name          text not null,
  country       text not null default 'IN',
  timezone      text not null default 'Asia/Kolkata',
  languages     text[] not null default '{English}',
  centroid      extensions.geography(Point,4326),
  admin_unit_singular text not null default 'ward',   -- ward | zone | district
  admin_unit_plural   text not null default 'wards',
  created_at    timestamptz not null default now()
);

insert into cities (id, name, languages, admin_unit_singular, admin_unit_plural)
values ('pune', 'Pune', '{English,Hindi,Marathi}', 'ward', 'wards')
on conflict (id) do nothing;

-- ---------------------------------------------------------- hazard types ---
create table if not exists hazard_types (
  id            text primary key,
  display_name  text not null,
  description   text not null default '',
  sort_order    int  not null default 100,
  created_at    timestamptz not null default now()
);

insert into hazard_types (id, display_name, sort_order) values
  ('flood',   'Flood',        10),
  ('heat',    'Heatwave',     20),
  ('fire',    'Wildfire',     30),
  ('air',     'Air Quality',  40),
  ('seismic', 'Seismic',      50)
on conflict (id) do nothing;

-- ---------------------------------------------------------- capabilities ---
-- What a unit can DO. Demands require capabilities; resources provide them.
-- This is what lets a new resource type work without touching the solver.
create table if not exists capabilities (
  id           text primary key,
  label        text not null,
  description  text not null default ''
);

insert into capabilities (id, label, description) values
  ('water_rescue',      'Water rescue',       'Reaching and extracting people from standing or moving water'),
  ('dewatering',        'Dewatering',         'Removing standing water from streets, basements, underpasses'),
  ('medical_transport', 'Medical transport',  'Moving casualties to a medical facility'),
  ('medical_care',      'On-site medical care','Treating casualties where they are'),
  ('mass_transport',    'Mass transport',     'Moving large numbers of people to shelter'),
  ('debris_clearance',  'Debris clearance',   'Clearing trees, rubble and blockages from routes'),
  ('fire_suppression',  'Fire suppression',   'Extinguishing and containing fire'),
  ('search_rescue',     'Search and rescue',  'Locating and extracting trapped people')
on conflict (id) do nothing;

-- -------------------------------------------------------- resource kinds ---
create table if not exists resource_kinds (
  id            text primary key,
  display_name  text not null,
  default_capacity int not null default 1,
  city_id       text references cities(id),   -- null = available to every city
  created_at    timestamptz not null default now()
);

insert into resource_kinds (id, display_name, default_capacity) values
  ('boat',        'Rescue boat',   6),
  ('pump',        'Dewatering pump', 1),
  ('ambulance',   'Ambulance',     4),
  ('fire_engine', 'Fire engine',   6),
  ('rescue_team', 'Rescue team',   8),
  ('bus',         'Bus',          40),
  ('jcb',         'Earthmover',    1)
on conflict (id) do nothing;

create table if not exists resource_kind_capabilities (
  kind_id       text not null references resource_kinds(id) on delete cascade,
  capability_id text not null references capabilities(id) on delete cascade,
  effectiveness numeric not null default 1.0 check (effectiveness > 0 and effectiveness <= 1),
  primary key (kind_id, capability_id)
);

insert into resource_kind_capabilities (kind_id, capability_id, effectiveness) values
  ('boat',        'water_rescue',      1.0),
  ('boat',        'search_rescue',     0.7),
  ('rescue_team', 'search_rescue',     1.0),
  ('rescue_team', 'water_rescue',      0.8),
  ('rescue_team', 'debris_clearance',  0.6),
  ('pump',        'dewatering',        1.0),
  ('fire_engine', 'fire_suppression',  1.0),
  ('fire_engine', 'dewatering',        0.5),
  ('fire_engine', 'water_rescue',      0.6),
  ('ambulance',   'medical_transport', 1.0),
  ('ambulance',   'medical_care',      0.7),
  ('bus',         'mass_transport',    1.0),
  ('jcb',         'debris_clearance',  1.0)
on conflict do nothing;

-- -------------------------------------------------------- lifeline kinds ---
create table if not exists lifeline_kinds (
  id           text primary key,
  display_name text not null,
  shelters_people boolean not null default false
);

insert into lifeline_kinds (id, display_name, shelters_people) values
  ('hospital',     'Hospital',      false),
  ('school',       'School',        false),
  ('shelter',      'Shelter',       true),
  ('pump_station', 'Pump station',  false),
  ('substation',   'Substation',    false)
on conflict (id) do nothing;

-- ----------------------------------------------------- incident categories --
-- Deduplication parameters live here as DATA. A city that needs a wider
-- clustering radius for waterlogging changes a row, not the code.
create table if not exists incident_categories (
  id                 text primary key,
  display_name       text not null,
  hazard_id          text references hazard_types(id),
  dedup_radius_m     int  not null default 250,
  dedup_window_min   int  not null default 45,
  base_severity      smallint not null default 3 check (base_severity between 1 and 5),
  life_safety        boolean not null default false
);

insert into incident_categories
  (id, display_name, hazard_id, dedup_radius_m, dedup_window_min, base_severity, life_safety) values
  ('flooded_road',       'Flooded road',        'flood', 300, 45, 3, false),
  ('waterlogging',       'Waterlogging',        'flood', 350, 60, 2, false),
  ('fallen_tree',        'Fallen tree',         'flood', 120, 90, 2, false),
  ('blocked_drain',      'Blocked drain',       'flood', 150, 120, 2, false),
  ('structural_damage',  'Structural damage',   'flood', 100, 60, 4, true),
  ('person_stranded',    'Person stranded',     'flood', 150, 15, 5, true),
  ('power_line',         'Downed power line',   'flood', 120, 45, 4, true),
  ('heat_casualty',      'Heat casualty',       'heat',  200, 30, 4, true)
on conflict (id) do nothing;

-- Which capabilities a category needs, per incident. Needs assessment reads this.
create table if not exists incident_category_needs (
  category_id   text not null references incident_categories(id) on delete cascade,
  capability_id text not null references capabilities(id) on delete cascade,
  qty_per_incident int not null default 1 check (qty_per_incident > 0),
  primary key (category_id, capability_id)
);

insert into incident_category_needs (category_id, capability_id, qty_per_incident) values
  ('flooded_road',      'dewatering',        1),
  ('waterlogging',      'dewatering',        1),
  ('fallen_tree',       'debris_clearance',  1),
  ('blocked_drain',     'dewatering',        1),
  ('structural_damage', 'search_rescue',     1),
  ('person_stranded',   'water_rescue',      1),
  ('power_line',        'debris_clearance',  1),
  ('heat_casualty',     'medical_transport', 1)
on conflict do nothing;
