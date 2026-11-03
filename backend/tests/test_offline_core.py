"""Unit tests for the stability fixes, the mesh envelope and the Copilot's
cancel parsing. Pure functions only: run with

    cd backend && python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import _stubs  # noqa: E402

_stubs.install()

from app.mesh import envelope  # noqa: E402
from app.solver import routing  # noqa: E402

# A straight east-west road in Pune, ~2.2 km long.
A = (73.8400, 18.5200)
B = (73.8610, 18.5200)
MID = (73.8505, 18.5200)


class Geometry(unittest.TestCase):
    def test_point_on_segment_is_zero(self):
        self.assertLess(routing.point_segment_km(MID, A, B), 0.001)

    def test_point_off_segment(self):
        # ~0.0018 deg latitude is ~200 m
        d = routing.point_segment_km((MID[0], MID[1] + 0.0018), A, B)
        self.assertAlmostEqual(d, 0.2, delta=0.02)

    def test_block_at_destination_is_not_relevant(self):
        near_dest = (B[0] - 0.0005, B[1])  # ~50 m from B
        self.assertEqual(routing.relevant_blocks(A, B, [near_dest]), [])
        self.assertEqual(routing.relevant_blocks(A, B, [MID]), [MID])

    def test_exposure_counts_distinct_blocks_not_vertices(self):
        # A densely drawn road past one block must count 1, however many
        # vertices the router used. The old count grew with vertex density.
        dense = [[A[0] + (B[0] - A[0]) * t / 100, A[1]] for t in range(101)]
        self.assertEqual(routing.exposure(dense, [MID]), 1)
        self.assertEqual(routing.exposure(dense, [MID, (MID[0] + 0.002, MID[1])]), 2)

    def test_exposure_sees_block_between_sparse_vertices(self):
        # Two vertices 2 km apart with a block in the middle of the segment:
        # sampling vertices missed this; segment distance does not.
        self.assertEqual(routing.exposure([list(A), list(B)], [MID]), 1)

    def test_exposure_ignores_block_at_the_job(self):
        self.assertEqual(routing.exposure([list(A), list(B)], [B]), 0)

    def test_remaining_path_drops_what_is_behind(self):
        path = [list(A), list(MID), list(B)]
        ahead = routing.remaining_path(path, 0.75)
        self.assertGreater(ahead[0][0], MID[0])
        self.assertEqual(ahead[-1], list(B))
        # a block the unit has already passed does not count any more
        passed = (A[0] + 0.004, A[1])
        self.assertEqual(routing.exposure(path, [passed]), 1)
        self.assertEqual(routing.exposure(ahead, [passed]), 0)

    def test_detour_waypoints_are_either_side(self):
        left, right = routing._detour_waypoints(A, B, MID)
        self.assertGreater(left[1], MID[1])
        self.assertLess(right[1], MID[1])
        self.assertAlmostEqual(routing.haversine_km(left, MID), routing.DETOUR_OFFSET_KM, delta=0.05)

    def test_exclude_param_nearest_first_and_capped(self):
        far = (MID[0], MID[1] + 0.02)
        blocks = [far, MID] + [(MID[0], MID[1] + 0.03 + i * 1e-4) for i in range(60)]
        s = routing._exclude_param(A, B, blocks)
        self.assertTrue(s.startswith("point(73.85050 18.52000)"))
        self.assertEqual(s.count("point("), routing.MAX_EXCLUDE_POINTS)

    def test_fallback_matrix_no_penalty_for_block_at_job(self):
        m = routing._fallback_matrix([A], [B], [B])
        m2 = routing._fallback_matrix([A], [B], [])
        self.assertAlmostEqual(m.durations[0][0], m2.durations[0][0])
        m3 = routing._fallback_matrix([A], [B], [MID])
        self.assertGreater(m3.durations[0][0], m2.durations[0][0])


class Envelope(unittest.TestCase):
    KEY = "test-key"

    def test_roundtrip_signed(self):
        text = envelope.encode("R", {"id": "p1", "k": "flooded_road", "la": 18.123456789,
                                     "lo": 73.1, "x": "water"}, key=self.KEY, human="[SOS] water")
        self.assertTrue(text.startswith("[SOS] water IDX1|R|"))
        pkt = envelope.decode(text, key=self.KEY)
        self.assertTrue(pkt.verified)
        self.assertEqual(pkt.body["la"], 18.12346)
        self.assertEqual(pkt.location, (73.1, 18.12346))
        self.assertEqual(pkt.human, "[SOS] water")

    def test_tamper_is_refused(self):
        text = envelope.encode("R", {"id": "p1", "k": "fire", "la": 1, "lo": 2}, key=self.KEY)
        with self.assertRaises(envelope.BadPacket):
            envelope.decode(text.replace('"fire"', '"flooded_road"'), key=self.KEY)

    def test_unsigned_accepted_but_not_verified(self):
        text = envelope.encode("R", {"id": "p1", "la": 1, "lo": 2})
        pkt = envelope.decode(text, key=self.KEY)
        self.assertFalse(pkt.signed)
        self.assertFalse(pkt.verified)
        with self.assertRaises(envelope.BadPacket):
            envelope.decode(text, key=self.KEY, require_signature=True)

    def test_not_ours(self):
        for t in ("hello", "IDX1|Z|{}|-", "IDX1|R|not json|-", "x" * 5000):
            with self.assertRaises(envelope.BadPacket):
                envelope.decode(t)

    def test_bridge_script_makes_identical_packets(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
        import mesh_bridge
        body = {"id": "s1", "n": "cam_01", "k": "fire", "c": 0.8, "la": 18.5, "lo": 73.8, "x": None}
        self.assertEqual(mesh_bridge.packet("S", body, self.KEY),
                         envelope.encode("S", body, key=self.KEY))


class CivBundle(unittest.TestCase):
    """The bitchat phone's back-online bundle becomes ordinary mesh packets."""

    KEY = "test-key"

    def setUp(self):
        from app.mesh import service
        self.service = service

    def test_geotagged_sos_becomes_signed_report(self):
        text = self.service.civ_incident_packet(
            {"id": "ABCDEF0123", "timestamp": 1790000000000, "sender": "anon1",
             "role": "CIVILIAN", "lat": 18.52, "lon": 73.85,
             "content": "SOS water rising geo:18.52,73.85"}, key=self.KEY)
        pkt = envelope.decode(text, key=self.KEY)
        self.assertEqual(pkt.type, "R")
        self.assertTrue(pkt.verified)
        self.assertEqual(pkt.body["id"], "bABCDEF0123")
        self.assertEqual(pkt.body["t"], 1790000000)
        self.assertEqual(pkt.location, (73.85, 18.52))

    def test_vlm_fire_brief_becomes_sensor_packet(self):
        text = self.service.civ_incident_packet(
            {"id": "v1", "lat": 18.5, "lon": 73.8, "content": "car on fire",
             "source": "vlm", "kind": "fire", "confidence": 0.9}, key=self.KEY)
        pkt = envelope.decode(text, key=self.KEY)
        self.assertEqual(pkt.type, "S")
        self.assertEqual(pkt.body["k"], "fire")
        self.assertEqual(pkt.body["v"], 1)

    def test_responder_role_is_kept_in_text(self):
        text = self.service.civ_incident_packet(
            {"id": "a1", "lat": 18.5, "lon": 73.8, "role": "AMBULANCE",
             "content": "two injured"}, key=self.KEY)
        self.assertIn("[ambulance] two injured", text)

    def test_no_location_or_id_is_skipped(self):
        for inc in ({"id": "x", "content": "no gps"}, {"lat": 1, "lon": 2},
                    {"id": "z", "lat": 0, "lon": 0}, {"id": "q", "lat": "a", "lon": 1}):
            self.assertIsNone(self.service.civ_incident_packet(inc, key=self.KEY))


