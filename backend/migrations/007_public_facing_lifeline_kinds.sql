-- Which lifeline kinds a member of the public may be sent to.
--
-- The citizen and field snapshots were filtering on `kind in ('hospital',
-- 'shelter')` — the enum this schema spent migration 002 turning into rows,
-- smuggled back in as a WHERE clause. A deployment that adds a kind (and
-- adding kinds is the point of the table) would have found its food kitchens
-- and water points invisible to the people they are for.
--
-- So the distinction becomes data too: a pump station and a substation matter
-- to the operator and are not somewhere you tell a family to walk to.

alter table public.lifeline_kinds
  add column if not exists serves_public boolean not null default true;

comment on column public.lifeline_kinds.serves_public is
  'True when residents may be directed here. False for infrastructure that '
  'appears on the operator map but is not a destination for the public.';

update public.lifeline_kinds
   set serves_public = false
 where id in ('pump_station', 'substation');
