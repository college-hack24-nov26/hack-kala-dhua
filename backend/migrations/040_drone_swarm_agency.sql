-- Data-only registration of the drone swarm as an EXTERNAL AGENCY. Patch proposal against DisruptionOps 1697407
-- (migrations 001-036). Safe on any database that has run 001-036; idempotent.
--
-- Why an agency and not a resource kind
-- -------------------------------------
-- The swarm is simulated and flies its own aircraft. If a 'drone' row were put in resource_kinds and resources,
-- the CP-SAT planner would allocate it like a road unit: agents/replan.py:_air_matrix() deliberately skips
-- kind == 'drone' (line 933: `p.mode != "air" or u.kind == "drone"`), so a drone would be priced with the ROAD
-- travel matrix. nav/profiles.py already has a 'drone' profile (aerial_survey, survivor_search, payload_drop,
-- comms_relay) that is not used by the solver. So aerial work is requested through the existing inter-agency
-- workflow (POST /api/v1/agency-requests, api/v1/reports.py:281) and answered by the swarm, not by the solver.
--
-- The capabilities below are NOT in incident_category_needs and no resource kind provides them, so the solver
-- never sees an unmet demand for them. They exist so agency_requests.capability_id (a foreign key) is valid.
begin;

insert into capabilities (id, label, description) values
  ('aerial_assessment', 'Aerial assessment', 'A drone flies to the site, confirms what is there and sends a briefing'),
  ('supply_drop',       'Drone supply drop', 'Light relief items (water, food, first-aid) dropped to people who cannot be reached yet'),
  ('relay',             'Comms relay',       'A drone holds position as a radio / mesh relay over a dead zone')
on conflict (id) do nothing;

insert into agencies (id, city_id, name, short_name, kind, jurisdiction, contact, active) values
  ('drone-swarm', 'pune', 'Drone swarm (simulated aerial assessment and light supply)', 'Drone swarm', 'private', 'city',
   '{"simulation_only": true, "protocol": "disruptionops.swarm.v2"}', true)
on conflict (id) do nothing;

insert into agency_capabilities (agency_id, capability_id) values
  ('drone-swarm', 'aerial_assessment'),
  ('drone-swarm', 'supply_drop'), ('drone-swarm', 'relay')
on conflict do nothing;

commit;
