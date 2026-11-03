-- 023 — a call for help gets somebody sent, even when we cannot classify it.
--
-- 018 gave `unknown_report` no needs on purpose: an unclassified report should
-- not dispatch a *specific* vehicle (a pump for what might be a fire). But no
-- needs at all meant no unit at all, and "I need help right now" arriving over
-- the mesh sat on the map with nobody going. The honest middle is to send one
-- first responder to look: `field_assessment`, which any crew that can reach a
-- person on foot can do. Once they report back, the incident is reclassified
-- and the real need follows.
--
-- Also: plain calls for help ("need help", "help me", "sos", "emergency") are
-- now `person_stranded` keywords, so they get a rescue unit directly instead of
-- landing in the unknown pen at all.

begin;

insert into capabilities (id, label, description)
values ('field_assessment', 'Go and assess',
        'Send a first responder to see what an unclassified report actually is.')
on conflict (id) do nothing;

insert into resource_kind_capabilities (kind_id, capability_id, effectiveness)
values
  ('rescue_team', 'field_assessment', 1.0),
  ('ambulance',   'field_assessment', 0.8),
  ('fire_engine', 'field_assessment', 0.8),
  ('boat',        'field_assessment', 0.6)
on conflict do nothing;

insert into incident_category_needs (category_id, capability_id, qty_per_incident)
values ('unknown_report', 'field_assessment', 1)
on conflict do nothing;

insert into incident_category_keywords (category_id, phrase, weight)
values
  ('person_stranded', 'need help', 1.0),
  ('person_stranded', 'needs help', 1.0),
  ('person_stranded', 'help me', 1.0),
  ('person_stranded', 'please help', 1.0),
  ('person_stranded', 'help right now', 1.0),
  ('person_stranded', 'sos', 1.0),
  ('person_stranded', 'emergency', 0.8),
  ('person_stranded', 'save us', 1.0),
  ('person_stranded', 'मदत करा', 1.0),
  ('person_stranded', 'मदद करो', 1.0)
on conflict do nothing;

-- Incidents already open as unknown get the need now, so the next replan
-- covers them.
insert into incident_needs (incident_id, capability_id, required, updated_at)
select i.id, 'field_assessment', 1, now()
  from incidents i
 where i.category = 'unknown_report' and i.status <> 'resolved'
on conflict (incident_id, capability_id) do nothing;

commit;