class EventRouter(unittest.TestCase):
    """What each event causes, independent of the demo runner."""

    def setUp(self):
        from app.agents import event_router
        self.route = event_router.route

    def test_new_incident_replans(self):
        r = self.route({"kind": "incident.opened", "actor": "agent:triage", "ward_id": "W1",
                        "payload": {"severity": 2, "category": "flooded_road"}})
        self.assertIn("incident.opened", r.replan)
        self.assertIsNone(r.nudge)

    def test_severe_incident_wakes_commander(self):
        r = self.route({"kind": "incident.opened", "actor": "agent:triage", "subject_id": "i9",
                        "payload": {"severity": 5, "category": "fire"}})
        self.assertIsNotNone(r.replan)
        self.assertEqual(r.nudge["key"], "incident:i9")

    def test_field_and_override_prefixes(self):
        self.assertIsNotNone(self.route({"kind": "field.route_blocked", "actor": "field:crew1"}).replan)
        self.assertIsNotNone(self.route({"kind": "field.route_blocked", "actor": "field:crew1"}).nudge)
        self.assertIsNotNone(self.route({"kind": "override.hold", "actor": "officer:a"}).replan)

    def test_planner_output_never_loops(self):
        for kind in ("plan.generated", "assignment.created", "assignment.changed", "demand.uncovered"):
            r = self.route({"kind": kind, "actor": "agent:allocation_planner"})
            self.assertIsNone(r.replan)
        r = self.route({"kind": "resource.status_changed", "actor": "agent:allocation_planner"})
        self.assertIsNone(r.replan)
        r = self.route({"kind": "incident.opened", "actor": "agent:demo:inject"})
        self.assertIsNone(r.replan)

    def test_officer_cancel_replans_agent_cancel_does_not(self):
        self.assertIsNotNone(self.route({"kind": "assignment.cancelled", "actor": "officer:a"}).replan)
        self.assertIsNone(self.route({"kind": "assignment.cancelled", "actor": "agent:executor"}).replan)

    def test_payload_as_json_text(self):
        r = self.route({"kind": "incident.severity_changed", "actor": "agent:triage",
                        "payload": '{"severity": 4}'})
        self.assertIsNotNone(r.nudge)


