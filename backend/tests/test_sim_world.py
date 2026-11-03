"""Simulator and observation generator (ml/sim). Skipped when the district data
has not been built (python -m ml.graph, ml.terrain, ml.sim.static, ml.sim.storms)."""
from __future__ import annotations

import numpy as np
import pytest

from ml.district import PROCESSED, SIM

pytestmark = pytest.mark.skipif(
    not (SIM / "storms.parquet").exists() or not (PROCESSED / "graph" / "terrain.parquet").exists(),
    reason="district data not built")

STORM = "20240724-08"   # the 25 Jul 2024 storm


@pytest.fixture(scope="module")
def truth():
    from ml.sim.world import simulate
    return simulate(STORM, 0)


@pytest.fixture(scope="module")
def obs(truth):
    from ml.sim.observe import observe
    return observe(truth)


def test_seeded_runs_reproduce_and_realizations_differ(truth):
    from ml.sim.world import simulate
    again = simulate(STORM, 0)
    assert np.array_equal(truth.depth, again.depth)
    assert truth.events.equals(again.events)
    other = simulate(STORM, 1)
    assert not np.array_equal(truth.depth, other.depth)


def test_physical_sanity(truth):
    from ml.sim.static import load_static
    from ml.sim.world import F_BRIDGE, F_DEBRIS
    st = load_static()
    d = truth.depth.astype(np.float32)
    assert (d >= 0).all() and np.isfinite(d).all()
    assert d[0].max() < d.max()                       # it floods during the storm, not at the start
    closed = (truth.flags & F_BRIDGE).any(axis=0)
    assert not closed[~(st["river_bridge"] | st["low_bridge"]).to_numpy()].any()
    trees = truth.events[truth.events["kind"] == "tree"]
    for e in trees.head(5).itertuples():
        assert truth.flags[e.t0, e.seg] & F_DEBRIS
    assert (truth.events["t1"] > truth.events["t0"]).all()


def test_state_feeds_the_router_rules(truth):
    from app.nav.hazards import effect
    from app.nav.profiles import PROFILES
    from ml.sim.static import load_static
    from ml.sim.world import F_DEBRIS
    st = load_static()
    t, i = np.argwhere(truth.flags & F_DEBRIS)[0]
    s = truth.state(int(t), int(i))
    seg = st.iloc[int(i)].to_dict()
    if seg["drivable"] and not s.wire and not s.collapse and s.depth_m < 0.3:
        assert effect(PROFILES["ambulance"], seg, s).blocked
        assert not effect(PROFILES["jcb"], seg, s).blocked


def test_save_load_roundtrip(truth, tmp_path):
    from ml.sim.world import Truth
    truth.save(tmp_path)
    back = Truth.load(STORM, 0, tmp_path)
    assert np.array_equal(back.depth, truth.depth) and back.events.equals(truth.events)


def test_observations_lie_the_way_they_should(truth, obs):
    r = obs.reports
    cit = r[r["source"] == "citizen"]
    assert (cit["t_obs_min"] >= cit["t_true_min"]).all()
    fake = cit[cit["is_false"]]
    assert len(fake) and (fake["truth_seg"] == -1).all()
    assert len(fake) < 0.3 * len(cit)                   # rumours are a minority, not the signal
    stale = r[r["is_stale"]]
    assert len(stale)
    orig = r.set_index("obs_id").loc[stale["stale_of"]]
    assert (stale["t_obs_min"].to_numpy() > orig["t_obs_min"].to_numpy()).all()
    pat = r[r["source"] == "patrol"]
    assert ((pat["t_obs_min"] - pat["t_true_min"]) <= 10).all()
    assert (obs.traffic == 255).any()                    # closed roads often show no data, not red
    assert obs.meta["drones"] == "paused"


def test_everyday_window_has_disruptions_without_a_storm():
    import pandas as pd
    from ml.sim.world import simulate
    cat = pd.read_parquet(SIM / "storms.parquet")
    ev = cat[cat["tier"] == "everyday"]
    if ev.empty:
        pytest.skip("no everyday windows")
    tr = simulate(ev["storm_id"].iloc[0], 0)
    kinds = set(tr.events["kind"])
    assert kinds & {"crash", "breakdown", "tree", "water_main", "drain_overflow"}
    for r in tr.events[tr.events["kind"] == "water_main"].itertuples():
        assert float(tr.depth[r.t0, r.seg]) >= r.depth_m - 0.01      # a burst main puts water on its road
