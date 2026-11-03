-- Roads, and the geometry of getting to them.
--
-- Two things were wrong and they were the same thing. Units moved in straight
-- lines across city blocks because no assignment stored a route, and hazards
-- were dropped at random points inside ward polygons, so they never landed on a
-- road and no route could be diverted by one. Avoidance had nothing to bite on.
--
-- `route` is what the router returned; `progress` is how far along it the unit
-- is. Movement becomes ST_LineInterpolatePoint over the whole fleet in one
-- statement, and a vehicle turns corners.

alter table public.assignments
  add column if not exists route extensions.geometry(LineString, 4326),
  add column if not exists route_engine text,
  add column if not exists progress numeric not null default 0,
  add column if not exists steps jsonb;

comment on column public.assignments.route is
  'Road geometry from the unit''s position at assignment time to the incident.';
comment on column public.assignments.progress is
  'Fraction of the route covered, 0..1. Driven by the world clock, not by GPS.';
comment on column public.assignments.steps is
  'Turn instructions with street names, as the crew sees them in the field app.';

-- Hazards are snapped to the nearest routable road before they are stored, and
-- the street name is kept so a report reads "Karve Road" rather than a pair of
-- decimals.
alter table public.incidents
  add column if not exists street text;

alter table public.citizen_reports
  add column if not exists street text;
