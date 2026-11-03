"""How wrong a real short-range rain forecast is, measured, not assumed.

    python -m ml.forecast_error        # after `python -m ml.fetch.meteo --only hfc`

The passability model takes the rain forecast for the next 30-90 minutes as a
feature (`rain_fc`). In training the "forecast" is the simulated storm's own
future rain, so it has to be degraded to look like a forecast. Until now that
degradation was invented: a log-normal multiplier with sigma 0.4.

This module replaces the invention with a measurement. Open-Meteo's historical
forecast archive keeps what the forecast actually said at the time (the first
hours of each model run, stitched), from 2022. ERA5 is the reanalysis of what
fell. For every hour at the district points we compare the forecast rain in the
next one and two hours with what ERA5 says fell, and keep:

  * per intensity bin of the true amount, the distribution of forecast/true
    (under- and over-forecasting, including near-misses that forecast nothing);
  * for dry hours, how often the forecast cried rain and how much;
  * plain skill scores (bias, MAE, correlation, POD, FAR, CSI), all hours and
    monsoon (June to September) only.

`perturb(true_future, u)` turns the simulated future into a forecast with the
measured error at quantile u. Training draws u per decision, so one decision's
forecast is consistently wet or dry across its horizons, the way a real forecast
miss is.

Honest limits: ERA5 is itself a model at ~25 km, not a rain gauge, so this is
"forecast versus best reanalysis", and a point forecast at ~10 km over one
district. It is still a real forecast's real error, which a made-up sigma is not.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from ml.district import PROCESSED, RAW, WEATHER_POINTS

OUT = PROCESSED / "forecast_error.json"
WINDOWS_H = (1, 2)
#: Bins of the true window amount (mm). Below WET is "dry".
WET = 0.1
EDGES = (WET, 1.0, 3.0, 8.0)
QUANTILES = np.linspace(0, 1, 101)


def _hourly(prefix: str, point: str) -> pd.Series:
    parts = []
    for f in sorted((RAW / "meteo").glob(f"{prefix}_{point}_*.json")):
        d = json.loads(f.read_text())
        h = d.get("hourly") or {}
        if not h.get("time"):
            continue
        parts.append(pd.Series(h.get("precipitation"), index=pd.to_datetime(h["time"]), dtype="float64"))
    if not parts:
        return pd.Series(dtype="float64")
    s = pd.concat(parts)
    return s[~s.index.duplicated(keep="last")].sort_index()


def pairs() -> pd.DataFrame:
    """Aligned (true, forecast) window sums for every point, hour and window."""
    rows = []
    for point in WEATHER_POINTS:
        era, hfc = _hourly("era5", point), _hourly("hfc", point)
        both = pd.DataFrame({"obs": era, "fc": hfc}).dropna()
        if both.empty:
            continue
        both = both.asfreq("h")
        for w in WINDOWS_H:
            # forward windows: rain in [t, t+w)
            o = both["obs"][::-1].rolling(w, min_periods=w).sum()[::-1]
            f = both["fc"][::-1].rolling(w, min_periods=w).sum()[::-1]
            df = pd.DataFrame({"obs": o, "fc": f}).dropna()
            df["point"], df["window_h"] = point, w
            rows.append(df)
    if not rows:
        return pd.DataFrame(columns=["obs", "fc", "point", "window_h"])
    return pd.concat(rows)


def _scores(df: pd.DataFrame, thr: float = 1.0) -> dict:
    if df.empty:
        return {}
    o, f = df["obs"].to_numpy(), df["fc"].to_numpy()
    hit = int(((o >= thr) & (f >= thr)).sum())
    miss = int(((o >= thr) & (f < thr)).sum())
    fa = int(((o < thr) & (f >= thr)).sum())
    return {
        "n": int(len(df)),
        "bias": float(f.sum() / o.sum()) if o.sum() > 0 else None,
        "mae_mm": float(np.abs(f - o).mean()),
        "corr": float(np.corrcoef(o, f)[0, 1]) if o.std() > 0 and f.std() > 0 else None,
        "threshold_mm": thr,
        "pod": hit / (hit + miss) if hit + miss else None,
        "far": fa / (hit + fa) if hit + fa else None,
        "csi": hit / (hit + miss + fa) if hit + miss + fa else None,
    }


def fit() -> dict:
    df = pairs()
    if df.empty:
        raise SystemExit("No historical forecasts found. Run: python -m ml.fetch.meteo --only hfc")
    wet = df[df["obs"] >= WET]
    dry = df[df["obs"] < WET]
    bins = np.searchsorted(np.array(EDGES), wet["obs"].to_numpy(), side="right") - 1
    ratio = (wet["fc"] / wet["obs"]).to_numpy()
    by_bin = []
    for b in range(len(EDGES)):
        r = ratio[bins == b]
        by_bin.append({
            "lo_mm": EDGES[b], "hi_mm": EDGES[b + 1] if b + 1 < len(EDGES) else None,
            "n": int(len(r)),
            "ratio_q": np.quantile(r, QUANTILES).round(4).tolist() if len(r) >= 30 else None,
            "missed_entirely": float((r < 0.05).mean()) if len(r) else None,
        })
    # Bins too thin to trust borrow the nearest bin that is not.
    for b, x in enumerate(by_bin):
        if x["ratio_q"] is None:
            near = sorted((abs(b - k), k) for k, y in enumerate(by_bin) if y["ratio_q"] is not None)
            if not near:
                raise SystemExit("Not enough wet hours to fit the forecast error.")
            x["ratio_q"], x["borrowed_from"] = by_bin[near[0][1]]["ratio_q"], near[0][1]
    fa = dry["fc"].to_numpy()
    monsoon = df[df.index.month.isin([6, 7, 8, 9])]
    model = {
        "source": "Open-Meteo historical forecast vs ERA5 (archive-api), hourly precipitation",
        "points": list(WEATHER_POINTS),
        "period": [str(df.index.min().date()), str(df.index.max().date())],
        "windows_h": list(WINDOWS_H),
        "edges_mm": list(EDGES),
        "quantiles": QUANTILES.round(2).tolist(),
        "wet_bins": by_bin,
        "dry": {
            "n": int(len(fa)),
            "p_false_alarm": float((fa >= WET).mean()) if len(fa) else 0.0,
            "amount_q": (np.quantile(fa[fa >= WET], QUANTILES).round(4).tolist()
                         if (fa >= WET).sum() >= 30 else [WET] * len(QUANTILES)),
        },
        "scores": {"all": _scores(df), "monsoon": _scores(monsoon),
                   "monsoon_heavy": _scores(monsoon, thr=5.0)},
        "replaces": "lognormal(0, 0.4) multiplier (invented)",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(model, indent=1))
    return model


class ForecastError:
    def __init__(self, m: dict) -> None:
        self.m = m
        self.edges = np.array(m["edges_mm"])
        self.q = np.array(m["quantiles"])
        self.ratio = [np.array(b["ratio_q"]) for b in m["wet_bins"]]
        self.p_fa = float(m["dry"]["p_false_alarm"])
        self.fa_q = np.array(m["dry"]["amount_q"])

    def perturb(self, true_future: np.ndarray, u: float) -> np.ndarray:
        """The forecast a real model would have issued for `true_future` (mm per
        window), at error quantile u in [0, 1] (high u: forecast too wet)."""
        x = np.asarray(true_future, dtype=float)
        out = np.empty_like(x)
        wet = x >= WET
        if wet.any():
            b = np.clip(np.searchsorted(self.edges, x[wet], side="right") - 1, 0, len(self.ratio) - 1)
            r = np.array([np.interp(u, self.q, self.ratio[k]) for k in b])
            out[wet] = x[wet] * r
        if (~wet).any():
            # The wettest (1 - p_fa) of draws on a dry hour are a false alarm.
            if u > 1 - self.p_fa and self.p_fa > 0:
                out[~wet] = np.interp((u - (1 - self.p_fa)) / self.p_fa, self.q, self.fa_q)
            else:
                out[~wet] = x[~wet]
        return out


@lru_cache(maxsize=1)
def model(path: Path | None = None) -> ForecastError | None:
    p = path or OUT
    if not p.exists():
        return None
    return ForecastError(json.loads(p.read_text()))


def main() -> None:
    m = fit()
    s = m["scores"]
    print(f"forecast error fitted on {m['period'][0]} .. {m['period'][1]} -> {OUT}")
    for k in ("all", "monsoon", "monsoon_heavy"):
        x = s[k]
        if not x:
            continue
        fmt = lambda v: "n/a" if v is None else f"{v:.2f}"  # noqa: E731
        print(f"  {k:14s} n={x['n']:6d} bias={fmt(x['bias'])} MAE={x['mae_mm']:.2f} mm "
              f"corr={fmt(x['corr'])} POD={fmt(x['pod'])} FAR={fmt(x['far'])} CSI={fmt(x['csi'])} "
              f"(>= {x['threshold_mm']} mm)")
    for b in m["wet_bins"]:
        q = b["ratio_q"]
        print(f"  true {b['lo_mm']:>4} mm+: n={b['n']:5d} forecast/true median {q[50]:.2f} "
              f"(10-90%: {q[10]:.2f}-{q[90]:.2f}), missed entirely {b['missed_entirely'] or 0:.0%}")
    print(f"  dry hours: false alarm {m['dry']['p_false_alarm']:.1%}")


if __name__ == "__main__":
    main()
