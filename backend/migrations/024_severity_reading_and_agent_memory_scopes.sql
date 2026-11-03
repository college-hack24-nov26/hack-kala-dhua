-- 024 — severity read from the words, and per-agent memory with a ledger.
--
-- citizen_reports.assessed_severity / severity_assessment: what the severity
-- reader (app/incidents/severity.py: keywords, then a zero-shot classifier,
-- then a guarded LLM, all bounded) made of the report text, and why. An
-- incident's severity is now the worst trusted reading, not only its category.
--
-- agent_memory.namespace: one namespace per agent in the LangGraph cycle
-- (triage, rescue, medical, logistics, planner, gate, commander) plus
-- `orders` (officers write, every agent reads). Access is enforced in
-- app/agents/agent_memory.py; every read, write and refusal is written to
-- agent_memory_ledger.

begin;

alter table public.citizen_reports
  add column if not exists assessed_severity smallint check (assessed_severity between 1 and 5),
  add column if not exists severity_assessment jsonb;

alter table public.agent_memory
  add column if not exists namespace text;
create index if not exists agent_memory_namespace_idx
  on public.agent_memory (city_id, namespace, created_at desc) where namespace is not null;

create table if not exists public.agent_memory_ledger (
  id          bigint generated always as identity primary key,
  at          timestamptz not null default now(),
  agent       text        not null,
  op          text        not null check (op in ('read', 'write', 'denied')),
  namespace   text        not null,
  memory_id   text,
  detail      text,
  run_id      text,
  city_id     text        not null default 'pune'
);
create index if not exists agent_memory_ledger_at_idx on public.agent_memory_ledger (at desc);
alter table public.agent_memory_ledger enable row level security;
revoke all on public.agent_memory_ledger from anon, authenticated;

commit;
