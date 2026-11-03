-- ============================================================================
-- 002  Convert taxonomy enum columns to text foreign keys.
--
-- Safe to run once 001 has seeded the reference tables: every operational table
-- is empty, and the seeded reference data (wards, lifelines, resources) uses
-- only values present in the new tables.
--
-- Not idempotent, unlike 001 and 003: dropping a type cannot be.
-- ============================================================================

drop view if exists current_ward_risk;
drop view if exists latest_hazard_runs;
alter table lifelines drop constraint if exists shelter_needs_capacity;

-- hazard_type -> hazard_types.id
alter table hazard_runs      alter column hazard type text using hazard::text;
alter table ward_risks       alter column hazard type text using hazard::text;
alter table incidents        alter column hazard type text using hazard::text;
alter table allocation_plans alter column hazard type text using hazard::text;
alter table decisions        alter column hazard type text using hazard::text;
alter table alerts           alter column hazard type text using hazard::text;
alter table agent_runs       alter column hazard type text using hazard::text;

alter table hazard_runs      add constraint hazard_runs_hazard_fk      foreign key (hazard) references hazard_types(id);
alter table ward_risks       add constraint ward_risks_hazard_fk       foreign key (hazard) references hazard_types(id);
alter table incidents        add constraint incidents_hazard_fk        foreign key (hazard) references hazard_types(id);
alter table allocation_plans add constraint allocation_plans_hazard_fk foreign key (hazard) references hazard_types(id);
alter table decisions        add constraint decisions_hazard_fk        foreign key (hazard) references hazard_types(id);
alter table alerts           add constraint alerts_hazard_fk           foreign key (hazard) references hazard_types(id);
alter table agent_runs       add constraint agent_runs_hazard_fk       foreign key (hazard) references hazard_types(id);

-- incident_category -> incident_categories.id
alter table incidents       alter column category      type text using category::text;
alter table citizen_reports alter column category      type text using category::text;
alter table citizen_reports alter column classified_as type text using classified_as::text;

alter table incidents       add constraint incidents_category_fk    foreign key (category)      references incident_categories(id);
alter table citizen_reports add constraint reports_category_fk      foreign key (category)      references incident_categories(id);
alter table citizen_reports add constraint reports_classified_as_fk foreign key (classified_as) references incident_categories(id);

-- resource_kind -> resource_kinds.id
alter table resources alter column kind type text using kind::text;
alter table resources add constraint resources_kind_fk foreign key (kind) references resource_kinds(id);

-- lifeline_kind -> lifeline_kinds.id
alter table lifelines alter column kind type text using kind::text;
alter table lifelines add constraint lifelines_kind_fk foreign key (kind) references lifeline_kinds(id);
alter table lifelines add constraint shelter_needs_capacity
  check (kind <> 'shelter' or capacity is not null);

-- agent_name -> plain text. The agent roster changes as the system grows and
-- should not need a type migration each time.
alter table agent_steps alter column agent type text using agent::text;

-- Retire the taxonomy enums. Lifecycle enums stay: incident_status,
-- assignment_status, task_status, decision_status, resource_status, app_role.
drop type if exists hazard_type;
drop type if exists incident_category;
drop type if exists resource_kind;
drop type if exists lifeline_kind;
drop type if exists agent_name;

-- Rebuild the views on the new column types.
create view latest_hazard_runs as
  select distinct on (hazard)
         id, hazard, started_at, finished_at, sources, mode, replay_of, created_at
    from hazard_runs
   order by hazard, started_at desc;

create view current_ward_risk as
  select r.id, r.run_id, r.ward_id, r.hazard, r.score, r.severity,
         r.lead_time_hours, r.confidence, r.population_at_risk, r.drivers,
         r.projection, r.created_at,
         w.number as ward_number, w.name as ward_name, w.population,
         w.elderly_share, w.elevation_m,
         extensions.ST_AsGeoJSON(w.centroid)::jsonb as centroid_geojson
    from ward_risks r
    join wards w on w.id = r.ward_id
    join latest_hazard_runs lr on lr.id = r.run_id;
