-- Two categories that were missing, and the second one is the important one.
--
-- Tested with: "there is fire in chintamani nagar and people have died".
-- Every tier of the classifier answered `flooded_road`, and not one of them was
-- broken. There is no fire category in `incident_categories`:
--
--   * the keyword vocabulary has no fire words, so the deterministic pass
--     matched nothing and returned the module's fallback — `flooded_road`;
--   * the zero-shot classifier is shown the taxonomy's own category names and
--     picks the nearest, and the nearest to a fire among ten flood categories
--     is not a fire;
--   * the chat model is handed the same ten ids and told "reply UNKNOWN if none
--     fits". It can say UNKNOWN. `parse_with_model` then discards that and
--     keeps the deterministic guess, which is `flooded_road`.
--
-- So a reported fire with deaths was filed as a flooded road, and a flooded
-- road wants `dewatering`. The city would have sent a pump.
--
-- `fire_suppression` has existed as a capability since the first migration and
-- six fire engines in the seeded fleet hold it. Nothing in the system could
-- ever ask for it, because no category named a fire. The whole chain was there
-- except the row that starts it.

insert into incident_categories
  (id, display_name, hazard_id, dedup_radius_m, dedup_window_min,
   base_severity, life_safety)
values
  ('fire', 'Fire', 'fire', 120, 30, 5, true)
on conflict (id) do nothing;

-- Two needs, not one. A fire that a person has bothered to report is a fire
-- with people near it: `base_severity` 5 and `life_safety` say so. Sending an
-- engine and no ambulance to that is the same class of mistake as sending a
-- pump. Nine ambulances and six engines in the fleet; a handful of fires will
-- not starve the city, and an over-commitment an officer can stand down is
-- cheaper than a casualty waiting for transport nobody requested.
insert into incident_category_needs (category_id, capability_id, qty_per_incident)
values
  ('fire', 'fire_suppression', 1),
  ('fire', 'medical_transport', 1)
on conflict do nothing;

-- The holding pen.
--
-- This is the fix that matters beyond fire. When no tier recognises a report,
-- the parser returned a *specific wrong category* — and a specific category
-- carries a specific need, which dispatches a specific vehicle. Guessing
-- "flooded road" for an unrecognised emergency is not a conservative default;
-- it is a confident wrong answer that commits a unit.
--
-- `unknown_report` has **no row in `incident_category_needs` on purpose**. It
-- opens an incident, puts it on the map and in the queue where an officer can
-- read the words the person actually wrote, and requests nothing. "We do not
-- know what this is" is a real answer and the only honest one available when
-- three classifiers have failed.
--
-- `hazard_id` is the deployment's primary hazard because the column is a
-- required foreign key, not because an unknown report is a flood. Nothing keys
-- dispatch off the hazard id; the needs table does that, and this category has
-- none.
insert into incident_categories
  (id, display_name, hazard_id, dedup_radius_m, dedup_window_min,
   base_severity, life_safety)
values
  ('unknown_report', 'Not yet classified', 'flood', 150, 60, 3, false)
on conflict (id) do nothing;
