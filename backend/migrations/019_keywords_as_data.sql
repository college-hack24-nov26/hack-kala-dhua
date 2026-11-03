-- Adding a hazard should be configuration, not a deployment.
--
-- The claim this project makes is that a second city, or a second hazard, is
-- rows rather than a release: categories, clustering radii, needs, capabilities
-- and resource kinds are all reference tables already. **The words that
-- recognise a report were not.** `VOCAB` in `app/incidents/parse.py` was a
-- Python dict, so `hazard_types` could carry five hazards while the parser knew
-- the vocabulary of three — and the two it did not know (air, seismic) had no
-- category either, which is the same hole that sent a reported fire a pump.
--
-- So the keywords move into the database beside everything else they belong
-- with. `parse._keyword_scores` reads them from the taxonomy cache and falls
-- back to the in-code dict only when this table is empty, so a deployment that
-- has not run this migration behaves exactly as before.
--
-- `weight` is a multiplier, not a score. A deployment that finds "drain" too
-- eager in its own city lowers that one row; nobody has to invent a synonym or
-- edit Python to tune a classifier.

create table if not exists incident_category_keywords (
  category_id  text    not null references incident_categories(id) on delete cascade,
  phrase       text    not null,
  weight       numeric not null default 1.0 check (weight > 0 and weight <= 5),
  primary key (category_id, phrase)
);

comment on table incident_category_keywords is
  'Phrases that identify an incident category in free text. Matched as '
  'substrings against the lowercased report, in any script. Longer phrases '
  'score higher than bare nouns, so "cannot cross" outranks "road".';

-- The two hazards that had adapters, a hazard_types row, and no way to be
-- reported.
--
-- `air_quality` has **no needs on purpose**. An air-quality episode is
-- advisories, clinics and staying indoors; it does not dispatch a vehicle, and
-- inventing a need so the row looks complete would put an ambulance on a
-- smog reading.
insert into incident_categories
  (id, display_name, hazard_id, dedup_radius_m, dedup_window_min,
   base_severity, life_safety)
values
  ('air_quality', 'Air quality', 'air', 2000, 180, 3, false),
  ('earthquake_damage', 'Earthquake damage', 'seismic', 200, 60, 5, true)
on conflict (id) do nothing;

insert into incident_category_needs (category_id, capability_id, qty_per_incident)
values
  ('earthquake_damage', 'search_rescue', 1),
  ('earthquake_damage', 'medical_transport', 1)
on conflict do nothing;

