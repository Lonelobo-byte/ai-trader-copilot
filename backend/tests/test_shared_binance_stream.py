from __future__ import annotations

import asyncio

import pytest

from app.data_sources import binance_ws
from app.data_sources.binance_ws import SharedBinanceStreamHub, SharedStreamCapacityError
from app.settings import Settings


@pytest.mark.asyncio
async def test_tabs_share_one_upstream_symbol_timeframe_stream(monkeypatch) -> None:
    release = asyncio.Event()

    class FakeSubscriber:
        starts = 0

        def __init__(self, symbol, timeframe, settings):
            self.symbol = symbol
            self.timeframe = timeframe

        async def start(self):
            FakeSubscriber.starts += 1
            yield {
                "type": "init",
                "candles": [],
                "ticker": {"lastPrice": 100},
                "order_book": {"bids": [], "asks": []},
            }
            await release.wait()

    monkeypatch.setattr(binance_ws, "BinanceWSSubscriber", FakeSubscriber)
    settings = Settings(
        _env_file=None,
        analysis_stream_max_pairs=2,
        analysis_stream_idle_seconds=0,
    )
    hub = SharedBinanceStreamHub(settings)
    first = hub.events("BTCUSDT", "15m")
    second = hub.events("BTCUSDT", "15m")
    first_event = await anext(first)
    second_event = await anext(second)
    assert first_event["type"] == "init"
    assert second_event["type"] == "init"
    assert FakeSubscriber.starts == 1
    await first.aclose()
    await second.aclose()
    await asyncio.sleep(0.01)
    assert not hub._streams


@pytest.mark.asyncio
async def test_shared_stream_pair_capacity_is_bounded(monkeypatch) -> None:
    release = asyncio.Event()

    class FakeSubscriber:
        def __init__(self, symbol, timeframe, settings):
            pass

        async def start(self):
            yield {"type": "init", "candles": [], "ticker": {}, "order_book": {}}
            await release.wait()

    monkeypatch.setattr(binance_ws, "BinanceWSSubscriber", FakeSubscriber)
    hub = SharedBinanceStreamHub(Settings(
        _env_file=None,
        analysis_stream_max_pairs=1,
        analysis_stream_idle_seconds=0,
    ))
    first = hub.events("BTCUSDT", "15m")
    await anext(first)
    second = hub.events("ETHUSDT", "15m")
    with pytest.raises(SharedStreamCapacityError):
        await anext(second)
    await second.aclose()
    await first.aclose()
    await asyncio.sleep(0.01)
