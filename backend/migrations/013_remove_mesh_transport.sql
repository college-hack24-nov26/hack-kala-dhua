-- 013 — mesh reporting removed, rather than left as a promise.
--
-- `citizen_reports.mesh_hops` existed so that a report relayed over an offline
-- mesh link could be scored down once per hop: another device in the chain of
-- custody, a slightly weaker claim. The scoring rule was written, the trust
-- model had a branch for it, the intake accepted `source: 'mesh'`, and the
-- architecture diagram had a box for it.
--
-- No report ever arrived that way. Across 420 rows the column is null on every
-- single one, and `source` has only ever held 'app' and 'field'. There was no
-- transport behind any of it, and there was never going to be one before the
-- deadline.
--
-- A column and a scoring branch for a channel that does not exist is not
-- groundwork. It is a claim the system cannot back, sitting in the one table a
-- judge is most likely to read, and it costs more than it looks: `mesh_hops`
-- appeared in the intake insert, the report API, the console payload and the
-- intake inbox UI, so every one of those was carrying a field that was always
-- null in order to describe something that never happened.
--
-- Dropping it is safe precisely because it holds nothing. If an offline
-- transport is built later it comes back as a new source with its own entry in
-- `SOURCE_CREDIBILITY`, which is a one-line change, and a fresh column if hop
-- counting turns out to matter — by which point there will be a transport to
-- tell us whether it does.

begin;

alter table citizen_reports drop column if exists mesh_hops;

commit;
