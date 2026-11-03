-- 026: a second operating region around IPEC, Sahibabad (Ghaziabad, UP).
--
-- Reports sent from Ghaziabad were snapping to the nearest *Pune* ward
-- (Alandi, 1,200 km away) because every ward, unit and lifeline in the
-- deployment was in Pune. Nothing near the report could be allocated.
--
-- The region lives in the same deployment (city_id 'pune' is the tenant id the
-- whole stack defaults to), so intake, mesh, the planner and the console pick
-- it up with no new code paths: a report resolves to the ward it is in, the
-- planner's travel matrix makes the local units the cheapest, and the flood
-- datum is computed per region (queries.ward_features, same commit).
--
-- Ward shapes are Voronoi cells around each locality's centre, clipped to the
-- area served, so every point in the area is inside exactly one ward.

insert into agencies (id, city_id, name, short_name, kind, jurisdiction, contact, active)
values ('gmc', 'pune', 'Ghaziabad Municipal Corporation', 'GMC', 'government', 'city', '{}', true),
       ('upfs', 'pune', 'UP Fire & Emergency Services, Ghaziabad', 'UP Fire', 'government', 'district', '{}', true),
       ('ndrf8', 'pune', 'NDRF 8th Battalion, Ghaziabad', 'NDRF 8', 'government', 'national', '{}', true)
on conflict (id) do nothing;

-- ------------------------------------------------------------------ wards ---
create temporary table gzb_w (id text, number text, name text, lng float8, lat float8,
                              population int, elderly numeric, elev numeric);
insert into gzb_w values
 ('w-gzb-01','101','Sahibabad Site IV (IPEC)',     77.3440, 28.6600, 58200, 0.07, 212),
 ('w-gzb-02','102','Vaishali',                     77.3380, 28.6480, 96400, 0.10, 210),
 ('w-gzb-03','103','Kaushambi',                    77.3240, 28.6420, 71800, 0.11, 209),
 ('w-gzb-04','104','Ramprastha–Surya Nagar',       77.3250, 28.6620, 52300, 0.14, 211),
 ('w-gzb-05','105','Shalimar Garden',              77.3350, 28.6830, 83900, 0.09, 208),
 ('w-gzb-06','106','Rajendra Nagar',               77.3500, 28.6780, 67700, 0.10, 210),
 ('w-gzb-07','107','Vasundhara',                   77.3660, 28.6600, 88100, 0.08, 211),
 ('w-gzb-08','108','Indirapuram',                  77.3700, 28.6400, 124500, 0.07, 207),
 ('w-gzb-09','109','Khoda Colony',                 77.3480, 28.6230, 142000, 0.06, 205),
 ('w-gzb-10','110','Sahibabad–Lajpat Nagar',       77.3650, 28.6800, 76300, 0.09, 209),
 ('w-gzb-11','111','Mohan Nagar',                  77.3850, 28.6780, 61200, 0.10, 208),
 ('w-gzb-12','112','Karhera (Hindon bank)',        77.4050, 28.6650, 34800, 0.08, 199),
 ('w-gzb-13','113','Arthala',                      77.4020, 28.6850, 45600, 0.08, 200),
 ('w-gzb-14','114','Kavi Nagar–Ghaziabad City',    77.4400, 28.6700, 118000, 0.12, 214);

with env as (select extensions.ST_MakeEnvelope(77.305, 28.605, 77.465, 28.705, 4326) g),
cells as (
  select (extensions.ST_Dump(extensions.ST_VoronoiPolygons(
            extensions.ST_Collect(extensions.ST_SetSRID(extensions.ST_MakePoint(lng, lat), 4326)),
            0.0, (select g from env)))).geom as cell
    from gzb_w
)
insert into wards (id, number, name, boundary, centroid, population, elderly_share,
                   elevation_m, area_sq_km, city_id)
select w.id, w.number, w.name,
       extensions.ST_Intersection(c.cell, (select g from env))::extensions.geography,
       extensions.ST_SetSRID(extensions.ST_MakePoint(w.lng, w.lat), 4326)::extensions.geography,
       w.population, w.elderly, w.elev,
       round((extensions.ST_Area(extensions.ST_Intersection(c.cell, (select g from env))::extensions.geography) / 1e6)::numeric, 1),
       'pune'
  from gzb_w w
  join cells c on extensions.ST_Contains(c.cell, extensions.ST_SetSRID(extensions.ST_MakePoint(w.lng, w.lat), 4326))
