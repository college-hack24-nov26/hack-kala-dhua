-- What a decision needs in order to be carried out, and who may carry it out.
--
-- Two gaps the Commissioner Copilot walks straight into.
--
-- First: a decision recorded *what* was authorised and never *what it would
-- act on*. "Move Ambulance 4 to Kothrud" was a sentence, and approving it
-- twenty minutes later left the ambulance where it was, because nothing
-- downstream knew which ambulance. `params` carries that, stored at proposal
-- time rather than re-derived at approval time — a decision approved late must
-- move the unit it named, not whichever unit the world would nominate now.
--
-- Second: the delegation matrix had no clause for the two actions a command
-- console proposes most. `reallocate_unit` is added to the clause that already
-- governs moving municipal equipment, because it is the same delegation and
-- inventing a second one would be dishonest. `request_mutual_aid` has no
-- municipal clause to sit under, so it gets an explicit deployment policy of
-- our own — named as ours, not dressed up as a citation.

alter table public.decisions
  add column if not exists params jsonb not null default '{}'::jsonb;

comment on column public.decisions.params is
  'What the executor needs to carry this out: which unit, which incident, '
  'which ward. Recorded when proposed so a late approval acts on what was '
  'actually decided.';

-- Moving a municipal unit is the same delegation as prepositioning one.
update public.policy_clauses
   set authorises = array(select distinct unnest(authorises || array['reallocate_unit']))
 where id = 'pol-2';

-- Asking another organisation for its units is not in the municipal plan, so
-- this is a deployment policy and says so. max_severity_without_escalation = 0
-- means it always waits for a person, which is the point: committing somebody
-- else's fleet is not a decision this system gets to make.
insert into public.policy_clauses
  (id, clause, source, delegated_to, body, max_severity_without_escalation, authorises, city_id)
values (
  'pol-8',
  'Indradhanu deployment policy MA-1',
  'Indradhanu deployment configuration (not a statutory instrument)',
  'Municipal Commissioner',
  'A request for another agency''s resources commits an organisation outside '
  'this corporation. It is raised by the Commissioner or their delegate, never '
  'automatically, whatever the projected shortfall. The system may identify the '
  'shortfall, name the agency that holds the capability, and draft the request.',
  0,
  array['request_mutual_aid'],
  'pune'
)
on conflict (id) do update
   set clause = excluded.clause,
       source = excluded.source,
       delegated_to = excluded.delegated_to,
       body = excluded.body,
       max_severity_without_escalation = excluded.max_severity_without_escalation,
       authorises = excluded.authorises;
