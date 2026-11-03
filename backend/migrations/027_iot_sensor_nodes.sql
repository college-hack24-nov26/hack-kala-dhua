-- 027 — LoRa sensor nodes and their readings.
--
-- A field node (Arduino + gas, temperature, sound, piezo, IMU, tilt) sends a
-- reading every ~2 s over LoRa to a gateway Arduino on the command-centre
-- laptop; `hardware/lora/lora_bridge.py` posts it to `POST /iot/observations`.
--
--   sensor_nodes     one row per node: where it is, its learned quiet baseline,
--                    and its latest scores
--   sensor_readings  every reading, raw values beside the scores derived from
--                    them, so the analytics page can rebuild any moment and an
--                    officer can see what a score was made of
--
-- Columns rather than a long (sensor_type, value) table: this hardware has a
-- fixed set of channels and every query reads several at once. Anything a
-- future node adds goes in `extra`.

begin;

create table if not exists public.sensor_nodes (
  id               text        primary key,
  city_id          text        not null default 'pune',
  label            text,
  lat              double precision,
  lon              double precision,
  ward_id          text,
  gateway_id       text,
  simulated        boolean     not null default false,
  first_seen       timestamptz not null default now(),
  last_seen        timestamptz not null default now(),
  last_seq         bigint,
  readings         bigint      not null default 0,
  lost             bigint      not null default 0,
  rssi             integer,
  snr              real,
  baseline         jsonb       not null default '{}'::jsonb,
  state            jsonb       not null default '{}'::jsonb,
  escalated        jsonb       not null default '{}'::jsonb   -- kind -> last escalation time
);

create table if not exists public.sensor_readings (
  id            bigint generated always as identity primary key,
  node_id       text        not null references public.sensor_nodes (id) on delete cascade,
  city_id       text        not null default 'pune',
  observed_at   timestamptz not null default now(),
  seq           bigint,
  uptime_s      integer,
  lat           double precision,
  lon           double precision,
  mq2           real,
  mq135         real,
  temp_c        real,
  tilt_deg      real,
  gyro_dps      real,
  vib_g         real,
  mic           real,
  piezo         real,
  knocks        smallint,
  tilt_sw       smallint,
  pir           smallint,
  rssi          integer,
  snr           real,
  human         real,          -- 0..1 likelihood a person is near the node
  structural    real,          -- 0..1 structural movement risk
  environmental real,          -- 0..1 gas / heat hazard
  overall       real,          -- 0..1 what the heatmap shows by default
  evidence      jsonb       not null default '{}'::jsonb,  -- per-channel contributions
  flags         text[]      not null default '{}',
  extra         jsonb       not null default '{}'::jsonb,
  raw           text
);

create index if not exists sensor_readings_node_time_idx
  on public.sensor_readings (node_id, observed_at desc);
create index if not exists sensor_readings_city_time_idx
  on public.sensor_readings (city_id, observed_at desc);
create index if not exists sensor_readings_flagged_idx
  on public.sensor_readings (city_id, observed_at desc) where flags <> '{}';

-- Same stance as 014: the API is the only door. Nothing for anon/authenticated
-- through PostgREST; the backend connects as the owner.
alter table public.sensor_nodes    enable row level security;
alter table public.sensor_readings enable row level security;
revoke all on public.sensor_nodes, public.sensor_readings from anon, authenticated;

commit;
