-- 015 — reliability that can learn from somebody who never signed in.
--
-- Already applied to the live database (as Supabase migration
-- `013_reporter_key_for_anonymous_reliability`; the numbering differs because
-- the repo and the Supabase migration history are separate sequences). Kept here
-- so a fresh database reaches the same shape.
--
-- Why this exists
-- ---------------
-- `reporter_id` is a foreign key to auth.users, so it can only ever hold a real
-- account. Virtually nobody reporting a flood is signed in, so it was null on
-- all 420 rows — and `reporter_reliability` filters `reporter_id is not null`.
-- The view therefore had zero rows in it, every reporter scored a flat 0.5
-- forever, and a feature described as "reporter reliability that learns" could
-- not learn anything at all.
--
-- `reporter_key` is the reliability identity, which is a different thing from an
-- account: 'user:<uuid>' for somebody signed in, 'device:<id>' for a phone that
-- has reported before. The device half is not identity in any legal sense — a
-- random id the app generates for itself, tied to no name, never joined to an
-- account, discarded with site data. It carries one fact: "this phone has
-- reported before, and here is how those turned out." That is the whole of what
-- the trust scorer needs, and considerably less than a phone number.

alter table citizen_reports
  add column if not exists reporter_key text;

update citizen_reports
   set reporter_key = 'user:' || reporter_id::text
 where reporter_id is not null
   and reporter_key is null;

-- Anonymous rows written before this shared the device id 'citizen-anon', which
-- is precisely what made them unusable. They are left null rather than lumped
-- under one key: a fabricated shared history is worse than no history, because
-- the scorer would believe it.
update citizen_reports
   set reporter_key = 'device:' || device_id
 where reporter_id is null
   and reporter_key is null
   and device_id is not null
   and device_id not in ('citizen-anon', 'anon');

create index if not exists citizen_reports_reporter_key_idx
    on citizen_reports (reporter_key)
 where reporter_key is not null;

drop view if exists reporter_reliability;

create view reporter_reliability as
with scored as (
  select
    reporter_key,
    -- Only counted once a human or the system has actually ruled on it.
    -- Everything else is null and drops out of the denominator, so nobody is
    -- punished for reports nobody has looked at yet.
    case
      when outcome = 'confirmed' then 1
      when outcome = 'false' then 0
      when verification_status = 'auto_confirmed' then 1
      when verification_status in ('quarantined', 'rejected') then 0
      else null::integer
    end as good,
    (outcome is not null)::integer as judged
  from citizen_reports
  where reporter_key is not null
)
select
  reporter_key,
  -- Still exposed for the screens that want to put a name to a key. Null for a
  -- device, which is the correct answer: there is no name.
  case when reporter_key like 'user:%'
       then substring(reporter_key from 6)::uuid
  end as reporter_id,
  count(*)::integer as total,
  count(*) filter (where good = 1)::integer as confirmed,
  count(*) filter (where good = 0)::integer as rejected,
  sum(judged)::integer as human_verdicts,
  -- Wilson lower bound at 95%, not a raw ratio. One correct report out of one
  -- is not a perfect reporter, and this is the arithmetic that says so: it
  -- starts everybody near 0.5 and moves only as evidence accumulates.
  case
    when count(*) filter (where good is not null) = 0 then 0.5
    else (count(*) filter (where good = 1)::numeric + 1.9208)
         / (count(*) filter (where good is not null)::numeric + 3.8416)
       - 1.96 * sqrt(
           count(*) filter (where good = 1)::numeric
           * count(*) filter (where good = 0)::numeric
           / greatest(1::bigint, count(*) filter (where good is not null))::numeric
           + 0.9604)
         / (count(*) filter (where good is not null)::numeric + 3.8416)
  end as reliability
from scored
group by reporter_key;
