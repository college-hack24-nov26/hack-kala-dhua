-- 029: a disaster zone around PCCOE (Akurdi–Nigdi, Pimpri-Chinchwad).
--
-- PCCOE (18.6527, 73.7624) is 11.4 km from the nearest ward the deployment had
-- (Aundh), so a report from the campus was pinned to Aundh and planned with
-- Aundh's units. This adds the Pimpri-Chinchwad (PCMC) side of the Pune region
-- the same way 026 added Ghaziabad: wards, local units, lifelines, and the
-- reports already filed from here re-homed to the ward they are actually in.
--
-- Positions are from OpenStreetMap (Nominatim, 2026-10-10): locality centres,
-- fire stations, hospitals and schools as mapped there. Ward populations and
-- elevations are PLANNING ESTIMATES for the demo, not census figures; replace
-- them with PCMC ward data before relying on the demand weights.
--
-- Hazard setting: the Pawana runs Ravet -> Chinchwad -> Pimpri -> Kasarwadi, so
-- the Ravet, Chinchwad Gaon and Pimpri wards are the flood-prone ones; MIDC
-- Bhosari/Chikhli carries the industrial fire and gas risk. The scenario
-- `pccoe_pawana_night` (app/demo/scenarios.py) exercises all of it through the
-- real intake. Ward shapes are Voronoi cells clipped to the area served, so
-- every point in it is inside exactly one ward; the envelope does not touch
-- any existing ward (checked: Aundh tops out at 18.570 N, Dighi–Bhosari starts
-- at 73.854 E).
--
-- Additive, plus the re-homing updates. Region stays 'pune' (lng < 75.5).

begin;

insert into agencies (id, city_id, name, short_name, kind, jurisdiction, contact, active)
values ('pcmc', 'pune', 'Pimpri Chinchwad Municipal Corporation', 'PCMC', 'government', 'city', '{}', true),
       ('pcmcfire', 'pune', 'PCMC Fire Brigade', 'PCMC Fire', 'government', 'city', '{}', true)
on conflict (id) do nothing;

-- ------------------------------------------------------------------ wards ---
create temporary table pc_w (id text, number text, name text, lng float8, lat float8,
                             population int, elderly numeric, elev numeric);
insert into pc_w values
 ('w-pc-01','P01','Akurdi–Pradhikaran (PCCOE)',    73.7624, 18.6527, 64000, 0.09, 586),
 ('w-pc-02','P02','Nigdi',                         73.7773, 18.6598, 71000, 0.11, 584),
 ('w-pc-03','P03','Ravet (Pawana bank)',           73.7451, 18.6433, 58000, 0.07, 571),
 ('w-pc-04','P04','Tathawade–Punawale',            73.7502, 18.6154, 62000, 0.06, 574),
 ('w-pc-05','P05','Chinchwad Gaon–Walhekarwadi',   73.7804, 18.6294, 83000, 0.10, 568),
 ('w-pc-06','P06','Chinchwad Station',             73.7915, 18.6395, 76000, 0.10, 572),
 ('w-pc-07','P07','Pimpri',                        73.8025, 18.6230, 92000, 0.10, 565),
 ('w-pc-08','P08','Thergaon',                      73.7729, 18.6093, 69000, 0.09, 570),
 ('w-pc-09','P09','Wakad',                         73.7644, 18.6022, 88000, 0.06, 573),
 ('w-pc-10','P10','Kasarwadi',                     73.8209, 18.6081, 54000, 0.09, 562),
 ('w-pc-11','P11','Chikhli–MIDC',                  73.8180, 18.6700, 74000, 0.07, 589),
 ('w-pc-12','P12','Dehu Road–Kiwale',              73.7343, 18.6800, 46000, 0.08, 592),
 ('w-pc-13','P13','Talawade',                      73.7908, 18.6987, 38000, 0.07, 596),
 ('w-pc-14','P14','Pimple Saudagar',               73.7978, 18.5982, 79000, 0.08, 563);