class Commitments(unittest.TestCase):
    def setUp(self):
        from app import taxonomy as tx
        from app.solver.allocation import Demand, Unit
        self.Demand, self.Unit = Demand, Unit
        cache = tx.cache
        self._saved = cache.resource_kinds
        Kind = type("Kind", (), {})
        pump, amb = Kind(), Kind()
        pump.capabilities = {"dewatering"}
        amb.capabilities = {"medical"}
        cache.resource_kinds = {"pump": pump, "ambulance": amb}

    def tearDown(self):
        from app import taxonomy as tx
        tx.cache.resource_kinds = self._saved

    def _d(self, did, cap):
        return self.Demand(id=did, ward_id="w", incident_id="inc", capability=cap,
                           purpose="p", location=(0, 0), severity=3, population_at_risk=0)

    def _u(self, uid, kind):
        return self.Unit(id=uid, kind=kind, label=uid, operator="o", location=(0, 0), capacity=1)

    def test_capability_aware_and_distinct(self):
        from app.agents.replan import map_commitments
        demands = [self._d("inc:medical:0", "medical"), self._d("inc:dewatering:0", "dewatering"),
                   self._d("inc:dewatering:1", "dewatering")]
        units = [self._u("pump1", "pump"), self._u("pump2", "pump"), self._u("amb", "ambulance")]
        got = map_commitments(units, {"pump1": "inc", "pump2": "inc", "amb": "inc"}, demands,
                              {"pump1": 0.2, "pump2": 0.7, "amb": 0.1})
        # the old code mapped all three to "inc:medical:0"
        self.assertEqual(got["amb"], "inc:medical:0")
        self.assertEqual({got["pump1"], got["pump2"]}, {"inc:dewatering:0", "inc:dewatering:1"})
        self.assertEqual(got["pump2"], "inc:dewatering:0")  # most progressed keeps first

    def test_unit_that_cannot_serve_gets_nothing(self):
        from app.agents.replan import map_commitments
        got = map_commitments([self._u("amb", "ambulance")], {"amb": "inc"},
                              [self._d("inc:dewatering:0", "dewatering")], {})
        self.assertEqual(got, {})


