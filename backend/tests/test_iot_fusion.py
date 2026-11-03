"""LoRa node scoring. Pure functions: python -m unittest tests.test_iot_fusion -v"""
from __future__ import annotations

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.iot import fusion  # noqa: E402


def calm(i: int, rnd: random.Random) -> dict:
    return {"node": "RN01", "seq": i, "up": 200 + 2 * i, "mq2": 180 + rnd.randint(-6, 6),
            "mq135": 260 + rnd.randint(-8, 8), "temp_c": 29 + rnd.random() * 0.4,
            "tilt_deg": 2.0 + rnd.random() * 0.2, "gyro_dps": rnd.random() * 2,
            "vib_g": 0.004, "mic": 20 + rnd.randint(0, 8), "piezo": 10 + rnd.randint(0, 5),
            "knocks": 0, "tilt_sw": 1, "pir": None}


def settle(n: int = 30):
    rnd = random.Random(1)
    base, state = {}, {}
    for i in range(n):
        s = fusion.score(calm(i, rnd), base, state)
        base, state = s.baseline, s.state
    return base, state, rnd


class Fusion(unittest.TestCase):
    def test_quiet_room_scores_low(self):
        base, state, rnd = settle()
        s = fusion.score(calm(99, rnd), base, state)
        self.assertLess(s.overall, 0.15)
        self.assertLess(s.human, 0.15)

    def test_tapping_and_voices_mean_a_person(self):
        base, state, rnd = settle()
        for i in range(4):
            r = calm(100 + i, rnd) | {"knocks": 3, "piezo": 220, "mic": 160}
            s = fusion.score(r, base, state)
            base, state = s.baseline, s.state
        self.assertGreater(s.human, 0.8)
        self.assertIn("tapping", s.flags)

    def test_lean_is_structural(self):
        base, state, rnd = settle()
        s = fusion.score(calm(100, rnd) | {"tilt_deg": 11.0, "tilt_sw": 0}, base, state)
        self.assertGreater(s.structural, 0.9)
        self.assertIn("tilt_shift", s.flags)

    def test_gas_ignored_while_warming_up(self):
        base, state, rnd = settle()
        s = fusion.score(calm(100, rnd) | {"mq2": 600, "up": 30}, base, state)
        self.assertEqual(s.environmental, 0.0)
        self.assertIn("warming_up", s.flags)

    def test_smoke_rise_is_environmental(self):
        base, state, rnd = settle()
        s = fusion.score(calm(100, rnd) | {"mq2": 400}, base, state)
        self.assertGreater(s.environmental, 0.9)

    def test_baseline_does_not_learn_the_emergency(self):
        base, state, rnd = settle()
        before = base["mq2"]
        for i in range(20):
            s = fusion.score(calm(100 + i, rnd) | {"mq2": 600}, base, state)
            base, state = s.baseline, s.state
        self.assertAlmostEqual(base["mq2"], before, delta=1)

    def test_escalation_needs_persistence_and_cools_down(self):
        base, state, rnd = settle()
        last: dict = {}
        s = fusion.score(calm(100, rnd) | {"tilt_deg": 12.0}, base, state)
        self.assertEqual(fusion.escalations(s, s.state, 1000, last), [])
        s2 = fusion.score(calm(101, rnd) | {"tilt_deg": 12.0}, s.baseline, s.state)
        found = fusion.escalations(s2, s2.state, 1002, last)
        self.assertEqual([e["kind"] for e in found], ["collapse"])
        last["collapse"] = 1002
        s3 = fusion.score(calm(102, rnd) | {"tilt_deg": 12.0}, s2.baseline, s2.state)
        self.assertEqual(fusion.escalations(s3, s3.state, 1004, last), [])

    def test_missing_sensors_do_not_crash(self):
        s = fusion.score({"node": "RN01", "seq": 1}, {}, {})
        self.assertEqual(s.overall, 0.0)


if __name__ == "__main__":
    unittest.main()
