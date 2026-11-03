"""Unit routing profiles and live-hazard effects."""
from __future__ import annotations

import sys
import unittest

sys.path.insert(0, "tests")
import _stubs  # noqa: E402

_stubs.install()

from app.nav import cost, hazards as hz, profiles as pf, sectors  # noqa: E402

ROAD = {"length_m": 1000, "free_kmh": 30, "signals": 2, "drivable": True, "walkable": True, "bus_ok": True}
LANE = {"length_m": 300, "free_kmh": 20, "signals": 0, "drivable": True, "walkable": True, "bus_ok": False}
S = hz.SegmentState


class Profiles(unittest.TestCase):
    def test_siren_beats_signals_and_traffic(self):
        st = S(jam=0.6)
        amb, _ = cost.traverse(pf.PROFILES["ambulance"], ROAD, st)
        bus, _ = cost.traverse(pf.PROFILES["bus"], ROAD, st)
        self.assertLess(amb * 2, bus)

    def test_depth_limits_differ_by_vehicle(self):
        st = S(depth_m=0.4)
        self.assertTrue(hz.effect(pf.PROFILES["ambulance"], ROAD, st).blocked)
        self.assertFalse(hz.effect(pf.PROFILES["fire_engine"], ROAD, st).blocked)
        self.assertTrue(hz.effect(pf.PROFILES["resident"], ROAD, st).blocked)
        self.assertTrue(hz.effect(pf.PROFILES["boat"], ROAD, st).blocked)          # too shallow
        self.assertFalse(hz.effect(pf.PROFILES["boat"], ROAD, S(depth_m=0.8)).blocked)

    def test_water_slows_before_it_blocks(self):
        t0, _ = cost.traverse(pf.PROFILES["police"], ROAD, S())
        t1, e = cost.traverse(pf.PROFILES["police"], ROAD, S(depth_m=0.2))
        self.assertGreater(t1, t0)
        self.assertEqual(e.hazard, "flood")

    def test_flowing_water_halves_wading(self):
        team = pf.PROFILES["rescue_team"]
        self.assertFalse(hz.effect(team, ROAD, S(depth_m=0.4)).blocked)
        self.assertTrue(hz.effect(team, ROAD, S(depth_m=0.4, flowing=True)).blocked)

    def test_debris_blocks_all_but_jcb_and_walkers(self):
        st = S(debris=True)
        self.assertTrue(hz.effect(pf.PROFILES["ambulance"], ROAD, st).blocked)
        self.assertFalse(hz.effect(pf.PROFILES["jcb"], ROAD, st).blocked)
        self.assertFalse(hz.effect(pf.PROFILES["rescue_team"], ROAD, st).blocked)

    def test_fire_zone_only_for_fire_crews(self):
        st = S(fire_dist_m=80)
        self.assertTrue(hz.effect(pf.PROFILES["ambulance"], ROAD, st).blocked)
        self.assertFalse(hz.effect(pf.PROFILES["fire_engine"], ROAD, st).blocked)

    def test_wire_blocks_everyone_on_the_ground(self):
        for k in ("ambulance", "fire_engine", "rescue_team", "resident"):
            self.assertEqual(hz.effect(pf.PROFILES[k], ROAD, S(wire=True)).hazard, "wire")

    def test_bus_stays_on_main_roads(self):
        self.assertTrue(hz.effect(pf.PROFILES["bus"], LANE, S()).blocked)
        self.assertFalse(hz.effect(pf.PROFILES["ambulance"], LANE, S()).blocked)

    def test_confirmed_closure_is_binding_for_road_units(self):
        self.assertTrue(hz.effect(pf.PROFILES["fire_engine"], ROAD, S(confirmed_closed=True)).blocked)

    def test_drones_grounded_by_weather_not_roads(self):
        d = pf.PROFILES["drone"]
        self.assertFalse(hz.air_effect(d, hz.AirState(wind_ms=5)).blocked)
        self.assertTrue(hz.air_effect(d, hz.AirState(wind_ms=14)).blocked)
        self.assertTrue(hz.air_effect(d, hz.AirState(rain_mmph=20)).blocked)
        self.assertTrue(hz.air_effect(d, hz.AirState(no_fly=True)).blocked)

    def test_sectors(self):
        self.assertEqual(sectors.sector_of("w-pc-03"), "pcmc-pawana")
        self.assertEqual(sectors.sector_of("w-gzb-01"), "ncr")
        self.assertEqual(sectors.sector_of("w-23"), "ward:w-23")
        self.assertEqual(sectors.name_of("pcmc-nigdi-akurdi"), "Nigdi–Akurdi (PCCOE)")


if __name__ == "__main__":
    unittest.main()


class SectorBoardEvents(unittest.TestCase):
    def test_graph_files_one_summary_per_sector_and_hazard(self):
        import asyncio
        import importlib

        import app.agents.graph as graph
        from app.db import session
        from app.world import events as ev

        graph = importlib.reload(graph)
        rows: list = []

        async def fetch(q, *a):
            return [{"id": "i1", "category": "flooded_road"}, {"id": "i2", "category": "fire"}]

        async def append_many(*, clock, rows: list, city_id="pune", conn=None):
            captured.extend(rows)
            return list(range(len(rows)))
        captured = rows
        saved = (session.fetch, ev.append_many)
        session.fetch, ev.append_many = fetch, append_many
        try:
            run = graph.Run(run_id="g-1", city_id="pune", trigger="x", started_at=0, outcome="dispatched")
            plan = {"assigned": [{"resource_id": "PC-BOAT-01", "incident_id": "i1", "ward_id": "w-pc-03"},
                                 {"resource_id": "PC-AMB-01", "incident_id": "i1", "ward_id": "w-pc-07"}],
                    "reassigned": [{"resource_id": "PC-FIRE-07", "incident_id": "i2", "ward_id": "w-pc-11"}]}
            asyncio.run(graph._sector_events(run, {"dispatched": plan}))
        finally:
            session.fetch, ev.append_many = saved
        cells = {(r["payload"]["sectorId"], r["payload"]["hazardTag"]): r["payload"] for r in rows}
        self.assertEqual(set(cells), {("pcmc-pawana", "flood"), ("pcmc-midc-chikhli", "fire")})
        self.assertEqual(cells[("pcmc-pawana", "flood")]["assigned"], 2)
        self.assertEqual(cells[("pcmc-midc-chikhli", "fire")]["reassigned"], 1)
