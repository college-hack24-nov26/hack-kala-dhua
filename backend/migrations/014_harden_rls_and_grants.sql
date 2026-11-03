-- 014 — close the one door that was left open, and take away the keys to the rest.
--
-- Context, because the fix is small and the reason is not. Every table in this
-- schema has RLS enabled and a carefully scoped SELECT policy: staff reads are
-- limited to their own city, a resident sees their own reports, a crew sees its
-- own tasks. That design is right.
--
-- What it did not account for is that Supabase grants `anon` and `authenticated`
-- full DML on every table in `public` by default. RLS is then the only thing
-- standing between the publicly-shipped anon key and the database. For almost
-- every table that is fine, because no write policy exists and RLS denies by
-- default.
--
-- `citizen_reports` was the exception:
--
--     policy reports_insert_any  FOR INSERT TO anon, authenticated
--     WITH CHECK (true)
--
-- Anyone who opened the browser devtools, copied the anon key out of the bundle
-- and POSTed to PostgREST could write rows straight into the reports table —
-- choosing their own `trust_score`, their own `verification_status`, their own
-- `reporter_name`, any ward, and `outcome = 'confirmed'` for good measure.
--
-- That is not a leak of data. It is worse: it is a way to put fabricated
-- evidence *into* the system, underneath the intake pipeline. Trust scoring,
-- duplicate clustering, ward validation and the whole "five channels, one door"
-- property are all implemented in `app/incidents/intake.py`, and this route did
-- not pass through any of it. A system whose central claim is that every report
-- is scored before it can move a unit should not have a side entrance.
--
-- Verified before changing anything: the frontend uses supabase-js for exactly
-- two things — `auth.*` and `channel()` for Realtime. It never calls `.from()`,
-- `.insert()` or `.update()`. Every read and write goes through the FastAPI
-- backend, which connects over its own Postgres DSN as the table owner and is
-- not subject to RLS. So nothing legitimate was using this policy or these
-- grants, and removing them costs no functionality.

begin;

-- ------------------------------------------------------- the open door -----
drop policy if exists reports_insert_any on public.citizen_reports;

-- ------------------------------------------- the keys to everything else ----
-- Defence in depth. With the grants gone, a permissive policy added by mistake
-- in six months' time is no longer sufficient on its own to open a table up —
-- which is exactly how the hole above came to exist.
--
-- `anon` keeps SELECT and nothing else. It is an unauthenticated visitor; there
-- is no circumstance in which it should write a row.
revoke insert, update, delete, truncate, references, trigger
    on all tables in schema public from anon;

-- `authenticated` keeps SELECT, and keeps UPDATE only where a policy
-- deliberately grants it: an officer acting on a decision within their
-- delegation, a crew updating its own task, a person editing their own profile.
-- Those three policies are real design, not accident, so the grant stays and
-- the policy keeps doing the deciding.
revoke insert, delete, truncate, references, trigger
    on all tables in schema public from authenticated;

-- New tables inherit the same posture rather than the Supabase default.
alter default privileges in schema public
    revoke insert, update, delete, truncate, references, trigger on tables from anon;
alter default privileges in schema public
    revoke insert, delete, truncate, references, trigger on tables from authenticated;

-- ------------------------------------------------ views bypassing RLS -------
-- A view runs with its creator's permissions unless told otherwise, so these
-- three answered every caller as the owner — straight past the row-level
-- security that the rest of the schema depends on. `reporter_reliability` is
-- the one that mattered: it is a per-person score, and it was readable by
-- anyone. `security_invoker` makes a view obey the policies of whoever queries
-- it, which is what every one of these was always meant to do.
alter view public.reporter_reliability set (security_invoker = on);
alter view public.latest_hazard_runs  set (security_invoker = on);
alter view public.current_ward_risk   set (security_invoker = on);

-- ------------------------------------------------ a tautology, tightened ----
-- `(app.is_staff() OR true)` is `true`. Whatever this was during development,
-- it means any authenticated account reads every field report in the system.
drop policy if exists field_reports_read on public.field_reports;
create policy field_reports_read on public.field_reports
    for select to authenticated
    using (app.is_staff() or reported_by = app.my_operator());

-- ------------------------------------------------------- search_path --------
-- Neither function is SECURITY DEFINER, so the exposure here is small, but a
-- resolvable `search_path` on a trigger that exists to be unbypassable is not
-- something to leave to the caller's session.
alter function app.events_are_immutable()  set search_path = app, public, pg_temp;
alter function app.preposition_hit_rate(text, text) set search_path = app, public, pg_temp;

-- --------------------------------------------------- RLS, once not per row --
-- `auth.uid()` in a policy is re-evaluated for every candidate row. Wrapped in
-- a scalar sub-select it is evaluated once and the result reused, which is the
-- difference between a sequential scan that calls a function 400 times and one
-- that calls it once. Same predicate, same answer.
drop policy if exists reports_read_own_or_staff on public.citizen_reports;
create policy reports_read_own_or_staff on public.citizen_reports
    for select to anon, authenticated
    using (
        app.is_staff()
        or (reporter_id is not null and reporter_id = (select auth.uid()))
    );

drop policy if exists profiles_read_own on public.profiles;
create policy profiles_read_own on public.profiles
    for select to authenticated
    using (id = (select auth.uid()) or app."current_role"() = 'admin'::app_role);

drop policy if exists profiles_update_own on public.profiles;
create policy profiles_update_own on public.profiles
    for update to authenticated
    using (id = (select auth.uid()))
    with check (id = (select auth.uid()));

-- ------------------------------------------------------ duplicate indexes ---
-- Five pairs of byte-identical indexes, each costing a second write on every
-- insert into the busiest tables in the schema for no read benefit. Migrations
-- that each added "the" spatial index without checking for the other one.
drop index if exists public.reports_open_idx;            -- = citizen_reports_created_idx
drop index if exists public.citizen_reports_location_idx; -- = reports_geo_gix
drop index if exists public.incidents_location_idx;       -- = incidents_geo_gix
drop index if exists public.lifelines_location_idx;       -- = lifelines_geo_gix
drop index if exists public.wards_boundary_idx;           -- = wards_boundary_gix

-- ------------------------------------------------ the index that was missing -
-- `GET /events/{id}/chain` walks `causation_id` backwards to answer "why did
-- that boat go there?", which is the audit story the whole system is sold on.
-- The column is a self-referencing foreign key and had no index, so every step
-- of that walk was a sequential scan over the event log — the one table that
-- only ever grows.
create index if not exists events_causation_idx
    on public.events (causation_id) where causation_id is not null;

-- The city scope on the two hot tables, both read on every console poll.
create index if not exists events_city_occurred_idx
    on public.events (city_id, occurred_at desc);
create index if not exists citizen_reports_city_created_idx
    on public.citizen_reports (city_id, created_at desc);

commit;

-- Deliberately NOT done here: the linter also reports 26 "unused" indexes and
-- 46 unindexed foreign keys. Most of the unused ones are PostGIS GiST indexes
-- that back ward location, nearest-shelter and radius queries — they are unused
-- because this database is young and the spatial paths have not been hammered,
-- not because they are useless, and dropping them would be undone the first
-- time the citizen app is opened by a crowd. Most of the unindexed foreign keys
-- are on reference tables with fewer than twenty rows, where an index is
-- strictly slower than the scan it replaces. Both lists are worth re-reading
-- after a real load test, and neither is worth acting on blind.