-- Seeded from the dict that was in the parser, so this migration changes where
-- the words live and not which words they are. Deliberately unambiguous
-- additions for the new categories: "tremor" and "aftershock" belong to nothing
-- else, and "smog"/"haze" are kept away from "smoke", which is a fire.
insert into incident_category_keywords (category_id, phrase, weight)
values
  ('person_stranded', 'stranded', 1.0),
  ('person_stranded', 'stuck', 1.0),
  ('person_stranded', 'trapped', 1.0),
  ('person_stranded', 'cannot get out', 1.0),
  ('person_stranded', 'cant get out', 1.0),
  ('person_stranded', 'rescue', 1.0),
  ('person_stranded', 'help us', 1.0),
  ('person_stranded', 'on the roof', 1.0),
  ('person_stranded', 'on terrace', 1.0),
  ('person_stranded', 'अडकले', 1.0),
  ('person_stranded', 'फसले', 1.0),
  ('person_stranded', 'बचाव', 1.0),
  ('person_stranded', 'फंसे', 1.0),
  ('person_stranded', 'मदद', 1.0),
  ('person_stranded', 'drowning', 1.0),
  ('person_stranded', 'swept', 1.0),
  ('person_stranded', 'not breathing', 1.0),
  ('person_stranded', 'unconscious', 1.0),
  ('person_stranded', 'bleeding', 1.0),
  ('person_stranded', 'collapsed person', 1.0),
  ('person_stranded', 'needs ambulance', 1.0),
  ('person_stranded', 'chest pain', 1.0),
  ('flooded_road', 'road', 1.0),
  ('flooded_road', 'street', 1.0),
  ('flooded_road', 'lane', 1.0),
  ('flooded_road', 'highway', 1.0),
  ('flooded_road', 'bridge', 1.0),
  ('flooded_road', 'underpass', 1.0),
  ('flooded_road', 'flooded', 1.0),
  ('flooded_road', 'water on', 1.0),
  ('flooded_road', 'under water', 1.0),
  ('flooded_road', 'knee deep', 1.0),
  ('flooded_road', 'cannot cross', 1.0),
  ('flooded_road', 'cant cross', 1.0),
  ('flooded_road', 'रस्ता', 1.0),
  ('flooded_road', 'रास्ता', 1.0),
  ('flooded_road', 'पूल', 1.0),
  ('flooded_road', 'waterlogged road', 1.0),
  ('flooded_road', 'submerged', 1.0),
  ('waterlogging', 'waterlogg', 1.0),
  ('waterlogging', 'water logged', 1.0),
  ('waterlogging', 'water collected', 1.0),
  ('waterlogging', 'standing water', 1.0),
  ('waterlogging', 'water outside', 1.0),
  ('waterlogging', 'basement', 1.0),
  ('waterlogging', 'साचले', 1.0),
  ('waterlogging', 'जमा', 1.0),
  ('blocked_drain', 'drain', 1.0),
  ('blocked_drain', 'sewer', 1.0),
  ('blocked_drain', 'manhole', 1.0),
  ('blocked_drain', 'overflow', 1.0),
  ('blocked_drain', 'गटार', 1.0),
  ('blocked_drain', 'नाली', 1.0),
  ('blocked_drain', 'choked', 1.0),
  ('fallen_tree', 'tree', 1.0),
  ('fallen_tree', 'branch', 1.0),
  ('fallen_tree', 'uprooted', 1.0),
  ('fallen_tree', 'झाड', 1.0),
  ('fallen_tree', 'पेड', 1.0),
  ('fallen_tree', 'fallen tree', 1.0),
  ('fallen_tree', 'tree fell', 1.0),
  ('fallen_tree', 'tree down', 1.0),
  ('fallen_tree', 'tree has fallen', 1.0),
  ('fallen_tree', 'झाड पडले', 1.0),
  ('power_line', 'wire', 1.0),
  ('power_line', 'cable', 1.0),
  ('power_line', 'electric', 1.0),
  ('power_line', 'current', 1.0),
  ('power_line', 'spark', 1.0),
  ('power_line', 'pole', 1.0),
  ('power_line', 'transformer', 1.0),
  ('power_line', 'तार', 1.0),
  ('power_line', 'बिजली', 1.0),
  ('power_line', 'वीज', 1.0),
  ('power_line', 'shock', 1.0),
  ('structural_damage', 'wall', 1.0),
  ('structural_damage', 'collapse', 1.0),
  ('structural_damage', 'roof', 1.0),
  ('structural_damage', 'crack', 1.0),
  ('structural_damage', 'building collapse', 1.0),
  ('structural_damage', 'wall came down', 1.0),
  ('structural_damage', 'भिंत', 1.0),
  ('structural_damage', 'कोसळ', 1.0),
  ('structural_damage', 'दीवार', 1.0),
  ('heat_casualty', 'heat', 1.0),
  ('heat_casualty', 'sunstroke', 1.0),
  ('heat_casualty', 'heatstroke', 1.0),
  ('heat_casualty', 'fainted', 1.0),
  ('heat_casualty', 'dehydrat', 1.0),
  ('heat_casualty', 'उष्ण', 1.0),
  ('heat_casualty', 'गर्मी', 1.0),
  ('fire', 'fire', 1.0),
  ('fire', 'burning', 1.0),
  ('fire', 'burnt', 1.0),
  ('fire', 'smoke', 1.0),
  ('fire', 'smouldering', 1.0),
  ('fire', 'smoldering', 1.0),
  ('fire', 'flames', 1.0),
  ('fire', 'blaze', 1.0),
  ('fire', 'gas leak', 1.0),
  ('fire', 'cylinder burst', 1.0),
  ('fire', 'short circuit', 1.0),
  ('fire', 'आग', 1.0),
  ('fire', 'जळत', 1.0),
  ('fire', 'धूर', 1.0),
  ('fire', 'धुर', 1.0),
  ('fire', 'आगीत', 1.0),
  ('fire', 'जल रहा', 1.0),
  ('fire', 'धुआं', 1.0),
  ('fire', 'आग लग', 1.0),
  ('air_quality', 'smog', 1.0),
  ('air_quality', 'haze', 1.0),
  ('air_quality', 'air quality', 1.0),
  ('air_quality', 'aqi', 1.0),
  ('air_quality', 'pollution', 1.0),
  ('air_quality', 'burning eyes', 1.0),
  ('air_quality', 'hard to breathe', 1.0),
  ('air_quality', 'धुरके', 1.0),
  ('air_quality', 'प्रदूषण', 1.0),
  ('air_quality', 'हवा खराब', 1.0),
  ('earthquake_damage', 'earthquake', 1.0),
  ('earthquake_damage', 'tremor', 1.0),
  ('earthquake_damage', 'tremors', 1.0),
  ('earthquake_damage', 'quake', 1.0),
  ('earthquake_damage', 'aftershock', 1.0),
  ('earthquake_damage', 'भूकंप', 1.0),
  ('earthquake_damage', 'धक्का बसला', 1.0),
  ('earthquake_damage', 'भूकंप आया', 1.0),
  ('supply_shortage', 'no food', 1.0),
  ('supply_shortage', 'no water', 1.0),
  ('supply_shortage', 'ration', 1.0),
  ('supply_shortage', 'supplies', 1.0),
  ('supply_shortage', 'relief', 1.0),
  ('supply_shortage', 'nothing to eat', 1.0),
  ('supply_shortage', 'अन्न', 1.0),
  ('supply_shortage', 'रेशन', 1.0),
  ('supply_shortage', 'पाणी नाही', 1.0)
on conflict (category_id, phrase) do nothing;
