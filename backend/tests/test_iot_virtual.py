"""The virtual sensor fleet: kinds, readings, and what the fusion makes of them."""
import unittest

from app.iot import fusion, kinds, service
from app.iot.virtual import VNode


def run_episode(kind: str, code: str, major: bool) -> tuple[dict, set]:
    n = VNode(f"V-PUN-{kinds.CODE_OF[kind]}9", kind, "pune", 18.5, 73.86, "Test")
    base, state, last, t = {}, {}, {}, 1_000_000.0
    for i in range(30):
        s = fusion.score(n.reading(t, 900 + i * 6), base, state)
        base, state, t = s.baseline, s.state, t + 6
    n.start(code, major=major)
    n.episode.start = t
    peak, esc = {"human": 0.0, "structural": 0.0, "environmental": 0.0}, set()
    for i in range(40):
        s = fusion.score(n.reading(t, 1200 + i * 6), base, state)
        base, state = s.baseline, s.state
        for e in fusion.escalations(s, state, t, last):
            esc.add(e["kind"])
            last[e["kind"]] = t
        for k in peak:
            peak[k] = max(peak[k], getattr(s, k))
        t += 6
    return peak, esc


class Kinds(unittest.TestCase):
    def test_kind_from_id(self):
        self.assertEqual(kinds.kind_of("V-PUN-GAS2"), "gas")
        self.assertEqual(kinds.kind_of("V-NCR-RES5"), "rescue")
        self.assertEqual(kinds.kind_of("N1"), "field")           # a real Uno
        self.assertFalse(kinds.is_virtual("N1"))

    def test_a_gas_sentinel_cannot_hear_tapping(self):
        self.assertFalse(kinds.can_cue("V-PUN-GAS1", "t"))
        self.assertTrue(kinds.can_cue("V-PUN-GAS1", "f"))
        self.assertTrue(kinds.can_cue("N1", "t"))

    def test_reading_only_carries_the_kinds_channels(self):
        o = VNode("V-PUN-GAS1", "gas", "pune", 18.5, 73.8, "X").reading(0, 900)
        self.assertIn("mq2", o)
        self.assertNotIn("mic", o)
        self.assertTrue(o["virtual"])


class Episodes(unittest.TestCase):
    def test_major_episodes_escalate_the_right_kind(self):
        for kind, code, want in (("gas", "f", "fire"), ("struct", "c", "collapse"),
                                 ("rescue", "t", "trapped")):
            _, esc = run_episode(kind, code, major=True)
            self.assertEqual(esc, {want}, (kind, code))

    def test_minor_episodes_are_watch_only(self):
        for kind in kinds.KINDS:
            for code in kinds.KINDS[kind]["cues"]:
                peak, esc = run_episode(kind, code, major=False)
                self.assertEqual(esc, set(), (kind, code))
                self.assertGreater(max(peak.values()), 0.2, (kind, code))


class Lift(unittest.TestCase):
    def test_fire_ward_is_lifted_flood_barely(self):
        w = {"nodes": 1, "human": 0.1, "structural": 0.0, "environmental": 0.9,
             "overall": 0.9, "top": "V-PUN-GAS1", "flags": ["gas_mq2"]}
        add, why = service.hazard_lift("fire", w)
        self.assertGreater(add, 0.3)
        self.assertIn("V-PUN-GAS1", why)
        self.assertLess(service.hazard_lift("flood", w)[0], 0.2)
        self.assertIsNone(service.hazard_lift("seismic", w))     # no structural signal
        self.assertIsNone(service.hazard_lift("fire", None))


if __name__ == "__main__":
    unittest.main()