on conflict (id) do nothing;

-- -------------------------------------------------------------- resources ---
create temporary table gzb_r (id text, kind text, label text, operator text, agency text,
                              capacity int, lng float8, lat float8);
insert into gzb_r values
 ('GZB-FIRE-01','fire_engine','Fire tender 01 (Vaishali stn)','UP Fire Service','upfs',6,77.3372,28.6505),
 ('GZB-FIRE-02','fire_engine','Fire tender 02 (Vaishali stn)','UP Fire Service','upfs',6,77.3378,28.6509),
 ('GZB-FIRE-03','fire_engine','Fire tender 03 (Sahibabad stn)','UP Fire Service','upfs',6,77.3560,28.6720),
 ('GZB-FIRE-04','fire_engine','Fire tender 04 (Sahibabad stn)','UP Fire Service','upfs',6,77.3566,28.6724),
 ('GZB-FIRE-05','fire_engine','Fire tender 05 (Kotwali)','UP Fire Service','upfs',6,77.4350,28.6710),
 ('GZB-FIRE-06','fire_engine','Hydraulic platform 06 (Indirapuram)','UP Fire Service','upfs',4,77.3690,28.6420),
 ('GZB-AMB-01','ambulance','108 Ambulance 01 (Max Vaishali)','UP 108 EMS','health',4,77.3390,28.6492),
 ('GZB-AMB-02','ambulance','108 Ambulance 02 (Kaushambi)','UP 108 EMS','health',4,77.3238,28.6445),
 ('GZB-AMB-03','ambulance','108 Ambulance 03 (Indirapuram)','UP 108 EMS','health',4,77.3700,28.6390),
 ('GZB-AMB-04','ambulance','108 Ambulance 04 (Vasundhara)','UP 108 EMS','health',4,77.3655,28.6610),
 ('GZB-AMB-05','ambulance','108 Ambulance 05 (Mohan Nagar)','UP 108 EMS','health',4,77.3845,28.6790),
 ('GZB-AMB-06','ambulance','108 Ambulance 06 (District Hospital)','UP 108 EMS','health',4,77.4340,28.6695),
 ('GZB-AMB-07','ambulance','ALS Ambulance 07 (Khoda)','UP 108 EMS','health',3,77.3480,28.6250),
 ('GZB-BOAT-01','boat','Rescue boat 01 (Hindon, Karhera)','NDRF 8th Bn','ndrf8',8,77.4040,28.6660),
 ('GZB-BOAT-02','boat','Rescue boat 02 (Hindon, Karhera)','NDRF 8th Bn','ndrf8',8,77.4046,28.6655),
 ('GZB-BOAT-03','boat','Rescue boat 03 (Arthala)','NDRF 8th Bn','ndrf8',8,77.4015,28.6860),
 ('GZB-BOAT-04','boat','Inflatable boat 04 (Kavi Nagar)','SDRF UP','sdrf',6,77.4390,28.6880),
 ('GZB-BOAT-05','boat','Inflatable boat 05 (Vasundhara)','SDRF UP','sdrf',6,77.3670,28.6590),
 ('GZB-TEAM-01','rescue_team','NDRF team 01 (8th Bn)','NDRF 8th Bn','ndrf8',30,77.4420,28.6900),
 ('GZB-TEAM-02','rescue_team','NDRF team 02 (8th Bn)','NDRF 8th Bn','ndrf8',30,77.4425,28.6905),
 ('GZB-TEAM-03','rescue_team','SDRF team 03 (Sahibabad)','SDRF UP','sdrf',15,77.3450,28.6640),
 ('GZB-TEAM-04','rescue_team','Civil Defence team 04 (Indirapuram)','Civil Defence Ghaziabad','gmc',12,77.3720,28.6380),
 ('GZB-TEAM-05','rescue_team','Civil Defence team 05 (Vaishali)','Civil Defence Ghaziabad','gmc',12,77.3360,28.6470),
 ('GZB-PUMP-01','pump','Dewatering pump 01 (Vaishali underpass)','GMC Jal Kal','gmc',1,77.3330,28.6520),
 ('GZB-PUMP-02','pump','Dewatering pump 02 (Kaushambi)','GMC Jal Kal','gmc',1,77.3260,28.6410),
 ('GZB-PUMP-03','pump','Dewatering pump 03 (Indirapuram)','GMC Jal Kal','gmc',1,77.3680,28.6430),
 ('GZB-PUMP-04','pump','Dewatering pump 04 (Karhera)','GMC Jal Kal','gmc',1,77.4060,28.6640),
 ('GZB-PUMP-05','pump','Dewatering pump 05 (Shalimar Garden)','GMC Jal Kal','gmc',1,77.3340,28.6840),
 ('GZB-PUMP-06','pump','Dewatering pump 06 (Khoda)','GMC Jal Kal','gmc',1,77.3490,28.6220),
 ('GZB-PUMP-07','pump','Dewatering pump 07 (Sahibabad Site IV)','GMC Jal Kal','gmc',1,77.3450,28.6590),
 ('GZB-PUMP-08','pump','Dewatering pump 08 (Mohan Nagar)','GMC Jal Kal','gmc',1,77.3860,28.6770),
 ('GZB-JCB-01','jcb','JCB 01 (GMC Works, Vasundhara)','GMC Works','gmc',1,77.3640,28.6620),
 ('GZB-JCB-02','jcb','JCB 02 (GMC Works, Kavi Nagar)','GMC Works','gmc',1,77.4380,28.6720),
 ('GZB-JCB-03','jcb','JCB 03 (GMC Works, Khoda)','GMC Works','gmc',1,77.3470,28.6240),
 ('GZB-TANK-01','water_tanker','Water tanker 01 (Vaishali)','GMC Jal Kal','gmc',10000,77.3400,28.6460),
 ('GZB-TANK-02','water_tanker','Water tanker 02 (Indirapuram)','GMC Jal Kal','gmc',10000,77.3710,28.6410),
 ('GZB-TANK-03','water_tanker','Water tanker 03 (Rajendra Nagar)','GMC Jal Kal','gmc',10000,77.3510,28.6790),
 ('GZB-TANK-04','water_tanker','Water tanker 04 (Kavi Nagar)','GMC Jal Kal','gmc',10000,77.4410,28.6690),
 ('GZB-TRUCK-01','supply_truck','Relief truck 01 (IPEC staging)','GMC Disaster Cell','gmc',400,77.3435,28.6605),
 ('GZB-TRUCK-02','supply_truck','Relief truck 02 (Kaushambi)','GMC Disaster Cell','gmc',400,77.3230,28.6430),
 ('GZB-TRUCK-03','supply_truck','Relief truck 03 (Kavi Nagar)','GMC Disaster Cell','gmc',400,77.4395,28.6705),
 ('GZB-BUS-01','bus','Evacuation bus 01 (Kaushambi depot)','UPSRTC','transport',45,77.3200,28.6450),
 ('GZB-BUS-02','bus','Evacuation bus 02 (Kaushambi depot)','UPSRTC','transport',45,77.3205,28.6455),
 ('GZB-BUS-03','bus','Evacuation bus 03 (Mohan Nagar)','UPSRTC','transport',45,77.3870,28.6760),
 ('GZB-BUS-04','bus','Evacuation bus 04 (Ghaziabad old bus stand)','UPSRTC','transport',45,77.4280,28.6680);

