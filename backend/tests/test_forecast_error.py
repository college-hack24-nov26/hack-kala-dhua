"""Measured forecast error: live forecasts pass through, training ones degrade."""
import json

import numpy as np

from ml import features
from ml import forecast_error as fe

Q = [round(x, 2) for x in np.linspace(0, 1, 101)]
MODEL = {
    "edges_mm": [0.1, 1.0, 3.0, 8.0], "quantiles": Q,
    "wet_bins": [{"ratio_q": list(np.linspace(0, 2, 101))} for _ in range(4)],
    "dry": {"p_false_alarm": 0.1, "amount_q": list(np.linspace(0.1, 2, 101))},
}


def test_perturb_is_monotone_in_u_and_keeps_dry_dry_mostly():
    m = fe.ForecastError(MODEL)
    x = np.array([0.0, 0.5, 4.0, 12.0])
    lo, mid, hi = m.perturb(x, 0.05), m.perturb(x, 0.5), m.perturb(x, 0.99)
    assert (lo[1:] <= mid[1:]).all() and (mid[1:] <= hi[1:]).all()
    assert mid[0] == 0.0          # a dry hour is only a false alarm in the wettest 10%
    assert hi[0] > 0.0


def test_live_forecast_passes_through(tmp_path, monkeypatch):
    p = tmp_path / "fe.json"
    p.write_text(json.dumps(MODEL))
    monkeypatch.setattr(fe, "OUT", p)
    fe.model.cache_clear()
    fut = np.array([1.0, 2.0])
    assert (features._forecast(fut, 1.0) == fut).all()          # live: real forecast, untouched
    out = features._forecast(fut, float(np.exp(0.4 * 2)))       # u ~ 0.98: too wet
    assert (out > fut).all()
    fe.model.cache_clear()


def test_fallback_without_measurement(tmp_path, monkeypatch):
    monkeypatch.setattr(fe, "OUT", tmp_path / "missing.json")
    fe.model.cache_clear()
    fut = np.array([1.0, 2.0])
    assert np.allclose(features._forecast(fut, 1.5), fut * 1.5)
    fe.model.cache_clear()
