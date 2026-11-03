-- ============================================================================
-- 003  City scoping, agencies as first-class entities, the append-only event
--      log, and simulation run scoping.
-- ============================================================================

-- ------------------------------------------------------------ city scope ---
alter table wards          add column if not exists city_id text not null default 'pune' references cities(id);
alter table lifelines      add column if not exists city_id text not null default 'pune' references cities(id);
alter table resources      add column if not exists city_id text not null default 'pune' references cities(id);
alter table policy_clauses add column if not exists city_id text not null default 'pune' references cities(id);

create index if not exists wards_city_idx     on wards(city_id);
create index if not exists lifelines_city_idx on lifelines(city_id);
create index if not exists resources_city_idx on resources(city_id);

-- -------------------------------------------------------------- agencies ---
create table if not exists agencies (
  id            text primary key,
  city_id       text not null default 'pune' references cities(id),
  name          text not null,
  short_name    text not null,
  kind          text not null default 'government',  -- government | ngo | volunteer | private
  jurisdiction  text not null default '',            -- 'city' | ward id | 'state'
  contact       jsonb not null default '{}',
  active        boolean not null default true,
  created_at    timestamptz not null default now()
);

create table if not exists agency_capabilities (
  agency_id     text not null references agencies(id) on delete cascade,
  capability_id text not null references capabilities(id) on delete cascade,
  primary key (agency_id, capability_id)
);

insert into agencies (id, name, short_name, kind, jurisdiction) values
  ('pmc',       'Pune Municipal Corporation',          'PMC',       'government', 'city'),
  ('fire',      'Pune Fire Brigade',                   'Fire',      'government', 'city'),
  ('police',    'Pune City Police',                    'Police',    'government', 'city'),
  ('ndrf',      'National Disaster Response Force',    'NDRF',      'government', 'state'),
  ('sdrf',      'State Disaster Response Force',       'SDRF',      'government', 'state'),
  ('health',    'Municipal Health Department',         'Health',    'government', 'city'),
  ('transport', 'Pune Mahanagar Parivahan Mahamandal', 'PMPML',     'government', 'city')
on conflict (id) do nothing;

insert into agency_capabilities (agency_id, capability_id) values
  ('pmc','dewatering'), ('pmc','debris_clearance'),
  ('fire','fire_suppression'), ('fire','water_rescue'), ('fire','search_rescue'), ('fire','dewatering'),
  ('police','debris_clearance'),
  ('ndrf','water_rescue'), ('ndrf','search_rescue'),
  ('sdrf','water_rescue'), ('sdrf','search_rescue'),
  ('health','medical_transport'), ('health','medical_care'),
  ('transport','mass_transport')
on conflict do nothing;

alter table resources add column if not exists agency_id text references agencies(id);

update resources set agency_id = case
  when operator ilike 'NDRF%'          then 'ndrf'
  when operator ilike '%Fire Brigade%' then 'fire'
  when operator ilike 'PMPML%'         then 'transport'
  when operator ilike '%Health%' or operator = '108 Service' then 'health'
  else 'pmc' end
where agency_id is null;

-- ------------------------------------------------------------- sim runs ----
create table if not exists sim_runs (
  id           uuid primary key default gen_random_uuid(),
  city_id      text not null default 'pune' references cities(id),
  name         text not null,
  mode         text not null default 'sim' check (mode in ('sim','replay')),
  scenario_key text,
  clock_start  timestamptz not null,
  clock_now    timestamptz not null,
  speed        numeric not null default 60 check (speed > 0),
  status       text not null default 'paused' check (status in ('paused','running','finished')),
  seed         bigint not null default 1,
  replay_of    uuid references sim_runs(id),
  created_at   timestamptz not null default now()
);

