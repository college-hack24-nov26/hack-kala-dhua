-- 012 — occupancy is modelled, not just measured.
--
-- Capacity was in the schema from the start and occupancy never moved off zero,
-- so every shelter in the city read `0 / 25,020` however bad the flood got. The
-- number was not wrong, it was unwritten: nothing in the system had a reason to
-- change it, because nobody had modelled people arriving anywhere.
--
-- Three things this adds, and each is a row rather than code, so a new city gets
-- them by seeding rather than by deploying:
--
--   1. `shelter_full` as an ordinary incident category. A facility at capacity
--      is an operational problem with a known answer — move people to the next
--      site — and that answer is mass transport, which the fleet already has.
--      Modelling it as a category means the existing solver covers it with a
--      bus, over the same road network, under the same switching cost, and the
--      decision gate treats it like any other. A special case here would have
--      meant a second scheduler to keep in step with the first.
--
--   2. An opening occupancy per facility, so a reset has something to go back
--      to. Zero is the right opening figure for a city that is not flooded yet,
--      but it has to be recorded as a *choice* rather than inherited from a
--      column default, or `reset` cannot tell "nobody has arrived" from "this
--      was never set".
--
--   3. An index on the read the citizen agent makes constantly — the nearest
--      facility of a kind that still has room. It was a sequential scan over
--      every lifeline in the city on every routing request.

begin;

-- ---------------------------------------------------------------- category ---
insert into incident_categories
       (id, display_name, hazard_id, dedup_radius_m, dedup_window_min,
        base_severity, life_safety)
values ('shelter_full', 'Shelter at capacity', 'flood',
        -- A building, not a street: two reports about the same site are the
        -- same site, and there is no second shelter 250 m away to confuse it
        -- with. Tight radius, long window, because a full shelter stays full
        -- for hours and re-reporting it every forty-five minutes would open a
        -- second incident for a problem nobody has solved yet.
        120, 180, 3, false)
on conflict (id) do nothing;

insert into incident_category_needs (category_id, capability_id, qty_per_incident)
values ('shelter_full', 'mass_transport', 1)
on conflict do nothing;

-- ------------------------------------------------------------- baselines ----
-- `reset` restores `occupancy_baseline`; without a value it restores null and
-- the coalesce puts back 0 anyway, but recording it makes the opening position
-- explicit and lets a scenario open with a partly-full shelter.
update lifelines
   set occupancy_baseline = coalesce(occupancy_baseline, coalesce(occupancy, 0))
 where occupancy_baseline is null;

-- ----------------------------------------------------------------- index ----
-- The citizen agent's hot read: nearest open facility of a kind with room.
create index if not exists lifelines_kind_room_idx
    on lifelines (city_id, kind)
 where status <> 'closed' and coalesce(capacity, 0) > 0;

commit;
