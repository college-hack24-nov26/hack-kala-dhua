-- 030: agent logs filed by hazard and sector.
--
-- Every event gets two tags: the hazard it is about (flood, tree, wire,
-- collapse, landslide, fire, gas, crowd, jam, storm, bridge, airspace, heat)
-- and the operational sector it happened in (a named group of wards, the
-- incident-command "division"). The console's agent board is a hazard x sector
-- grid built from these.
--
-- New rows are tagged by a BEFORE INSERT trigger, so no write path in the
-- application has to change and append_many is covered too. Old rows cannot be
-- updated (events_no_update refuses it, by design), so the `agent_log` view
-- derives the same tags for them with the same function. Sectors and the
-- category -> hazard map are tables, so an officer can regroup wards without a
-- deploy.
--
-- Additive: two nullable columns, three small tables, one function, one
-- trigger, one view, one index.

begin;

create table if not exists public.sectors (
  id     text primary key,
  name   text not null,
  faces  text[] not null default '{}'
);

create table if not exists public.ward_sectors (
  ward_id   text primary key,
  sector_id text not null references public.sectors(id)
);

create table if not exists public.category_hazards (
  category_id text primary key,
  hazard_id   text not null
);

insert into sectors (id, name, faces) values
  ('pcmc-pawana', 'Pawana riverside', '{flood,bridge,crowd}'),
  ('pcmc-nigdi-akurdi', 'Nigdi–Akurdi (PCCOE)', '{flood,tree,wire}'),
  ('pcmc-wakad-thergaon', 'Wakad–Thergaon', '{flood,jam}'),
  ('pcmc-midc-chikhli', 'MIDC–Chikhli', '{fire,gas,flood}'),
  ('pcmc-dehu-talawade', 'Dehu Road–Talawade', '{landslide,flood,airspace}')
on conflict (id) do nothing;

insert into ward_sectors (ward_id, sector_id) values
  ('w-pc-03','pcmc-pawana'), ('w-pc-05','pcmc-pawana'), ('w-pc-07','pcmc-pawana'), ('w-pc-10','pcmc-pawana'),
  ('w-pc-01','pcmc-nigdi-akurdi'), ('w-pc-02','pcmc-nigdi-akurdi'),
  ('w-pc-04','pcmc-wakad-thergaon'), ('w-pc-08','pcmc-wakad-thergaon'), ('w-pc-09','pcmc-wakad-thergaon'),
  ('w-pc-14','pcmc-wakad-thergaon'),
  ('w-pc-06','pcmc-midc-chikhli'), ('w-pc-11','pcmc-midc-chikhli'),
  ('w-pc-12','pcmc-dehu-talawade'), ('w-pc-13','pcmc-dehu-talawade')
on conflict (ward_id) do nothing;

insert into category_hazards (category_id, hazard_id) values
  ('flooded_road','flood'), ('waterlogging','flood'), ('blocked_drain','flood'),
  ('person_stranded','flood'), ('shelter_full','flood'), ('supply_shortage','flood'),
  ('fallen_tree','tree'), ('power_line','wire'), ('structural_damage','collapse'),
  ('earthquake_damage','collapse'), ('fire','fire'), ('air_quality','gas'),
  ('heat_casualty','heat'), ('unknown_report','unknown')
on conflict (category_id) do nothing;

alter table public.events add column if not exists hazard_id text;
alter table public.events add column if not exists sector_id text;

-- The one place the tagging rules live. Stable: reads reference tables only.
create or replace function public.event_tags(
  p_kind text, p_subject_type text, p_subject_id text, p_ward_id text, p_payload jsonb,
  out hazard_id text, out sector_id text)
language plpgsql stable
set search_path = public
as $$
declare
  v_ward text := p_ward_id;
  v_cat  text := coalesce(p_payload->>'category', p_payload->>'categoryId');
  v_text text := lower(coalesce(p_payload->>'reason', '') || ' ' || coalesce(p_payload->>'note', '')
                       || ' ' || coalesce(p_payload->>'summary', ''));
begin
  hazard_id := coalesce(p_payload->>'hazardTag', p_payload->>'hazard_tag');

  if p_subject_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-' then
    if p_subject_type = 'incident' then
      select coalesce(v_cat, i.category), coalesce(v_ward, i.ward_id) into v_cat, v_ward
        from incidents i where i.id = p_subject_id::uuid;
    elsif p_subject_type = 'assignment' then
      select coalesce(v_cat, i.category), coalesce(v_ward, i.ward_id) into v_cat, v_ward
        from assignments a join incidents i on i.id = a.incident_id where a.id = p_subject_id::uuid;
    end if;
  end if;

  if hazard_id is null and v_cat is not null then
    select ch.hazard_id into hazard_id from category_hazards ch where ch.category_id = v_cat;
  end if;
  if hazard_id is null and (p_kind like 'road.%' or p_kind like 'field.%' or p_kind like 'guardrail.%'
                            or p_kind like 'agent.%' or p_kind like 'drone.%') then
    hazard_id := case
      when v_text ~ '(flood|water|waterlog|submerg|inundat)' then 'flood'
      when v_text ~ '(tree|debris)' then 'tree'
      when v_text ~ '(wire|electric|power line)' then 'wire'
      when v_text ~ '(collapse|wall|building)' then 'collapse'
      when v_text ~ 'landslide' then 'landslide'
      when v_text ~ '(fire|smoke)' then 'fire'
      when v_text ~ 'gas' then 'gas'
      when v_text ~ 'crowd' then 'crowd'
      when v_text ~ '(traffic|jam|congest)' then 'jam'
      else null end;
  end if;

  if v_ward is not null then
    select ws.sector_id into sector_id from ward_sectors ws where ws.ward_id = v_ward;
    if sector_id is null then
      sector_id := case when v_ward like 'w-gzb-%' then 'ncr' else 'ward:' || v_ward end;
    end if;
  else
    sector_id := p_payload->>'sectorId';
  end if;
end $$;

create or replace function public.events_fill_tags()
returns trigger language plpgsql
set search_path = public
as $$
declare t record;
begin
  if new.hazard_id is null or new.sector_id is null then
    t := public.event_tags(new.kind, new.subject_type, new.subject_id, new.ward_id, new.payload);
    new.hazard_id := coalesce(new.hazard_id, t.hazard_id);
    new.sector_id := coalesce(new.sector_id, t.sector_id);
  end if;
  return new;
end $$;

drop trigger if exists events_fill_tags on public.events;
create trigger events_fill_tags before insert on public.events
  for each row execute function public.events_fill_tags();

create index if not exists events_sector_hazard_idx on public.events (sector_id, hazard_id, id desc);

-- Old rows were written before the columns existed: derive their tags on read.
create or replace view public.agent_log with (security_invoker = true) as
select e.id, e.city_id, e.sim_run_id, e.occurred_at, e.recorded_at, e.kind, e.actor,
       e.subject_type, e.subject_id, e.ward_id, e.payload, e.causation_id,
       coalesce(e.hazard_id, t.hazard_id) as hazard_id,
       coalesce(e.sector_id, t.sector_id) as sector_id
  from public.events e
  left join lateral public.event_tags(e.kind, e.subject_type, e.subject_id, e.ward_id, e.payload) t
    on e.hazard_id is null or e.sector_id is null;

alter table public.sectors enable row level security;
alter table public.ward_sectors enable row level security;
alter table public.category_hazards enable row level security;
revoke insert, update, delete, truncate on public.sectors, public.ward_sectors, public.category_hazards
  from anon, authenticated;
revoke all on function public.event_tags(text, text, text, text, jsonb) from public, anon;

commit;
