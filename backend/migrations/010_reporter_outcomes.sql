-- Closing the loop on trust.
--
-- `reporter_reliability` computes a Wilson lower bound over each reporter's
-- history, and `trust.score()` reads it as one of seven components. The
-- arithmetic was right and the input was circular: the only thing that had ever
-- set `verification_status` was the trust score itself. A reporter was
-- "reliable" because the scorer had liked their earlier reports, which is not
-- evidence about the world — it is the model agreeing with itself, and over a
-- long run it amplifies its own first impression of somebody.
--
-- What was missing is the one signal a control room actually produces: an
-- officer, or a crew on scene, saying "yes, that was real" or "no, there was
-- nothing there". That is a label. It is the only label this system will ever
-- have, and it was being thrown away.
--
-- `outcome` records it, and nothing but a person writes it. Where a person has
-- ruled, their ruling counts; where nobody has, the automatic status still
-- counts, so the loop starts working immediately rather than after the first
-- hundred verdicts.

alter table public.citizen_reports
  add column if not exists outcome text
    check (outcome is null or outcome in ('confirmed', 'false')),
  add column if not exists outcome_note text,
  add column if not exists outcome_by text,
  add column if not exists outcome_at timestamptz;

comment on column public.citizen_reports.outcome is
  'Set only by a person who found out: confirmed = the report was real, false '
  '= it was not. Null means nobody has ruled. This is the only ground truth '
  'the system has and it overrides the automatic verification status when '
  'computing reporter reliability.';

create index if not exists citizen_reports_outcome_idx
  on public.citizen_reports (reporter_id) where outcome is not null;

-- Reliability, now with the human verdict taking precedence per row.
-- Dropped and recreated rather than replaced: a new column in the middle of the
-- select list is not something `create or replace view` will accept.
drop view if exists public.reporter_reliability;

create view public.reporter_reliability as
with scored as (
  select
    reporter_id,
    case
      when outcome = 'confirmed' then 1
      when outcome = 'false'     then 0
      when verification_status = 'auto_confirmed' then 1
      when verification_status in ('quarantined', 'rejected') then 0
      else null
    end                                   as good,
    (outcome is not null)::int            as judged
  from public.citizen_reports
  where reporter_id is not null
)
select
  reporter_id,
  count(*)::integer                                    as total,
  count(*) filter (where good = 1)::integer            as confirmed,
  count(*) filter (where good = 0)::integer            as rejected,
  sum(judged)::integer                                 as human_verdicts,
  case when count(*) filter (where good is not null) = 0 then 0.5
       else
         (count(*) filter (where good = 1)::numeric + 1.9208)
           / (count(*) filter (where good is not null)::numeric + 3.8416)
         - 1.96 * sqrt(
             count(*) filter (where good = 1)::numeric
             * count(*) filter (where good = 0)::numeric
             / greatest(1, count(*) filter (where good is not null))::numeric
             + 0.9604
           ) / (count(*) filter (where good is not null)::numeric + 3.8416)
  end                                                  as reliability
from scored
group by reporter_id;

comment on view public.reporter_reliability is
  'Wilson lower bound on the share of a reporter''s reports that turned out '
  'real. A human verdict on a report overrides the automatic status for that '
  'row; `human_verdicts` says how much of the figure is actual ground truth.';
