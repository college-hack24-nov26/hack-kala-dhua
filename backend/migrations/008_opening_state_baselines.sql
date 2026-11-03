-- What "the beginning" means, written down.
--
-- A demo you cannot rewind is a demo you get one take of. Worse, the second run
-- starts on the first run's wreckage: two hundred resolved incidents, half the
-- fleet parked wherever it finished, and every relief centre at whatever stock
-- the last flood left it. Nothing in the database said what the opening
-- position was, so nothing could restore it.
--
-- Resources already carried `base_location` — where a unit lives when it is not
-- out. Lifelines carried no equivalent, so the opening stock is recorded here,
-- from the same expressions the seed used. A real deployment sets these from
-- its own register; the point is that the column exists and reset reads it
-- rather than a constant compiled into the application.

alter table public.lifelines
  add column if not exists supplies_baseline jsonb,
  add column if not exists occupancy_baseline integer;

comment on column public.lifelines.supplies_baseline is
  'Opening stock. `POST /demo/reset` restores `supplies` to this.';
comment on column public.lifelines.occupancy_baseline is
  'Opening occupancy. `POST /demo/reset` restores `occupancy` to this.';

-- Relief centres, kitchens, water points and camps: the seed expressions.
update public.lifelines set supplies_baseline = jsonb_build_object(
    'food_packets', 600 + (abs(hashtext(ward_id)) % 700),
    'water_litres', 4000 + (abs(hashtext(ward_id)) % 5000),
    'medical_kits', 20 + (abs(hashtext(ward_id)) % 40),
    'blankets',     200 + (abs(hashtext(ward_id)) % 300))
 where kind = 'relief_centre' and supplies_baseline is null;

update public.lifelines set supplies_baseline = jsonb_build_object(
    'food_packets', 900 + (abs(hashtext(ward_id || 'k')) % 900),
    'water_litres', 2000 + (abs(hashtext(ward_id || 'k')) % 3000))
 where kind = 'food_kitchen' and supplies_baseline is null;

update public.lifelines set supplies_baseline = jsonb_build_object(
    'water_litres', 6000 + (abs(hashtext(ward_id)) % 9000))
 where kind = 'water_point' and supplies_baseline is null;

update public.lifelines set supplies_baseline = jsonb_build_object(
    'medical_kits', 30 + (abs(hashtext(ward_id)) % 50))
 where kind = 'medical_camp' and supplies_baseline is null;

-- Everything else holds no relief stock, and whatever occupancy it has now is
-- its opening figure.
update public.lifelines
   set supplies_baseline  = coalesce(supplies_baseline, '{}'::jsonb),
       occupancy_baseline = coalesce(occupancy_baseline, occupancy, 0);

-- A unit with no recorded home has nowhere to be sent back to.
update public.resources set base_location = location
 where base_location is null;