with env as (select extensions.ST_MakeEnvelope(73.715, 18.585, 73.845, 18.712, 4326) g),
cells as (
  select (extensions.ST_Dump(extensions.ST_VoronoiPolygons(
            extensions.ST_Collect(extensions.ST_SetSRID(extensions.ST_MakePoint(lng, lat), 4326)),
            0.0, (select g from env)))).geom as cell
    from pc_w
)
insert into wards (id, number, name, boundary, centroid, population, elderly_share,
                   elevation_m, area_sq_km, city_id)
select w.id, w.number, w.name,
       extensions.ST_Intersection(c.cell, (select g from env))::extensions.geography,
       extensions.ST_SetSRID(extensions.ST_MakePoint(w.lng, w.lat), 4326)::extensions.geography,
       w.population, w.elderly, w.elev,
       round((extensions.ST_Area(extensions.ST_Intersection(c.cell, (select g from env))::extensions.geography) / 1e6)::numeric, 1),
       'pune'
  from pc_w w
  join cells c on extensions.ST_Contains(c.cell, extensions.ST_SetSRID(extensions.ST_MakePoint(w.lng, w.lat), 4326))
on conflict (id) do nothing;

-- -------------------------------------------------------------- resources ---
-- Stations and hospitals at their mapped positions; how many units each holds
-- is a demo fleet, not PCMC's actual inventory.
create temporary table pc_r (id text, kind text, label text, operator text, agency text,
                             capacity int, lng float8, lat float8);
