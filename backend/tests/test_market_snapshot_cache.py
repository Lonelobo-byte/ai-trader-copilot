from __future__ import annotations

import pytest

from app.data_sources import data_aggregator
from app.settings import Settings


@pytest.mark.asyncio
async def test_market_snapshot_cache_deduplicates_and_isolates_callers(monkeypatch) -> None:
    data_aggregator._SNAPSHOT_CACHE.clear()
    data_aggregator._SNAPSHOT_INFLIGHT.clear()
    calls = 0

    async def fake_fetch(symbol: str, timeframe: str, settings: Settings, candle_limit: int = 200):
        nonlocal calls
        calls += 1
        return {"symbol": symbol, "timeframe": timeframe, "candles": [{"close": 100.0}]}

    monkeypatch.setattr(data_aggregator, "fetch_market_intelligence", fake_fetch)
    settings = Settings(market_snapshot_cache_seconds=30, market_snapshot_cache_max_entries=8)

    first = await data_aggregator.fetch_market_intelligence_cached("BTCUSDT", "15m", settings)
    first["candles"][0]["close"] = 0.0
    second = await data_aggregator.fetch_market_intelligence_cached("BTCUSDT", "15m", settings)

    assert calls == 1
    assert second["candles"][0]["close"] == 100.0
    data_aggregator._SNAPSHOT_CACHE.clear()
    data_aggregator._SNAPSHOT_INFLIGHT.clear()