class CancelParsing(unittest.TestCase):
    def setUp(self):
        from app.copilot import agent
        self.agent = agent
        self.names = agent.Names(
            wards={"kothrud": "w-12", "baner": "w-3"},
            resources={"pump 3": "unit-pump-3", "ambulance 4": "unit-amb-4"},
            incidents={"flooded road — baner": "inc-1", "person stranded — kothrud": "inc-2"},
            capabilities=[],
        )

    def route(self, q):
        return self.agent._rule_route(q, self.names)

    def test_cancel_and_redirect(self):
        intent, args = self.route("Cancel Pump 3 and send it to flooded road — baner instead")
        self.assertEqual(intent, "cancel")
        self.assertEqual(args["unit"], "pump 3")
        self.assertEqual(args["instead"], "redirect")
        self.assertEqual(args["instead_target"], "flooded road — baner")

    def test_cancel_and_hold_minutes(self):
        intent, args = self.route("stop ambulance 4, hold it for 20 minutes because crew is exhausted")
        self.assertEqual(intent, "cancel")
        self.assertEqual(args["instead"], "hold")
        self.assertEqual(args["minutes"], 20)
        self.assertIn("crew is exhausted", args["reason"])

    def test_cancel_to_base_and_stage(self):
        self.assertEqual(self.route("recall pump 3 back to base")[1]["instead"], "return_to_base")
        _, a = self.route("call off ambulance 4 and stage it in kothrud")
        self.assertEqual(a["instead"], "stage")

    def test_operations_and_memory_intents(self):
        self.assertEqual(self.route("who is on what right now")[0], "operations")
        i, a = self.route("remember that pump 3 must stay in kothrud until 6am")
        self.assertEqual(i, "remember")
        self.assertEqual(a["scope"], "standing_order")
        self.assertEqual(self.route("what do you remember")[0], "memory")
        self.assertEqual(self.route("forget the instruction about pump 3")[0], "forget")

    def test_existing_intents_unchanged(self):
        self.assertEqual(self.route("what if we send pump 3 to baner")[0], "simulate")
        self.assertEqual(self.route("which wards are worst")[0], "rank")


class MemoryShaping(unittest.TestCase):
    def test_refs_and_search_terms(self):
        from app.copilot import memory
        refs = memory.refs_from_blocks([
            {"type": "actions", "actions": [{"params": {"resource_id": "u1", "incident_id": "i1"}}]},
            {"type": "strategies", "strategies": [{"id": "s-a"}, {"id": "s-b"}]},
        ])
        self.assertEqual(refs["resource_ids"], ["u1"])
        self.assertEqual(refs["strategy_ids"], ["s-a", "s-b"])
        self.assertEqual(memory._search_terms("Why is Kothrud above Aundh?"),
                         "kothrud or above or aundh")


if __name__ == "__main__":
    unittest.main()


class Commander(unittest.TestCase):
    def test_parse_json_action(self):
        from app.agents import commander
        got = commander._parse('```json\n{"thought":"look","tool":"get_situation","args":{}}\n```')
        self.assertEqual(got["tool"], "get_situation")
        self.assertEqual(commander._parse('Sure! {"tool":"no_action"} done')["args"], {})
        self.assertIsNone(commander._parse("I think we should move the boat"))

    def test_only_read_and_analyse_tools_allowed(self):
        from app.agents import commander
        self.assertNotIn("propose_action", commander.READ_ANALYSE)
        self.assertTrue(all(not t.startswith(("cancel", "reallocate", "execute"))
                            for t in commander.READ_ANALYSE))

    def test_no_model_means_no_episode(self):
        import asyncio
        from unittest.mock import patch
        from app.agents import commander, llm
        with patch.object(llm, "current_engine", return_value="fallback"):
            ep = asyncio.run(commander.run({"kind": "test", "summary": "x"}))
        self.assertEqual(ep.outcome, "skipped")
