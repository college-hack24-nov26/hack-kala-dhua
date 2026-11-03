-- 028 — Scale: a leader lease for singleton loops, and LLM usage rollups.
--
-- service_leases
--   The event router tails the event log and re-plans. With one replica that
--   is fine; with N replicas every one of them re-plans the same change. The
--   lease makes exactly one replica the router: it renews every few seconds,
--   and if it dies another takes over after `expires_at`. It works through
--   the transaction pooler (a plain UPDATE), unlike an advisory lock, and it
--   carries the router's cursor so a new leader resumes where the old stopped.
--
-- llm_usage_minutely
--   Token spend per task per minute, flushed by the API. The live numbers are
--   in /status/llm/usage; this is the history a cost dashboard reads.
--
-- Additive only. Service role writes; nobody else reads or writes.

begin;

create table if not exists public.service_leases (
  name        text        primary key,
  holder      text        not null,
  expires_at  timestamptz not null,
  meta        jsonb       not null default '{}'::jsonb,
  updated_at  timestamptz not null default now()
);

create table if not exists public.llm_usage_minutely (
  minute       timestamptz not null,
  task         text        not null,
  calls        integer     not null default 0,
  model_calls  integer     not null default 0,
  cache_saved  integer     not null default 0,
  degraded     integer     not null default 0,
  tokens_in    bigint      not null default 0,
  tokens_out   bigint      not null default 0,
  cost_usd     numeric(12, 6),
  primary key (minute, task)
);

alter table public.service_leases enable row level security;
alter table public.llm_usage_minutely enable row level security;
revoke all on public.service_leases from anon, authenticated;
revoke all on public.llm_usage_minutely from anon, authenticated;

commit;
