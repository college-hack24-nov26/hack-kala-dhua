-- 038: agency handoffs get an answer, and the agent acts on it.
--
-- Until now a request to another agency sat at "Waiting for a reply" forever
-- unless an officer clicked a button that played the other agency's part. The
-- other agency now replies (app/ops/agency_replies.py): a reply drawn at random
-- from a set we wrote ourselves (accept, partial, delayed, needs info, decline
-- for capacity, decline for jurisdiction), and the coordinator agent takes the
-- next step from it: staging the units it was given, asking the next agency for
-- the remainder, answering the question, or escalating.
--
-- Columns on agency_requests:
--   reply          the latest thing the other agency said
--   reply_kind     which kind of reply it was (drives the next step)
--   next_step      what the agent did about it, in words
--   replies        the whole exchange, [{at, who, kind, text}]
--   reply_due_at   when the other agency answers next (null: nothing pending)
--   followup_of    the request this one was raised because of
--   units          the units the request put in the fleet
-- Additive and idempotent. Run it once in the SQL editor.

begin;

alter table agency_requests add column if not exists reply        text;
alter table agency_requests add column if not exists reply_kind   text;
alter table agency_requests add column if not exists next_step    text;
alter table agency_requests add column if not exists replies      jsonb not null default '[]'::jsonb;
alter table agency_requests add column if not exists reply_due_at timestamptz;
alter table agency_requests add column if not exists followup_of  uuid references agency_requests(id) on delete set null;
alter table agency_requests add column if not exists units        text[] not null default '{}';

create index if not exists agency_requests_reply_due
  on agency_requests (reply_due_at) where reply_due_at is not null;

-- Requests already waiting get an answer within a few minutes of deploy.
update agency_requests
   set reply_due_at = now() + (random() * interval '2 minutes')
 where status in ('requested', 'acknowledged')
   and reply_due_at is null
   and not exists (select 1 from surge_aid a where a.request_id = agency_requests.id);

commit;
