-- 041 evidence media: keep every actual image and clip an agent decision was based on.
--
-- feed_media (033) only took small photos from citizens, cameras, drones and crews. The audit trail
-- now also keeps:
--   * satellite products (Copernicus GFM flood mask, likelihood, exclusion mask, Sentinel-1 radar crop)
--   * short video clips from edge cameras (the seconds before a VLM alert), mp4 / webm / gif
--   * sensor plots and drone frames
-- Additive and idempotent: only widens three check constraints and adds two optional columns.

begin;

alter table public.feed_media drop constraint if exists feed_media_source_check;
alter table public.feed_media add constraint feed_media_source_check
  check (source in ('citizen', 'camera', 'drone', 'crew', 'satellite', 'sensor', 'agent'));

alter table public.feed_media drop constraint if exists feed_media_mime_check;
alter table public.feed_media add constraint feed_media_mime_check
  check (mime like 'image/%' or mime like 'video/%');

alter table public.feed_media drop constraint if exists feed_media_bytes_check;
alter table public.feed_media add constraint feed_media_bytes_check
  check (octet_length(bytes) <= 4000000);

-- What the image is (sensor, processing level) and what was extracted from it, for the audit view.
alter table public.feed_media add column if not exists kind text;
alter table public.feed_media add column if not exists meta jsonb not null default '{}'::jsonb;

commit;
