-- 031: memory for the re-learning loop.
--
-- The passability and ETA models are trained on simulated storms first. In
-- operation they must keep learning from what actually happens. That needs
-- four things in the database:
--
--   model_registry   every trained model, its metrics, and which one is live
--                    (exactly one champion per task)
--   nav_predictions  what a model said about a road, when, for which unit and
--                    horizon, with the exact inputs (for audit and retraining)
--   nav_outcomes     what was then seen on that road: crew/patrol/official
--                    reports (blocked or cleared), sensors, and roads a unit
--                    actually drove (open). Written by triggers from
--                    road_blocks, and by the app for driven roads.
--   trip_outcomes    promised ETA vs actual arrival per assignment
--
-- nav_examples (view) joins each prediction to the trusted outcome nearest the
-- moment it was about (made_at + horizon, +-15 min, within the outcome's
-- radius): that is a labelled example. `python -m ml.relearn` reads it.
--
-- Roads are matched by place, not id: a prediction stores its segment's
-- midpoint, an outcome its point and radius, so a crew's pin on a map labels
-- the segments around it. Additive only; RLS on, writes are server-side.

begin;

create table if not exists public.model_registry (
  version      text primary key,
  task         text not null check (task in ('passability', 'eta')),
  status       text not null default 'candidate'
                 check (status in ('candidate', 'champion', 'retired', 'rejected')),
  trained_at   timestamptz not null default now(),
  data_window  jsonb not null default '{}',     -- storms / real-outcome window it learned from
  metrics      jsonb not null default '{}',     -- frozen test + recent real outcomes
  decision     jsonb not null default '{}',     -- why it was promoted or rejected
  artifact_uri text,                            -- where the model files live
  promoted_at  timestamptz,
  retired_at   timestamptz
);
create unique index if not exists model_registry_one_champion
  on public.model_registry (task) where status = 'champion';

create table if not exists public.nav_predictions (
  id            bigint generated always as identity primary key,
  city_id       text not null default 'pune' references public.cities(id),
  sim_run_id    uuid,
  model_version text not null references public.model_registry(version),
  made_at       timestamptz not null default now(),
  seg_id        text not null,                  -- OSM segment "{way}:{k}" (ml.graph)
  location      extensions.geography(Point, 4326) not null,   -- segment midpoint
  profile       text not null,
  horizon_min   smallint not null check (horizon_min between 0 and 240),
  p_blocked     real not null check (p_blocked between 0 and 1),
  p_sd          real,
  features      jsonb,
  assignment_id uuid references public.assignments(id) on delete set null,
  used_for      text not null default 'route' check (used_for in ('route', 'scan', 'shadow'))
);
create index if not exists nav_predictions_time_idx on public.nav_predictions (made_at desc);
create index if not exists nav_predictions_loc_idx on public.nav_predictions using gist (location);

create table if not exists public.nav_outcomes (
  id           bigint generated always as identity primary key,
  city_id      text not null default 'pune' references public.cities(id),
  sim_run_id   uuid,
  observed_at  timestamptz not null default now(),
  location     extensions.geography(Point, 4326) not null,
  radius_m     integer not null default 40,
  seg_id       text,                            -- when the writer knows the segment
  profile      text,                            -- null: applies to every road unit
  blocked      boolean not null,
  depth_m      real,
  source       text not null check (source in ('crew', 'patrol', 'sensor', 'official', 'drone', 'trip')),
  confidence   real not null default 1.0 check (confidence between 0 and 1),
  ref_table    text,
  ref_id       text
);
create index if not exists nav_outcomes_time_idx on public.nav_outcomes (observed_at desc);
create index if not exists nav_outcomes_loc_idx on public.nav_outcomes using gist (location);

create table if not exists public.trip_outcomes (
  assignment_id  uuid primary key references public.assignments(id) on delete cascade,
  city_id        text not null default 'pune' references public.cities(id),
  model_version  text references public.model_registry(version),
  profile        text not null,
  called_at      timestamptz,
  departed_at    timestamptz,
  arrived_at     timestamptz,
  eta_p50_min    real,
  eta_p90_min    real,
  replans        smallint not null default 0,
  invalid_route  boolean not null default false,
  features       jsonb
);

-- A crew/officer pin in road_blocks is an outcome: blocked when raised,
-- open again when it is cleared.
create or replace function public.road_blocks_to_outcomes()
returns trigger language plpgsql
security definer                  -- crews insert road_blocks under RLS; the outcome row is server-owned
set search_path = public
as $$
begin
  if tg_op = 'INSERT' and new.active then
    insert into nav_outcomes (city_id, sim_run_id, observed_at, location, radius_m, blocked, source,
                              confidence, ref_table, ref_id)
    values (new.city_id, new.sim_run_id, new.created_at, new.location, least(greatest(coalesce(new.radius_m, 40), 20), 150),
            true, case when new.reported_by ilike 'drone%' then 'drone' else 'crew' end, 0.95,
            'road_blocks', new.id::text);
  elsif tg_op = 'UPDATE' and old.active and not new.active then
    insert into nav_outcomes (city_id, sim_run_id, observed_at, location, radius_m, blocked, source,
                              confidence, ref_table, ref_id)
    values (new.city_id, new.sim_run_id, now(), new.location, least(greatest(coalesce(new.radius_m, 40), 20), 150),
            false, 'crew', 0.9, 'road_blocks', new.id::text);
  end if;
  return new;
end $$;

drop trigger if exists road_blocks_outcomes on public.road_blocks;
create trigger road_blocks_outcomes after insert or update of active on public.road_blocks
  for each row execute function public.road_blocks_to_outcomes();

create or replace view public.nav_examples with (security_invoker = true) as
select p.id as prediction_id, p.model_version, p.city_id, p.sim_run_id, p.seg_id, p.profile,
       p.horizon_min, p.p_blocked, p.p_sd, p.features, p.made_at, p.used_for,
       o.blocked as label, o.source as label_source, o.observed_at, o.confidence
  from public.nav_predictions p
  join lateral (
    select o.*
      from public.nav_outcomes o
     where o.city_id = p.city_id
       and (o.profile is null or o.profile = p.profile)
       and o.confidence >= 0.8
       and o.observed_at between p.made_at + make_interval(mins => p.horizon_min - 15)
                             and p.made_at + make_interval(mins => p.horizon_min + 15)
       and (o.seg_id = p.seg_id
            or (o.seg_id is null and extensions.ST_DWithin(o.location, p.location, o.radius_m + 30)))
     order by abs(extract(epoch from (o.observed_at - (p.made_at + make_interval(mins => p.horizon_min)))))
     limit 1) o on true;

alter table public.model_registry enable row level security;
alter table public.nav_predictions enable row level security;
alter table public.nav_outcomes enable row level security;
alter table public.trip_outcomes enable row level security;
revoke all on public.model_registry, public.nav_predictions, public.nav_outcomes, public.trip_outcomes
  from anon, authenticated;
revoke all on public.nav_examples from anon, authenticated;
revoke all on function public.road_blocks_to_outcomes() from public, anon, authenticated;

commit;
