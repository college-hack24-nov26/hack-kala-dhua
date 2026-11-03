"""Evidence fusion: repeats are urgency not proof, independent kinds of evidence confirm, contradiction lowers."""
import unittest

from app.incidents import fusion as f
from app.incidents.fusion import Evidence as E


def citizen(n, **k):
    return E(modality="citizen", witness=f"user:{n}", **k)


class Fusion(unittest.TestCase):
    def test_single_scared_caller_life_safety_dispatches(self):
        b = f.fuse([citizen(1)], category="person_stranded", life_safety=True)
        self.assertTrue(b.dispatch)
        self.assertEqual(b.status, "uncertain")

    def test_repeats_are_one_witness_plus_urgency(self):
        once = f.fuse([citizen(1)], category="waterlogging")
        ten = f.fuse([citizen(1, age_min=i) for i in range(10)], category="waterlogging")
        self.assertLess(ten.p - once.p, 0.08)            # barely more proof
        self.assertEqual(len(ten.distress), 1)           # but flagged as distress
        self.assertGreater(ten.p, 0.4)                   # and never punished as spam

    def test_independent_people_add_with_diminishing_weight(self):
        one = f.fuse([citizen(1)], category="waterlogging").p
        two = f.fuse([citizen(1), citizen(2)], category="waterlogging").p
        three = f.fuse([citizen(i) for i in range(3)], category="waterlogging").p
        self.assertGreater(two, one)
        self.assertGreater(two - one, three - two)

    def test_crowd_alone_is_likely_not_confirmed(self):
        b = f.fuse([citizen(i) for i in range(40)], category="waterlogging")
        self.assertEqual(b.status, "likely")
        self.assertTrue(b.dispatch)
        self.assertTrue(any("independent sensor" in r for r in b.reasons))

    def test_people_plus_camera_plus_drone_confirms(self):
        b = f.fuse([citizen(1), E(modality="camera", witness="camera:vlm-cam-01", confidence=0.75),
                    E(modality="drone", witness="drone-swarm", confidence=0.6)], category="person_stranded", life_safety=True)
        self.assertEqual(b.status, "confirmed")
        self.assertEqual(sorted(b.modalities_seen), ["camera", "citizen", "drone"])

    def test_drone_alone_with_low_confidence_still_dispatches_life_safety(self):
        b = f.fuse([E(modality="drone", witness="drone-swarm", confidence=0.44)], category="person_stranded", life_safety=True)
        self.assertTrue(b.dispatch)

    def test_drone_over_empty_dry_street_contradicts(self):
        b = f.fuse([citizen(1), E(modality="drone", witness="drone-swarm", polarity=-1, confidence=0.8)], category="waterlogging")
        self.assertEqual(b.status, "doubtful")
        self.assertFalse(b.dispatch)

    def test_below_floor_asks_for_a_drone_when_one_can_fly(self):
        b = f.fuse([E(modality="mesh_unsigned", witness="device:mesh:x")], category="waterlogging", can_fly=True)
        self.assertFalse(b.dispatch)
        self.assertEqual(b.next_step, "send a drone to look")

    def test_old_evidence_fades(self):
        fresh = f.fuse([citizen(1), citizen(2)], category="fire").p
        stale = f.fuse([citizen(1, age_min=180), citizen(2, age_min=180)], category="fire").p
        self.assertLess(stale, fresh)
        self.assertAlmostEqual(f.fuse([citizen(1, age_min=10_000)], category="fire").p, f.DEFAULT_PRIOR, places=3)

    def test_crew_on_scene_confirms(self):
        self.assertEqual(f.fuse([E(modality="field_crew", witness="officer:x")], category="fallen_tree").status, "confirmed")

    def test_modality_of(self):
        self.assertEqual(f.modality_of("sensor", "device:sensor:drone-swarm"), "drone")
        self.assertEqual(f.modality_of("sensor", "device:sensor:vlm-cam-01"), "camera")
        self.assertEqual(f.modality_of("sensor", "device:sensor:lora-7", lora_nodes={"lora-7"}), "sensor")
        self.assertEqual(f.modality_of("app", "user:1"), "citizen")
        self.assertEqual(f.modality_of("mesh_unsigned", "device:mesh:p"), "mesh_unsigned")
        self.assertEqual(f.polarity_of("Water has receded, road open"), -1)

    def test_deterministic(self):
        items = [citizen(1), E(modality="camera", witness="c", confidence=0.7, distance_m=120)]
        self.assertEqual(f.fuse(items).p, f.fuse(list(reversed(items))).p)


if __name__ == "__main__":
    unittest.main()