insert into resources (id, kind, label, operator, base_location, location, capacity, status,
                       city_id, agency_id, crew_available, fuel_pct, last_reported_at, updated_at)
select id, kind, label, operator,
       extensions.ST_SetSRID(extensions.ST_MakePoint(lng, lat), 4326)::extensions.geography,
       extensions.ST_SetSRID(extensions.ST_MakePoint(lng, lat), 4326)::extensions.geography,
       capacity, 'available', 'pune', agency, true, 85 + (abs(hashtext(id)) % 15), now(), now()
  from gzb_r
on conflict (id) do nothing;

-- -------------------------------------------------------------- lifelines ---
create temporary table gzb_l (id text, kind text, name text, lng float8, lat float8,
                              capacity int, casualties bool, specs text[], served int);
insert into gzb_l values
 ('lf-gzb-h1','hospital','Max Super Speciality Hospital, Vaishali',77.3386,28.6488,null,true,'{general,trauma,burns}',null),
 ('lf-gzb-h2','hospital','Yashoda Hospital, Kaushambi',77.3235,28.6440,null,true,'{general,trauma}',null),
 ('lf-gzb-h3','hospital','Shanti Gopal Hospital, Indirapuram',77.3695,28.6385,null,true,'{general}',null),
 ('lf-gzb-h4','hospital','MMG District Hospital, Ghaziabad',77.4340,28.6690,null,true,'{general,trauma,burns}',null),
 ('lf-gzb-h5','hospital','Yashoda Hospital, Nehru Nagar',77.4270,28.6720,null,true,'{general,trauma}',null),
 ('lf-gzb-s1','shelter','IPEC campus (relief staging & shelter)',77.3432,28.6598,900,false,'{}',null),
 ('lf-gzb-s2','shelter','Vaishali Sector 4 Community Centre',77.3405,28.6470,450,false,'{}',null),
 ('lf-gzb-s3','shelter','Indirapuram Shipra Community Hall',77.3685,28.6370,500,false,'{}',null),
 ('lf-gzb-s4','shelter','Karhera Govt. Inter College',77.4030,28.6675,700,false,'{}',null),
 ('lf-gzb-s5','shelter','Shalimar Garden Ext. Barat Ghar',77.3330,28.6860,350,false,'{}',null),
 ('lf-gzb-s6','shelter','Khoda Primary School',77.3470,28.6215,400,false,'{}',null),
 ('lf-gzb-r1','relief_centre','Sahibabad Relief Centre',77.3470,28.6640,1200,false,'{}',180),
 ('lf-gzb-r2','relief_centre','Hindon Flood Relief Camp, Karhera',77.4010,28.6640,1500,false,'{}',220),
 ('lf-gzb-r3','relief_centre','Vasundhara Sector 5 Relief Centre',77.3650,28.6580,900,false,'{}',150),
 ('lf-gzb-k1','food_kitchen','Gurudwara langar, Vaishali',77.3415,28.6455,600,false,'{}',300),
 ('lf-gzb-k2','food_kitchen','Community kitchen, Arthala',77.4000,28.6840,400,false,'{}',200),
 ('lf-gzb-m1','medical_camp','Medical camp, Karhera',77.4045,28.6630,120,true,'{general}',60),
 ('lf-gzb-m2','medical_camp','Medical camp, Khoda',77.3495,28.6240,120,true,'{general}',60),
 ('lf-gzb-p1','water_point','Water point, Kaushambi',77.3250,28.6405,null,false,'{}',400),
 ('lf-gzb-p2','water_point','Water point, Rajendra Nagar',77.3520,28.6770,null,false,'{}',400),
 ('lf-gzb-e1','substation','Sahibabad 132 kV substation',77.3560,28.6690,null,false,'{}',null),
 ('lf-gzb-e2','pump_station','Hindon barrage pump station',77.4080,28.6720,null,false,'{}',null);

