-- 020 — memory for the Copilot and the Incident Commander.
--
-- Until now every question to the Copilot started from nothing. "Apply the
-- second one", "and what about Baner?", "do the same for Kothrud" had no
-- referent, an officer who said "keep Boat 2 in Kothrud tonight" had to say it
-- again to the next screen, and nothing learned in one incident was available
-- in the next.
--
-- Three kinds of memory, in two tables, and the split is deliberate:
--
--   copilot_turns     short-term. What was asked and answered in a session,
--                     so a follow-up can be resolved. Expires with the session.
--   agent_memory      long-term, one row per thing worth keeping:
--                       standing_order  an instruction from a named person
--                                       ("hold Boat 2 in Kothrud until 06:00")
--                       fact            something true about the city that is
--                                       not in another table ("the Aundh
--                                       underpass floods first")
--                       lesson          after-action: what happened last time
--                       episode         a summary of one Commander episode
--                       preference      how this officer wants answers
--
-- Retrieval is full-text plus recency plus importance, in SQL, so it works with
-- no embedding model. An `embedding` column is created only if pgvector is
-- available; nothing reads it yet and nothing breaks without it.
--
-- What memory is NOT allowed to do: carry a number the solver or the database
-- should supply. A memory can say "the officer asked that Boat 2 stay in
-- Kothrud"; it cannot say "Boat 2 is 4 minutes away". Every number in a
-- Copilot answer still comes from a tool call made at the time of the answer.

begin;

create table if not exists public.copilot_turns (
  id           bigint generated always as identity primary key,
  session_id   text        not null,
  city_id      text        not null default 'pune',
  actor        text        not null,
  role         text        not null check (role in ('user', 'assistant')),
  text         text        not null,
  intent       text,
  args         jsonb       not null default '{}'::jsonb,
  -- The ids an answer was about, so "that one" can be resolved without
  -- re-reading the prose: {"resource_ids": [...], "incident_ids": [...],
  -- "strategy_ids": [...], "ward_ids": [...]}
  refs         jsonb       not null default '{}'::jsonb,
  created_at   timestamptz not null default now()
);

create index if not exists copilot_turns_session_idx
  on public.copilot_turns (session_id, created_at desc);

create table if not exists public.agent_memory (
  id            uuid        primary key default gen_random_uuid(),
  city_id       text        not null default 'pune',
  scope         text        not null check (scope in
                  ('standing_order', 'fact', 'lesson', 'episode', 'preference')),
  content       text        not null check (length(content) between 3 and 2000),
  -- Structured form, when there is one: {"resource_id": "...", "ward_id": "...",
  -- "action": "hold"}. Standing orders that the planner enforces live in
  -- `operator_overrides` (migration 021); this row is the human-readable record
  -- of why, and links to it through `data.override_id`.
  data          jsonb       not null default '{}'::jsonb,
  ward_id       text,
  subject_type  text,
  subject_id    text,
  importance    smallint    not null default 3 check (importance between 1 and 5),
  source        text        not null default 'copilot',
  created_by    text        not null,
  created_at    timestamptz not null default now(),
  expires_at    timestamptz,
  -- Forgetting is a soft delete, so the audit trail keeps what was believed
  -- and when, and who withdrew it.
  active        boolean     not null default true,
  withdrawn_by  text,
  withdrawn_at  timestamptz,
  tsv           tsvector generated always as (
                  to_tsvector('simple', coalesce(content, '') || ' ' ||
                                        coalesce(ward_id, '') || ' ' ||
                                        coalesce(subject_id, ''))
                ) stored
);

create index if not exists agent_memory_tsv_idx on public.agent_memory using gin (tsv);
create index if not exists agent_memory_scope_idx
  on public.agent_memory (city_id, scope, active, created_at desc);
create index if not exists agent_memory_subject_idx
  on public.agent_memory (subject_type, subject_id) where active;

-- Ranked recall: text match, then importance, then recency. One function so the
-- backend and any future SQL client rank the same way.
create or replace function public.recall_memory(
  p_city   text,
  p_query  text,
  p_ward   text default null,
  p_limit  int  default 8
)
returns table (
  id uuid, scope text, content text, data jsonb, ward_id text,
  importance smallint, created_by text, created_at timestamptz, score real
)
language sql stable set search_path = public, pg_catalog as $$
  with q as (
    select case when coalesce(trim(p_query), '') = '' then null
                else websearch_to_tsquery('simple', p_query) end as tq
  )
  select m.id, m.scope, m.content, m.data, m.ward_id, m.importance,
         m.created_by, m.created_at,
         (coalesce(ts_rank(m.tsv, q.tq), 0) * 4
          + m.importance * 0.3
          + case when p_ward is not null and m.ward_id = p_ward then 1.5 else 0 end
          + case when m.scope = 'standing_order' then 2 else 0 end
          -- about a point lost per day
          - extract(epoch from (now() - m.created_at)) / 86400.0
         )::real as score
    from public.agent_memory m, q
   where m.city_id = p_city
     and m.active
     and (m.expires_at is null or m.expires_at > now())
     and (q.tq is null or m.tsv @@ q.tq or m.scope = 'standing_order'
          or (p_ward is not null and m.ward_id = p_ward))
   order by score desc
   limit greatest(1, least(p_limit, 50));
$$;
revoke execute on function public.recall_memory(text, text, text, int)
  from public, anon, authenticated;

-- Same posture as 014: the backend connects as the owner; nothing public writes.
alter table public.copilot_turns enable row level security;
alter table public.agent_memory  enable row level security;
revoke all on public.copilot_turns from anon, authenticated;
revoke all on public.agent_memory  from anon, authenticated;

commit;

-- Optional: vector column when pgvector is installed. Separate from the
-- transaction above so a missing extension cannot roll the tables back.
do $$
declare
  vschema text;
begin
  select n.nspname into vschema
    from pg_extension e join pg_namespace n on n.oid = e.extnamespace
   where e.extname = 'vector';
  if vschema is not null then
    execute format(
      'alter table public.agent_memory add column if not exists embedding %I.vector(768)',
      vschema);
  end if;
exception when others then
  raise notice 'pgvector column skipped: %', sqlerrm;
end $$;