-- null sim_run_id means live. Every read model filters on the active scope, so
-- a simulation never contaminates live operations and can be deleted whole.
alter table citizen_reports  add column if not exists sim_run_id uuid references sim_runs(id) on delete cascade;
alter table incidents        add column if not exists sim_run_id uuid references sim_runs(id) on delete cascade;
alter table allocation_plans add column if not exists sim_run_id uuid references sim_runs(id) on delete cascade;
alter table assignments      add column if not exists sim_run_id uuid references sim_runs(id) on delete cascade;
alter table decisions        add column if not exists sim_run_id uuid references sim_runs(id) on delete cascade;
alter table alerts           add column if not exists sim_run_id uuid references sim_runs(id) on delete cascade;
alter table field_tasks      add column if not exists sim_run_id uuid references sim_runs(id) on delete cascade;
alter table agent_runs       add column if not exists sim_run_id uuid references sim_runs(id) on delete cascade;
alter table hazard_runs      add column if not exists sim_run_id uuid references sim_runs(id) on delete cascade;

create index if not exists incidents_sim_idx   on incidents(sim_run_id);
create index if not exists reports_sim_idx     on citizen_reports(sim_run_id);
create index if not exists assignments_sim_idx on assignments(sim_run_id);

-- ------------------------------------------------------------- event log ---
-- Append only. This single table is the PS20 activity/audit log, the trigger
-- for reactive replanning, and the tape that replay reads back.
create table if not exists events (
  id           bigserial primary key,
  city_id      text not null default 'pune' references cities(id),
  sim_run_id   uuid references sim_runs(id) on delete cascade,
  occurred_at  timestamptz not null,              -- world time (sim clock or wall)
  recorded_at  timestamptz not null default now(),-- wall time, always
  kind         text not null,
  actor        text not null,
  subject_type text not null,
  subject_id   text not null,
  ward_id      text references wards(id),
  payload      jsonb not null default '{}',
  causation_id bigint references events(id)
);

create index if not exists events_scope_idx    on events(sim_run_id, id);
create index if not exists events_subject_idx  on events(subject_type, subject_id, id);
create index if not exists events_kind_idx     on events(kind, id desc);
create index if not exists events_occurred_idx on events(occurred_at desc);

-- Append only, enforced rather than assumed. An audit log you can quietly
-- rewrite is not an audit log.
create or replace function app.events_are_immutable() returns trigger
language plpgsql as $$
begin
  raise exception 'events is append-only; % is not permitted', tg_op;
end;
$$;

drop trigger if exists events_no_update on events;
drop trigger if exists events_no_delete on events;
create trigger events_no_update before update on events
  for each row execute function app.events_are_immutable();
create trigger events_no_delete before delete on events
  for each row execute function app.events_are_immutable();

-- ------------------------------------------------------------------ RLS ----
alter table cities                     enable row level security;
alter table hazard_types               enable row level security;
alter table capabilities               enable row level security;
alter table resource_kinds             enable row level security;
alter table resource_kind_capabilities enable row level security;
alter table lifeline_kinds             enable row level security;
alter table incident_categories        enable row level security;
alter table incident_category_needs    enable row level security;
alter table agencies                   enable row level security;
alter table agency_capabilities        enable row level security;
alter table sim_runs                   enable row level security;
alter table events                     enable row level security;

-- Reference data is public: the citizen portal needs to know what a shelter is
-- and which categories it may report.
create policy cities_public_read      on cities                     for select to anon, authenticated using (true);
create policy hazard_types_read       on hazard_types               for select to anon, authenticated using (true);
create policy capabilities_read       on capabilities               for select to anon, authenticated using (true);
create policy resource_kinds_read     on resource_kinds             for select to anon, authenticated using (true);
create policy rkc_read                on resource_kind_capabilities for select to anon, authenticated using (true);
create policy lifeline_kinds_read     on lifeline_kinds             for select to anon, authenticated using (true);
create policy incident_cats_read      on incident_categories        for select to anon, authenticated using (true);
create policy incident_cat_needs_read on incident_category_needs    for select to anon, authenticated using (true);
create policy agencies_read           on agencies                   for select to anon, authenticated using (true);

-- Operational surfaces are staff only.
create policy agency_caps_read on agency_capabilities for select to authenticated using (app.is_staff());
create policy sim_runs_read    on sim_runs            for select to authenticated using (app.is_staff());
create policy events_read      on events              for select to authenticated using (app.is_staff());
