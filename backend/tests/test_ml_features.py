"""Decision-time features must not see the future; the relearn bookkeeping must hold."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ml.district import SIM

pytestmark = pytest.mark.skipif(not (SIM / "runs" / "20240724-08" / "r0").exists(), reason="simulated runs not built")


def _ev():
    from ml.features import Evidence
    return Evidence.from_run("20240724-08", 0)


def test_no_truth_columns_reach_features():
    ev = _ev()
    for c in ("truth_seg", "is_false", "is_stale", "stale_of", "t_true_min"):
        assert c not in ev.reports.columns


def test_future_evidence_is_invisible():
    from ml.features import build
    ev = _ev()
    cand = np.arange(2000, 2400)
    args = (cand, np.full(len(cand), 60), np.zeros(len(cand), int), np.full(len(cand), 0.3))
    t0 = 20 * 60
    before = build(ev, t0, *args)
    from ml.sim.static import load_static
    st = load_static()
    fut = pd.DataFrame({"source": "official", "kind": "closure", "t_obs_min": t0 + 1,
                        "x": st["x"].to_numpy()[cand], "y": st["y"].to_numpy()[cand], "seg_rep": cand,
                        "depth_class": 3, "depth_est_m": 1.0, "blocked_est": 1.0, "reliability": 1.0})
    ev.reports = pd.concat([ev.reports, fut], ignore_index=True)
    after = build(ev, t0, *args)
    pd.testing.assert_frame_equal(before.drop(columns="rain_fc"), after.drop(columns="rain_fc"))


def test_current_status_honours_closures():
    from ml.features import build
    ev = _ev()
    cand = np.arange(100, 110)
    from ml.sim.static import load_static
    st = load_static()
    ev.reports = pd.concat([ev.reports, pd.DataFrame({
        "source": "official", "kind": "closure", "t_obs_min": 100, "x": st["x"].to_numpy()[cand[:1]],
        "y": st["y"].to_numpy()[cand[:1]], "seg_rep": cand[:1], "depth_class": -1, "depth_est_m": np.nan,
        "blocked_est": np.nan, "reliability": 1.0})], ignore_index=True)
    F = build(ev, 200, cand, np.full(10, 30), np.zeros(10, int), np.full(10, 0.3))
    assert F["closure_active"].iloc[0] == 1 and F["status_now"].iloc[0] == 1


def test_psi_and_registry(tmp_path, monkeypatch):
    import ml.relearn as rl
    rng = np.random.default_rng(0)
    a = rng.normal(size=5000)
    assert rl.psi(a, rng.normal(size=5000)) < 0.05
    assert rl.psi(a, rng.normal(2, 1, size=5000)) > 0.25
    monkeypatch.setattr(rl, "REGISTRY", tmp_path / "registry.json")
    monkeypatch.setattr(rl, "_db_record", lambda *a, **k: None)
    rl.record("passability", "v1", "champion", {"outcome": "x"})
    rl.record("passability", "v2", "champion", {"outcome": "y"})
    r = rl.registry()
    assert r["passability"]["champion"] == "v2"
    assert any(h["version"] == "v1" and h["status"] == "retired" for h in r["passability"]["history"])
