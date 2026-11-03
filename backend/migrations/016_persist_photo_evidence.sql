-- 016 — keep what the vision model actually saw.
--
-- Already applied to the live database (as Supabase migration
-- `016_persist_photo_evidence`).
--
-- Until now a photo analysis lived in an in-memory TTL cache keyed by a token,
-- was read exactly once when the report was filed, contributed a single float
-- to the trust score, and was then gone. Three things were wrong with that:
--
--   1. It did not survive a restart, so the evidence behind a trust score could
--      vanish while the score it produced stayed on the report.
--   2. An officer could see that a photo moved a report's trust and could not
--      see *why*. "The picture disagreed with the text" is a claim somebody has
--      to be able to check before they act on it, or decline to.
--   3. Nothing downstream could ever use it. A structured reading of every
--      photo the city received during a flood — water depth against a named
--      reference, people in water, time of day, image quality — is one of the
--      more valuable things this system produces, and it was being discarded
--      within seconds of being computed.
--
-- Stored as jsonb rather than columns because the contract in
-- `app/incidents/vision.py` is versioned and will gain fields; the scalar the
-- scorer actually consumed is lifted out beside it so it can be queried and
-- indexed without unpacking the document.

alter table citizen_reports
  add column if not exists photo_evidence  jsonb,
  add column if not exists photo_agreement numeric;

comment on column citizen_reports.photo_evidence is
  'Validated vision-model reading of the attached photo, in the shape of '
  'vision.PhotoEvidence. Null when there was no photo or nothing looked at it.';

comment on column citizen_reports.photo_agreement is
  'How well the photo agreed with what was reported, -1..1, from vision.assess. '
  'This is the number that moved the trust score''s evidence component.';

-- Partial: most reports have no photo, and an index over mostly-null rows is
-- wasted pages.
create index if not exists citizen_reports_photo_evidence_idx
    on citizen_reports using gin (photo_evidence)
 where photo_evidence is not null;

-- "Show me every report where the picture contradicted the text" — the query an
-- officer reviewing a suspected false-report campaign actually wants.
create index if not exists citizen_reports_photo_agreement_idx
    on citizen_reports (photo_agreement)
 where photo_agreement is not null;