insert into lifelines (id, kind, name, ward_id, location, capacity, occupancy, city_id,
                       accepts_casualties, status, specialities, supplies, supplies_baseline,
                       people_served_per_hour, occupancy_baseline, last_reported_at, opened_at)
select l.id, l.kind, l.name,
       (select w.id from wards w
         where w.id like 'w-gzb-%'
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
  from gzb_l l
on conflict (id) do nothing;

-- -------------------------------------------------------- existing reports ---
-- Reports already filed from here were attached to a Pune ward. Re-home them
-- (and the incidents they opened) to the ward they are actually in.
update citizen_reports r
   set ward_id = (select w.id from wards w where w.id like 'w-gzb-%'
                   order by extensions.ST_Distance(w.boundary, r.location) limit 1)
 where extensions.ST_DWithin(r.location,
         extensions.ST_SetSRID(extensions.ST_MakePoint(77.38, 28.66), 4326)::extensions.geography, 30000);

update incidents i
   set ward_id = (select w.id from wards w where w.id like 'w-gzb-%'
                   order by extensions.ST_Distance(w.boundary, i.location) limit 1),
       updated_at = now()
 where extensions.ST_DWithin(i.location,
         extensions.ST_SetSRID(extensions.ST_MakePoint(77.38, 28.66), 4326)::extensions.geography, 30000);

drop table gzb_w;
drop table gzb_r;
drop table gzb_l;
