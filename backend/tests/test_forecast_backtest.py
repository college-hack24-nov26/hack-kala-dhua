"""The forecast checks itself against what followed (rolling origin)."""
import asyncio
import random
from datetime import UTC, datetime, timedelta

from app.agents import forecast


def test_backtest_beats_uniform_when_wards_differ(monkeypatch):
    rnd = random.Random(3)
    start = datetime(2026, 10, 10, 0, 0, tzinfo=UTC)
    wards = [f"w{k}" for k in range(10)]
    rate = {w: (3.0 if k >= 8 else 0.2) for k, w in enumerate(wards)}   # two hot wards
    rows = []
    t = 0.0
    while t < 12:
        for w in wards:
            if rnd.random() < rate[w] / 12:
                rows.append({"ward_id": w, "category": "flooded_road", "created_at": start + timedelta(hours=t)})
        t += 1 / 12

    async def fake_fetch(sql, *args):
        if "from incidents" in sql:
            return rows
        if "select id from wards" in sql:
            return [{"id": w} for w in wards]
        return []

    monkeypatch.setattr(forecast.db, "fetch", fake_fetch)
    out = asyncio.run(forecast.backtest(city_id="pune", horizon_hours=1.0))
    assert out["available"]
    assert out["model"]["brier"] < out["uniform"]["brier"]
    assert out["model"]["mae"] < out["uniform"]["mae"]
    assert out["model"]["top5"] >= 0.6 and out["uniform"]["top5"] is None