insert into pc_r values
 ('PC-FIRE-01','fire_engine','Fire tender 01 (Pradhikaran stn, Nigdi)','PCMC Fire Brigade','pcmcfire',6,73.77174,18.65925),
 ('PC-FIRE-02','fire_engine','Fire tender 02 (Pradhikaran stn, Nigdi)','PCMC Fire Brigade','pcmcfire',6,73.77180,18.65930),
 ('PC-FIRE-03','fire_engine','Fire tender 03 (Main PCMC stn, Pimpri)','PCMC Fire Brigade','pcmcfire',6,73.81805,18.62276),
 ('PC-FIRE-04','fire_engine','Hydraulic platform 04 (Main PCMC stn)','PCMC Fire Brigade','pcmcfire',4,73.81810,18.62280),
 ('PC-FIRE-05','fire_engine','Fire tender 05 (Thergaon stn)','PCMC Fire Brigade','pcmcfire',6,73.77881,18.61219),
 ('PC-FIRE-06','fire_engine','Fire tender 06 (Rahatani stn)','PCMC Fire Brigade','pcmcfire',6,73.78172,18.59624),
 ('PC-FIRE-07','fire_engine','Foam tender 07 (Chikhali stn, MIDC)','PCMC Fire Brigade','pcmcfire',6,73.81780,18.66441),
 ('PC-AMB-01','ambulance','108 Ambulance 01 (YCM Hospital)','108 EMS','health',4,73.82116,18.62203),
 ('PC-AMB-02','ambulance','108 Ambulance 02 (YCM Hospital)','108 EMS','health',4,73.82120,18.62210),
 ('PC-AMB-03','ambulance','Ambulance 03 (Aditya Birla Hospital)','Hospital fleet','health',3,73.77499,18.62584),
 ('PC-AMB-04','ambulance','Ambulance 04 (Lokmanya Hospital, Nigdi)','Hospital fleet','health',3,73.77320,18.65618),
 ('PC-AMB-05','ambulance','Ambulance 05 (D Y Patil Hospital)','Hospital fleet','health',3,73.82197,18.62317),
 ('PC-AMB-06','ambulance','Ambulance 06 (Global Hospital, Ravet)','Hospital fleet','health',3,73.75716,18.64646),
 ('PC-BOAT-01','boat','Rescue boat 01 (Pawana, Ravet)','PCMC Fire Brigade','pcmcfire',8,73.74560,18.64380),
 ('PC-BOAT-02','boat','Rescue boat 02 (Pawana, Morya Gosavi ghat)','PCMC Fire Brigade','pcmcfire',8,73.77844,18.62632),
 ('PC-BOAT-03','boat','Inflatable boat 03 (SDRF, Pimpri)','SDRF Maharashtra','sdrf',6,73.80250,18.62300),
 ('PC-TEAM-01','rescue_team','PCMC disaster cell team 01 (Pimpri)','PCMC Disaster Cell','pcmc',15,73.81800,18.62270),
 ('PC-TEAM-02','rescue_team','SDRF team 02 (Nigdi)','SDRF Maharashtra','sdrf',20,73.77170,18.65920),
 ('PC-TEAM-03','rescue_team','Civil Defence team 03 (Chinchwad)','PCMC Disaster Cell','pcmc',12,73.79150,18.63950),
 ('PC-PUMP-01','pump','Dewatering pump 01 (Akurdi station underpass)','PCMC Water Supply','pcmc',1,73.76535,18.64899),
 ('PC-PUMP-02','pump','Dewatering pump 02 (Chinchwad station)','PCMC Water Supply','pcmc',1,73.79150,18.63948),
 ('PC-PUMP-03','pump','Dewatering pump 03 (Pimpri station)','PCMC Water Supply','pcmc',1,73.80251,18.62295),
 ('PC-PUMP-04','pump','Dewatering pump 04 (Ravet)','PCMC Water Supply','pcmc',1,73.74506,18.64327),
 ('PC-PUMP-05','pump','Dewatering pump 05 (Kasarwadi)','PCMC Water Supply','pcmc',1,73.82087,18.60809),
 ('PC-JCB-01','jcb','JCB 01 (PCMC Works, Nigdi)','PCMC Works','pcmc',1,73.77063,18.66526),
 ('PC-JCB-02','jcb','JCB 02 (PCMC Works, Thergaon)','PCMC Works','pcmc',1,73.77290,18.60930),
 ('PC-TANK-01','water_tanker','Water tanker 01 (Nigdi)','PCMC Water Supply','pcmc',10000,73.77060,18.66520),
 ('PC-TANK-02','water_tanker','Water tanker 02 (Pimpri)','PCMC Water Supply','pcmc',10000,73.80260,18.62290),
 ('PC-TRUCK-01','supply_truck','Relief truck 01 (PCCOE staging)','PCMC Disaster Cell','pcmc',400,73.76238,18.65267),
 ('PC-TRUCK-02','supply_truck','Relief truck 02 (Chinchwad)','PCMC Disaster Cell','pcmc',400,73.78040,18.62940),
 ('PC-BUS-01','bus','Evacuation bus 01 (Bhakti Shakti depot)','PMPML','transport',45,73.77063,18.66526),
 ('PC-BUS-02','bus','Evacuation bus 02 (Bhakti Shakti depot)','PMPML','transport',45,73.77070,18.66530);

insert into resources (id, kind, label, operator, base_location, location, capacity, status,
                       city_id, agency_id, crew_available, fuel_pct, last_reported_at, updated_at)
select id, kind, label, operator,
       extensions.ST_SetSRID(extensions.ST_MakePoint(lng, lat), 4326)::extensions.geography,
       extensions.ST_SetSRID(extensions.ST_MakePoint(lng, lat), 4326)::extensions.geography,
       capacity, 'available', 'pune', agency, true, 85 + (abs(hashtext(id)) % 15), now(), now()
  from pc_r
on conflict (id) do nothing;

-- -------------------------------------------------------------- lifelines ---
create temporary table pc_l (id text, kind text, name text, lng float8, lat float8,
                             capacity int, casualties bool, specs text[], served int);
