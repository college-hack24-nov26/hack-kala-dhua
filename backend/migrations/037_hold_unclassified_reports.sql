-- 037: an unclassified report never opens an incident.
--
-- Until now a report nothing could classify ("Not yet classified",
-- unknown_report) opened an incident and went on the command wall. The
-- simulated structural-monitor cameras made it worse: their "collapse"
-- detections were mapped to unknown_report, so every ten minutes one opened or
-- fed a holding incident. The intake now holds such reports (verification
-- 'pending', no incident) until an officer classifies them
-- (POST /reports/{id}/classify), and camera collapses are structural damage.
--
-- This migration:
--   1. lets unsigned mesh packets in (the trust scorer already scores
--      'mesh_unsigned' lower; the check constraint refused them outright, so
--      such a packet failed at insert);
--   2. releases the open "Not yet classified" incidents: their reports go back
--      to held / pending, their needs and links are removed, assignments to
--      them are released, and the incident is closed with an event saying why.
-- Additive and idempotent. Run it once in the SQL editor.

begin;

-- 1. unsigned mesh packets
alter table citizen_reports drop constraint if exists reports_source_check;
alter table citizen_reports add constraint reports_source_check
  check (source = any (array['app','field','agency','mesh','mesh_unsigned','sensor','phone','sim']));

-- 2. release open unclassified incidents
create temporary table _unk on commit drop as
  select id from incidents
   where category = 'unknown_report' and status <> 'resolved';

update citizen_reports r
   set incident_id = null,
       verification_status = case when r.verification_status = 'rejected' then 'rejected' else 'pending' end
 where r.incident_id in (select id from _unk);

delete from report_links  where incident_id in (select id from _unk);
delete from incident_needs where incident_id in (select id from _unk);

update assignments a
   set status = 'cancelled'
 where a.incident_id in (select id from _unk)
   and a.status::text not in ('complete', 'cancelled');

update incidents i
   set status = 'resolved', report_count = 0, updated_at = now()
 where i.id in (select id from _unk);

insert into events (city_id, occurred_at, kind, actor, subject_type, subject_id, ward_id, payload)
select i.city_id, now(), 'incident.resolved', 'system:migration-037', 'incident', i.id::text, i.ward_id,
       jsonb_build_object('reason',
         'Closed: it was opened by a report nothing could classify. Unclassified reports are now held for an officer and never open an incident; its reports are back in the inbox under "Needs classifying".')
  from incidents i where i.id in (select id from _unk);

commit;

-- Check: should return 0
-- select count(*) from incidents where category = 'unknown_report' and status <> 'resolved';
