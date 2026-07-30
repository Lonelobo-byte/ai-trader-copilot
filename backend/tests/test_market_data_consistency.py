from __future__ import annotations

from app.data_sources.binance_public import interval_seconds
from app.data_sources.binance_ws import BinanceWSSubscriber
from app.data_sources.data_aggregator import (
    _snapshot_key,
    synchronize_ticker_with_order_book,
)
from app.settings import Settings


def test_futures_ticker_uses_bid_and_ask_from_matching_order_book() -> None:
    ticker = {"symbol": "BTCUSDT", "lastPrice": 100.0}
    book = {
        "bids": [[99.9, 12.0]],
        "asks": [[100.1, 8.0]],
    }

    synchronized = synchronize_ticker_with_order_book(ticker, book)

    assert synchronized["bidPrice"] == 99.9
    assert synchronized["askPrice"] == 100.1
    assert synchronized["bidQty"] == 12.0
    assert synchronized["askQty"] == 8.0


def test_snapshot_cache_normalizes_to_canonical_story_history() -> None:
    assert _snapshot_key("btcusdt", "15m", 60) == _snapshot_key("BTCUSDT", "15m", 200)


def test_research_websocket_uses_futures_history_and_stream() -> None:
    subscriber = BinanceWSSubscriber("BTCUSDT", "15m", Settings())

    assert subscriber.rest_client.market == "futures"
    assert subscriber.rest_client.base_url == "https://fapi.binance.com"
    assert subscriber.websocket_url.startswith("wss://fstream.binance.com/")


def test_weekly_candles_are_supported_for_higher_timeframe_context() -> None:
    assert interval_seconds("1w") == 604_800