insert into pc_l values
 ('lf-pc-h1','hospital','YCM Hospital (PCMC), Pimpri',73.82116,18.62203,null,true,'{general,trauma,burns}',null),
 ('lf-pc-h2','hospital','Aditya Birla Memorial Hospital, Thergaon',73.77499,18.62584,null,true,'{general,trauma}',null),
 ('lf-pc-h3','hospital','Lokmanya Hospital, Nigdi',73.77320,18.65618,null,true,'{general,trauma}',null),
 ('lf-pc-h4','hospital','Dr D Y Patil Hospital, Pimpri',73.82197,18.62317,null,true,'{general,trauma,burns}',null),
 ('lf-pc-h5','hospital','Global Hospital, Ravet',73.75716,18.64646,null,true,'{general}',null),
 ('lf-pc-h6','hospital','Thergaon Hospital (PCMC)',73.78166,18.61338,null,true,'{general}',null),
 ('lf-pc-s1','shelter','PCCOE campus, Nigdi (shelter & relief staging)',73.76238,18.65267,800,false,'{}',null),
 ('lf-pc-s2','shelter','Jnana Prabodhini ground, Nigdi',73.76625,18.65879,400,false,'{}',null),
 ('lf-pc-s3','shelter','S B Patil Public School, Ravet',73.74602,18.65596,450,false,'{}',null),
 ('lf-pc-s4','shelter','Kamalnayan Bajaj High School, More Wasti',73.79867,18.65895,400,false,'{}',null),
 ('lf-pc-s5','shelter','St Ursula''s High School, Nigdi',73.77732,18.65712,350,false,'{}',null),
 ('lf-pc-s6','shelter','Mhalsakant School, Akurdi',73.77545,18.64839,350,false,'{}',null),
 ('lf-pc-r1','relief_centre','Morya Gosavi temple relief centre, Chinchwad',73.77844,18.62632,1000,false,'{}',180),
 ('lf-pc-r2','relief_centre','Ravet relief camp (D Y Patil Dnyanshanti School)',73.75948,18.64756,900,false,'{}',150),
 ('lf-pc-m1','medical_camp','Medical camp, Ravet',73.74600,18.64420,120,true,'{general}',60),
 ('lf-pc-p1','water_point','Water point, Bhakti Shakti depot',73.77063,18.66526,null,false,'{}',400);

insert into lifelines (id, kind, name, ward_id, location, capacity, occupancy, city_id,
                       accepts_casualties, status, specialities, supplies, supplies_baseline,
                       people_served_per_hour, occupancy_baseline, last_reported_at, opened_at)
select l.id, l.kind, l.name,
       (select w.id from wards w
         where w.id like 'w-pc-%'
         order by extensions.ST_Distance(w.centroid,
                  extensions.ST_SetSRID(extensions.ST_MakePoint(l.lng, l.lat), 4326)::extensions.geography)
         limit 1),
       extensions.ST_SetSRID(extensions.ST_MakePoint(l.lng, l.lat), 4326)::extensions.geography,
       l.capacity, 0, 'pune', l.casualties, 'open', l.specs,
       case when l.kind in ('relief_centre','food_kitchen','shelter')
            then '{"blankets":400,"food_packets":800,"medical_kits":40,"water_litres":6000}'::jsonb
            else '{}'::jsonb end,
       case when l.kind in ('relief_centre','food_kitchen','shelter')
            then '{"blankets":400,"food_packets":800,"medical_kits":40,"water_litres":6000}'::jsonb
            else '{}'::jsonb end,
       l.served, 0, now(), now()
  from pc_l l
on conflict (id) do nothing;

-- -------------------------------------------------------- existing reports ---
-- Anything already filed from inside the new zone was attached to a distant
-- ward. Re-home it (and the incidents it opened) to the ward it is in.
update citizen_reports r
   set ward_id = (select w.id from wards w where w.id like 'w-pc-%'
                   order by extensions.ST_Distance(w.boundary, r.location) limit 1)
 where extensions.ST_Covers(extensions.ST_MakeEnvelope(73.715, 18.585, 73.845, 18.712, 4326)::extensions.geography,
                            r.location);

update incidents i
   set ward_id = (select w.id from wards w where w.id like 'w-pc-%'
                   order by extensions.ST_Distance(w.boundary, i.location) limit 1),
       updated_at = now()
 where extensions.ST_Covers(extensions.ST_MakeEnvelope(73.715, 18.585, 73.845, 18.712, 4326)::extensions.geography,
                            i.location);

drop table pc_w;
drop table pc_r;
drop table pc_l;

commit;
