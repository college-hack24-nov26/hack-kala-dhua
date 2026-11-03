"""The vectorized rules (ml/labels.py) must agree with the router's own rules."""
from __future__ import annotations

import math

import numpy as np
import pytest

from ml.district import SIM

pytestmark = pytest.mark.skipif(not (SIM / "storms.parquet").exists(), reason="district data not built")


def test_vectorized_rules_match_router():
    from app.nav.cost import traverse
    from app.nav.profiles import PROFILES
    from ml.labels import WHY, blocked, seconds, step_state
    from ml.sim.static import load_static
    from ml.sim.world import simulate

    tr = simulate("20240724-08", 0)
    st = load_static()
    rng = np.random.default_rng(0)
    steps = [20, 50, 70]
    checked = 0
    for t in steps:
        S = step_state(tr, t)
        fire, gas = S["fire_d"], S["gas_d"]
        hot = np.flatnonzero((S["depth"] > 0.05) | S["debris"] | S["wire"] | S["bridge_closed"])
        idx = np.concatenate([rng.choice(hot, size=min(150, len(hot)), replace=False),
                              rng.integers(0, len(st), 150)])
        for kind in ("ambulance", "fire_engine", "bus", "jcb", "rescue_team", "resident", "boat"):
            p = PROFILES[kind]
            blk, why = blocked(p, S)
            secs = seconds(p, S)
            for i in idx:
                seg = st.iloc[int(i)].to_dict()
                s1, eff = traverse(p, seg, tr.state(t, int(i), fire, gas))
                assert eff.blocked == bool(blk[i]), (kind, t, i, eff.reasons, WHY[why[i]])
                if s1 is None:
                    assert math.isinf(secs[i])
                elif p.mode != "water":
                    assert secs[i] == pytest.approx(s1, rel=1e-4), (kind, t, i)
                checked += 1
    assert checked > 2000
