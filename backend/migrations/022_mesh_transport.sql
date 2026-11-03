-- 022 — the offline mesh, for real this time.
--
-- 013 removed `mesh_hops` because no transport existed behind it. This adds the
-- transport's own tables instead of a column on reports:
--
--   mesh_messages  every IDX1 packet a gateway handed us, verified or refused,
--                  and what it became (a report, a road block, an ack). The
--                  packet id is unique, so the same report arriving through
--                  three gateways is one row and one report.
--   mesh_outbox    what the control room wants said on the mesh: alerts,
--                  dispatches, cancellations, road blocks. Gateways pull it,
--                  send it, and ack it.
--   mesh_nodes     camera nodes, gateways and phones we have heard from, with
--                  where they were and when.
--   mesh_state     cursors (which event the outbox has read up to).
--
-- A report that arrives over the mesh still enters through
-- `intake.receive`, with source 'mesh' (or 'sensor' for a camera node), and is
-- trust-scored, deduplicated and gated exactly like one typed into the app.

begin;

create table if not exists public.mesh_messages (
  id           bigint generated always as identity primary key,
  packet_id    text        not null,
  type         char(1)     not null,
  node_id      text,
  gateway_id   text,
  hops         smallint,
  body         jsonb       not null,
  signed       boolean     not null default false,
  verified     boolean     not null default false,
  occurred_at  timestamptz,
  received_at  timestamptz not null default now(),
  outcome      text,          -- report | linked | held | road_block | ack | heartbeat | refused
  outcome_ref  text,          -- report id / incident id / road block id
  error        text,
  unique (packet_id, type)
);
create index if not exists mesh_messages_recent_idx on public.mesh_messages (received_at desc);

create table if not exists public.mesh_outbox (
  id              bigint generated always as identity primary key,
  city_id         text        not null default 'pune',
  kind            char(1)     not null,
  text            text        not null,
  body            jsonb       not null,
  ward_id         text,
  lat             double precision,
  lon             double precision,
  radius_m        integer,
  channel         text        not null default '#indradhanu',
  priority        smallint    not null default 3,
  status          text        not null default 'pending'
                  check (status in ('pending', 'sent', 'acked', 'expired')),
  attempts        smallint    not null default 0,
  source_event_id bigint      unique,
  created_at      timestamptz not null default now(),
  sent_at         timestamptz,
  sent_by         text,
  acked_at        timestamptz,
  expires_at      timestamptz not null default now() + interval '2 hours'
);
create index if not exists mesh_outbox_pending_idx
  on public.mesh_outbox (status, priority desc, id) where status in ('pending', 'sent');

create table if not exists public.mesh_nodes (
  id          text        primary key,
  kind        text        not null default 'phone',  -- camera | gateway | phone | sensor
  label       text,
  lat         double precision,
  lon         double precision,
  ward_id     text,
  last_seen   timestamptz not null default now(),
  meta        jsonb       not null default '{}'::jsonb
);

create table if not exists public.mesh_state (
  key    text primary key,
  value  jsonb not null
);

alter table public.mesh_messages enable row level security;
alter table public.mesh_outbox   enable row level security;
alter table public.mesh_nodes    enable row level security;
alter table public.mesh_state    enable row level security;
revoke all on public.mesh_messages, public.mesh_outbox, public.mesh_nodes,
              public.mesh_state from anon, authenticated;

commit;
