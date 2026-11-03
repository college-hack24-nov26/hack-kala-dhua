-- Relief distribution, which PS20 names first and this system did not model.
--
-- "food, medical supplies, shelter capacity, and rescue teams" — three of those
-- four were here and the supplies were not. A shelter with no food is a room,
-- and a relief operation that tracks boats but not ration packets is tracking
-- the easy half.
--
-- Modelled as data rather than as new code paths: relief centres are lifelines,
-- their stock is a column, running low raises an ordinary incident, and the
-- truck that refills them is allocated by the same solver that sends a boat.
-- Already applied to the live database; kept here so the schema is reproducible.

insert into public.lifeline_kinds (id, display_name, shelters_people) values
  ('relief_centre', 'Relief centre',        true),
  ('water_point',   'Drinking water point', false),
  ('medical_camp',  'Medical camp',         false),
  ('food_kitchen',  'Community kitchen',    false)
on conflict (id) do nothing;

insert into public.capabilities (id, label, description) values
  ('supply_delivery', 'Relief supply delivery',
   'Moving food, water and medical stock to a distribution point.'),
  ('water_supply', 'Drinking water supply',
   'Delivering potable water in bulk to a point of distribution.')
on conflict (id) do nothing;

insert into public.resource_kinds (id, display_name, default_capacity, city_id) values
  ('supply_truck',  'Relief supply truck', 400,   'pune'),
  ('water_tanker',  'Water tanker',        10000, 'pune')
on conflict (id) do nothing;

insert into public.resource_kind_capabilities (kind_id, capability_id, effectiveness) values
  ('supply_truck', 'supply_delivery', 1.0),
  ('supply_truck', 'mass_transport',  0.4),
  ('water_tanker', 'water_supply',    1.0),
  ('water_tanker', 'supply_delivery', 0.6),
  -- A bus can carry ration packets when nothing better is free. Worse at it,
  -- and the solver already knows what "worse" costs.
  ('bus',          'supply_delivery', 0.45)
on conflict (kind_id, capability_id) do update set effectiveness = excluded.effectiveness;

insert into public.incident_categories
  (id, display_name, hazard_id, base_severity, life_safety, dedup_radius_m, dedup_window_min)
values ('supply_shortage', 'Relief supplies running out', 'flood', 3, false, 150, 120)
on conflict (id) do nothing;

insert into public.incident_category_needs (category_id, capability_id, qty_per_incident) values
  ('supply_shortage', 'supply_delivery', 1)
on conflict (category_id, capability_id) do nothing;

alter table public.lifelines
  add column if not exists supplies jsonb not null default '{}'::jsonb,
  add column if not exists people_served_per_hour integer,
  add column if not exists opened_at timestamptz;

comment on column public.lifelines.supplies is
  'Stock on hand, e.g. {"food_packets": 800, "water_litres": 5000}. Drains as people are served and is refilled by a supply_delivery assignment.';

-- The places themselves are seeded from ward centroids with a small offset, so
-- every one sits inside the covered area and is routable. A real deployment
-- imports these from the municipal register instead; see the applied migration
-- `relief_logistics_food_water_medical` for the seed bodies.
