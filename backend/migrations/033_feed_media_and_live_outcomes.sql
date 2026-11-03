-- 033: photos behind the live feed, and everyday incidents as learning outcomes.
--
--   feed_media   small images (<= 400 KB) a citizen's phone or a camera node sent
--                with a report/detection, shown in the live feed next to the
--                VLM's reading of them (/mesh/detail). LoRa never carries images;
--                HTTPS uploads do.
--   incidents -> nav_outcomes
--                a fallen tree, a waterlogged or flooded road, a collapse or a
--                downed line is a road outcome whether or not it rained: when
--                such an incident is confirmed (or trusted >= 0.8) it is written
--                as "blocked" at its place; when it is resolved, as "open" again.
--                So everyday disruptions feed the re-learning loop, not storms
--                only.
--
-- Additive. RLS on; writes are server-side.

begin;

create table if not exists public.feed_media (
  id           bigint generated always as identity primary key,
  city_id      text not null default 'pune' references public.cities(id),
  source       text not null check (source in ('citizen', 'camera', 'drone', 'crew')),
  mime         text not null check (mime like 'image/%'),
  bytes        bytea not null check (octet_length(bytes) <= 400000),
  caption      text,
  report_id    uuid references public.citizen_reports(id) on delete set null,
  event_ref    text,
  photo_token  text,
  created_at   timestamptz not null default now()
);
create index if not exists feed_media_report_idx on public.feed_media (report_id);
create index if not exists feed_media_event_idx on public.feed_media (event_ref);
create index if not exists feed_media_token_idx on public.feed_media (photo_token);
alter table public.feed_media enable row level security;
revoke all on public.feed_media from anon, authenticated;

create or replace function public.incidents_to_outcomes()
returns trigger language plpgsql
security definer
set search_path = public
as $$
declare
  road_cats text[] := array['flooded_road', 'waterlogging', 'fallen_tree', 'structural_damage',
                            'power_line', 'blocked_drain'];
  became_sure boolean;
begin
  if new.location is null or not (new.category = any(road_cats)) then
    return new;
  end if;
  became_sure := (new.status in ('confirmed', 'dispatched', 'in_progress') or coalesce(new.trust_score, 0) >= 0.8)
             and (tg_op = 'INSERT'
                  or not (old.status in ('confirmed', 'dispatched', 'in_progress') or coalesce(old.trust_score, 0) >= 0.8));
  if new.status <> 'resolved' and became_sure then
    insert into nav_outcomes (city_id, sim_run_id, observed_at, location, radius_m, blocked, source,
                              confidence, ref_table, ref_id)
    values (new.city_id, new.sim_run_id, now(), new.location, 60, true, 'official',
            greatest(0.8, least(1.0, coalesce(new.trust_score, 0.8))), 'incidents', new.id::text);
  elsif tg_op = 'UPDATE' and new.status = 'resolved' and old.status <> 'resolved' then
    insert into nav_outcomes (city_id, sim_run_id, observed_at, location, radius_m, blocked, source,
                              confidence, ref_table, ref_id)
    values (new.city_id, new.sim_run_id, now(), new.location, 60, false, 'official', 0.9,
            'incidents', new.id::text);
  end if;
  return new;
end $$;

drop trigger if exists incidents_outcomes on public.incidents;
create trigger incidents_outcomes after insert or update of status, trust_score on public.incidents
  for each row execute function public.incidents_to_outcomes();

revoke all on function public.incidents_to_outcomes() from public, anon, authenticated;

commit;
