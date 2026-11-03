"""Storm catalogue: the real wet spells of 2015-2026 over the district.

    python -m ml.sim.storms

Every simulated storm is a real one. A day is *wet* if any of these held
(thresholds are assumptions, chosen so that every spell the news covered is in):

    district rain (ERA5)          >= 50 mm in the day
    upstream rain (Maval, ERA5)   >= 90 mm in the day     (Pawana dam catchment)
    any river cell (GloFAS)       >= 0.8 x its median annual peak

Wet days less than two days apart form one spell. Each spell becomes one
72-hour window anchored on its wettest six hours (or, for a river-only spell,
on the peak-flow day), starting 18 hours before the anchor so the rise is in
the window. A sample of ordinary monsoon days (25-50 mm) is added as `moderate`
windows, so the models also see storms where little goes wrong, and three dry
days a year as `everyday` windows: no storm, but trees still fall, mains burst
and crashes block roads (ml.sim.world PARAMS["everyday"]).

Split by year, never within a storm: train 2015-2021, tune 2022, test 2023+.
GloFAS after July 2022 is forecast-based (`glofas_kind` says which).

Writes data/sim/storms.parquet and data/sim/drivers/<storm_id>.parquet
(30-minute drivers: rain, upstream rain, wind, gusts, cloud, discharge per cell).
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from ml.district import RAW, RIVER_POINTS, SIM, WEATHER_POINTS

STEP_MIN = 30
HOURS = 72
STEPS = HOURS * 60 // STEP_MIN
LEAD_H = 18
GLOFAS_REANALYSIS_END = pd.Timestamp("2022-07-31")


def split_of(year: int) -> str:
    return "train" if year <= 2021 else ("tune" if year == 2022 else "test")


def load_era5(point: str) -> pd.DataFrame:
    frames = []
    for f in sorted((RAW / "meteo").glob(f"era5_{point}_*.json")):
        h = json.loads(f.read_text())["hourly"]
        frames.append(pd.DataFrame(h))
    df = pd.concat(frames, ignore_index=True)
    df["time"] = pd.to_datetime(df["time"])
    return df.set_index("time").sort_index().astype("float32").fillna(0.0)


def load_glofas() -> pd.DataFrame:
    cols = {}
    for name in RIVER_POINTS:
        d = json.loads((RAW / "glofas" / f"{name}.json").read_text())["daily"]
        cols[name] = pd.Series(d["river_discharge"], index=pd.to_datetime(d["time"]), dtype="float32")
    return pd.DataFrame(cols).sort_index().ffill()


def river_stats(q: pd.DataFrame) -> dict[str, dict[str, float]]:
    """Median annual peak (~2-year flow, our bankfull proxy) and record peak per cell."""
    out = {}
    for c in q.columns:
        am = q[c].groupby(q.index.year).max()
        out[c] = {"q_bf": float(am.median()), "q_top": float(am.max())}
    return out


def build(moderate_per_year: int = 2, everyday_per_year: int = 3, seed: int = 7) -> pd.DataFrame:
    dist, up = load_era5("district"), load_era5("maval_upstream")
    q = load_glofas()
    stats = river_stats(q)
    day_rain = dist["precipitation"].resample("D").sum()
    day_up = up["precipitation"].resample("D").sum()
    hi_q = pd.concat([q[c] >= 0.8 * stats[c]["q_bf"] for c in q.columns], axis=1).any(axis=1)
    hi_q = hi_q.reindex(day_rain.index, fill_value=False)

    wet = (day_rain >= 50) | (day_up.reindex(day_rain.index, fill_value=0) >= 90) | hi_q
    days = list(day_rain.index[wet])
    spells: list[list[pd.Timestamp]] = []
    for d in days:
        if spells and (d - spells[-1][-1]).days <= 2:
            spells[-1].append(d)
        else:
            spells.append([d])

    six = dist["precipitation"].rolling(6).sum()
    rows = []
    used: list[tuple[pd.Timestamp, pd.Timestamp]] = []

    def window(anchor: pd.Timestamp, tier: str, spell_days: int) -> None:
        start = anchor.floor("h") - pd.Timedelta(hours=LEAD_H)
        end = start + pd.Timedelta(hours=HOURS)
        if any(s < end and start < e for s, e in used):
            return
        if end > dist.index[-1]:
            return
        used.append((start, end))
        sl = dist.loc[start: end - pd.Timedelta(hours=1)]
        su = up.loc[start: end - pd.Timedelta(hours=1)]
        qs = q.loc[start.normalize(): end.normalize()]
        rows.append({
            "storm_id": start.strftime("%Y%m%d-%H"), "start": start, "end": end, "tier": tier,
            "year": start.year, "split": split_of(start.year), "spell_days": spell_days,
            "rain_72h_mm": round(float(sl["precipitation"].sum()), 1),
            "peak_rain_mmph": round(float(sl["precipitation"].max()), 1),
            "upstream_72h_mm": round(float(su["precipitation"].sum()), 1),
            "peak_gust_kmh": round(float(sl["wind_gusts_10m"].max()), 1),
            "antecedent_7d_mm": round(float(dist.loc[start - pd.Timedelta(days=7): start]["precipitation"].sum()), 1),
            **{f"peak_q_{c}": round(float(qs[c].max()), 1) for c in q.columns},
            "glofas_kind": "reanalysis" if end <= GLOFAS_REANALYSIS_END else "forecast",
        })

    for sp in spells:
        lo, hi = sp[0], sp[-1] + pd.Timedelta(hours=23)
        if float(day_rain.loc[sp].max()) >= 25:
            # Long monsoon spells hold several storms: take up to three
            # non-overlapping peaks, wettest first, each on a wet day.
            s6 = six.loc[lo:hi].sort_values(ascending=False)
            taken = 0
            for t, v in s6.items():
                if taken == 3 or v < 8:
                    break
                if not wet.get(t.normalize(), False):
                    continue
                before = len(rows)
                window(t - pd.Timedelta(hours=3), "major", len(sp))
                taken += len(rows) > before
        else:
            qq = q.loc[lo:hi].max(axis=1)
            window(qq.idxmax() + pd.Timedelta(hours=12), "major", len(sp))

    rng = np.random.default_rng(seed)
    # Everyday windows: dry or near-dry days in every season. No storm, but the
    # everyday disruptions (tree falls, burst mains, overflowing drains, crashes)
    # still close roads, and the models must learn those too.
    dry = day_rain[day_rain < 2]
    for y, grp in dry.groupby(dry.index.year):
        pick = rng.choice(len(grp), size=min(everyday_per_year, len(grp)), replace=False)
        for i in sorted(pick):
            window(grp.index[i] + pd.Timedelta(hours=LEAD_H + 6), "everyday", 0)
    mod = day_rain[(day_rain >= 25) & (day_rain < 50)]
    for y, grp in mod.groupby(mod.index.year):
        pick = rng.choice(len(grp), size=min(moderate_per_year, len(grp)), replace=False)
        for i in sorted(pick):
            d = grp.index[i]
            s6 = six.loc[d: d + pd.Timedelta(hours=23)]
            window(s6.idxmax() - pd.Timedelta(hours=3), "moderate", 1)

    cat = pd.DataFrame(rows).sort_values("start").reset_index(drop=True)
    out = SIM / "drivers"
    out.mkdir(parents=True, exist_ok=True)
    for r in cat.itertuples():
        drivers(r.start, dist, up, q).to_parquet(out / f"{r.storm_id}.parquet")
    cat.to_parquet(SIM / "storms.parquet", index=False)
    (SIM / "river_stats.json").write_text(json.dumps(stats, indent=1))
    return cat


def drivers(start: pd.Timestamp, dist: pd.DataFrame, up: pd.DataFrame, q: pd.DataFrame) -> pd.DataFrame:
    """30-minute drivers for one window. Hourly ERA5 is held over both halves
    (rain is redistributed inside the hour by the world, per realization);
    daily GloFAS means are placed at noon and interpolated."""
    idx = pd.date_range(start, periods=STEPS, freq=f"{STEP_MIN}min")
    hr = idx.floor("h")
    d = dist.reindex(hr).fillna(0.0)
    u = up.reindex(hr).fillna(0.0)
    df = pd.DataFrame({
        "time": idx,
        "rain_mmph": d["precipitation"].to_numpy(),
        "upstream_mmph": u["precipitation"].to_numpy(),
        "wind_kmh": d["wind_speed_10m"].to_numpy(),
        "gust_kmh": d["wind_gusts_10m"].to_numpy(),
        "cloud_pct": d["cloud_cover"].to_numpy(),
    })
    qn = q.copy()
    qn.index = qn.index + pd.Timedelta(hours=12)
    qi = qn.reindex(qn.index.union(idx)).interpolate("time").reindex(idx)
    for c in q.columns:
        df[f"q_{c}"] = qi[c].to_numpy(dtype="float32")
    return df


if __name__ == "__main__":
    c = build()
    pd.set_option("display.width", 220)
    print(c[["storm_id", "tier", "split", "rain_72h_mm", "peak_rain_mmph", "upstream_72h_mm",
             "peak_gust_kmh", "peak_q_pawana_chinchwad", "peak_q_mula_wakad"]].to_string())
    print(c.groupby(["split", "tier"]).size())
