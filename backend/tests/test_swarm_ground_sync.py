"""DisruptionOps owns the ground: the swarm reasoner never assigns a ground unit itself, and the ground
picture sent to the swarm is DisruptionOps' real units (clamped to the map edge when outside it)."""
import unittest

from app.drone import swarm_link as sl
from app.drone.swarm_agency import ANCHOR, to_local
from app.mesh import service as mesh


def _inc(**k):
    return {"id": "I001", "category": "flood", "people": 6, "severity": 3, "position": [10, 20, 0], "status": "responding", **k}


SIM_UNITS = [{"id": "R01", "kind": "boat", "state": "available", "position": [0, 0, 0], "capacity": 6},
             {"id": "R02", "kind": "ambulance", "state": "available", "position": [5, 5, 0], "capacity": 2}]


class ReasonGround(unittest.TestCase):
    def test_swarm_mode_assigns_swarm_units(self):
        out = sl.reason(_inc(), {"injured": {"estimate": 1}}, SIM_UNITS, [], ground="swarm")
        assigned = [a for a in out["actions"] if a["action"] == "assign" and a["payload"].get("resources")]
        self.assertTrue(assigned)

    def test_disruptionops_mode_files_needs_instead(self):
        out = sl.reason(_inc(), {"injured": {"estimate": 1}}, SIM_UNITS, [], ground="disruptionops")
        self.assertFalse([a for a in out["actions"] if a["action"] == "assign" and a["payload"].get("resources")])
        kinds = sorted(a["payload"]["kind"] for a in out["actions"] if a["action"] == "dops_need")
        self.assertEqual(kinds, ["ambulance", "boat"])
        # drones are still the swarm's to fly
        self.assertTrue([a for a in out["actions"] if a["action"] == "assign" and a["payload"].get("drones")])

    def test_one_need_per_kind(self):
        out = sl.reason(_inc(people=20), {}, [], [], ground="disruptionops")
        boats = [a for a in out["actions"] if a["action"] == "dops_need" and a["payload"]["kind"] == "boat"]
        self.assertEqual(len(boats), 1)


class GroundPayload(unittest.TestCase):
    def setUp(self):
        self.link = sl.SwarmLink(url="ws://x/ops/ws", token="t", ports=object(), take_command=True)
        self.link.run_id = "run-1"
        self.link.filed["I007"] = {"incident_id": "11111111-1111-1111-1111-111111111111", "ward_id": "w-pc-01", "kinds": ["boat"], "at": 0}

    def test_units_inside_and_outside(self):
        lat0, lng0 = ANCHOR
        rows = [
            {"id": "PC-TEAM-02", "kind": "rescue_team", "status": "available", "lng": lng0 + 0.001, "lat": lat0, "route": []},
            {"id": "PC-AMB-04", "kind": "ambulance", "status": "en_route", "lng": lng0 + 0.02, "lat": lat0, "assignment_status": "en_route",
             "incident_id": "11111111-1111-1111-1111-111111111111", "eta_minutes": 10, "progress": 0.5,
             "route": [[lng0 + 0.02, lat0], [lng0 + 0.002, lat0], [lng0 + 0.001, lat0]], "incident_status": "responding"},
        ]
        p = self.link.ground_payload(rows)
        self.assertEqual(p["source"], "disruptionops")
        self.assertEqual(p["run_id"], "run-1")
        team, amb = p["resources"]
        self.assertEqual(team["kind"], "rescue")                      # rescue_team -> swarm vocabulary
        self.assertFalse(team["off_map"])
        self.assertEqual(team["position"][:2], to_local(lng0 + 0.001, lat0, ANCHOR))
        self.assertTrue(amb["off_map"])
        self.assertLessEqual(amb["position"][0], self.link.bounds[2])  # shown at the map edge, not teleported
        self.assertEqual(amb["state"], "enroute")
        self.assertEqual(amb["eta"], 300.0)                           # 10 min x (1 - 0.5)
        self.assertTrue(all(self.link.bounds[0] <= q[0] <= self.link.bounds[2] for q in amb["route"]))
        self.assertEqual(p["incidents"], [{"local_id": "I007", "external_id": "11111111-1111-1111-1111-111111111111",
                                           "status": "responding", "resolved": False}])

    def test_ground_owner_follows_command(self):
        self.assertEqual(self.link.ground_mode(), "disruptionops")
        self.link.take_command = False
        self.assertEqual(self.link.ground_mode(), "swarm")


class CameraScene(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(mesh.scene_kind("Two people stranded on a rooftop, water up to the windows"), "trapped")
        self.assertEqual(mesh.scene_kind("Thick smoke from a shed"), "smoke")
        self.assertIsNone(mesh.scene_kind("A man is sitting at a desk looking at his phone"))
        self.assertEqual(mesh.scene_kind("street", hazards=["fire"]), "fire")
        self.assertEqual(mesh.SENSOR_CATEGORY["trapped"], "person_stranded")


if __name__ == "__main__":
    unittest.main()
