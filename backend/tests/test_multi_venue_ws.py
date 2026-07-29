from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from time import time

import pytest

from app.data_sources import data_aggregator, multi_venue_ws
from app.data_sources.multi_venue_ws import (
    BookIntegrityError,
    MultiVenueMarketDataHub,
    SequenceGapError,
    SubscriptionError,
)
from app.quant.live_confirmation import apply_live_confirmation
from app.settings import Settings


def _settings(**overrides) -> Settings:
    values = {
        "app_env": "test",
        "multi_venue_symbols": ["BTCUSDT"],
        "multi_venue_max_symbols": 12,
        "multi_venue_book_levels": 20,
        "multi_venue_max_events": 100,
        "coinbase_multi_venue_book_levels": 20,
        "multi_venue_min_book_levels": 1,
        "multi_venue_flow_warmup_seconds": 1.0,
        "multi_venue_min_flow_trades": 1,
        "multi_venue_min_flow_notional_usd": 1.0,
        "multi_venue_max_event_lag_seconds": 10.0,
        "multi_venue_stale_seconds": 15.0,
        "multi_venue_trade_window_seconds": 60.0,
        "multi_venue_liquidation_window_seconds": 300.0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _bybit_book(*, message_type: str = "snapshot", update_id: int = 100, sequence: int = 500):
    return {
        "topic": "orderbook.50.BTCUSDT",
        "ts": int(time() * 1000),
        "type": message_type,
        "data": {
            "s": "BTCUSDT",
            "b": [["100.00", "2.0"], ["99.50", "1.0"]],
            "a": [["100.50", "3.0"], ["101.00", "1.5"]],
            "u": update_id,
            "seq": sequence,
        },
    }


def _coinbase_book(*, sequence: int = 10, event_type: str = "snapshot"):
    return {
        "channel": "l2_data",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sequence_num": sequence,
        "events": [{
            "type": event_type,
            "product_id": "BTC-USD",
            "updates": [
                {"side": "bid", "price_level": "100.00", "new_quantity": "2.0"},
                {"side": "bid", "price_level": "99.50", "new_quantity": "1.0"},
                {"side": "offer", "price_level": "100.50", "new_quantity": "3.0"},
                {"side": "offer", "price_level": "101.00", "new_quantity": "1.5"},
            ],
        }],
    }


def _seed_books(hub: MultiVenueMarketDataHub, *, now: float = 100.0) -> None:
    hub._set_connected("bybit", True, now=now)
    hub._set_connected("coinbase", True, now=now)
    hub.process_bybit_message(_bybit_book(), now=now)
    hub.process_coinbase_message(_coinbase_book(), now=now)


def test_bybit_snapshot_delta_delete_and_service_reset() -> None:
    hub = MultiVenueMarketDataHub(_settings())
    hub._set_connected("bybit", True, now=100.0)
    hub.process_bybit_message(_bybit_book(), now=100.0)
    state = hub.states[("bybit", "BTCUSDT")]

    hub.process_bybit_message({
        "topic": "orderbook.50.BTCUSDT",
        "ts": int(time() * 1000),
        "type": "delta",
        "data": {
            "s": "BTCUSDT",
            "b": [["100.00", "0"], ["99.75", "4.0"]],
            "a": [["100.50", "2.5"]],
            "u": 101,
            "seq": 501,
        },
    }, now=101.0)
    assert 100.0 not in state.bids
    assert state.bids[99.75] == 4.0
    assert state.asks[100.5] == 2.5

    # Bybit documents u=1 as a service-reset snapshot even if the frame is
    # labelled as a delta. The old local book must be discarded.
    reset = _bybit_book(message_type="delta", update_id=1, sequence=600)
    reset["data"]["b"] = [["98.00", "1.0"]]
    reset["data"]["a"] = [["102.00", "1.0"]]
    hub.process_bybit_message(reset, now=102.0)
    assert state.bids == {98.0: 1.0}
    assert state.asks == {102.0: 1.0}


def test_trade_sides_liquidations_dedup_and_cross_venue_consensus() -> None:
    hub = MultiVenueMarketDataHub(_settings())
    _seed_books(hub)
    bybit_trade = {
        "topic": "publicTrade.BTCUSDT",
        "data": [{"T": int(time() * 1000), "s": "BTCUSDT", "S": "Buy", "v": "2", "p": "100", "i": "trade-1"}],
    }
    hub.process_bybit_message(bybit_trade, now=101.0)
    hub.process_bybit_message(bybit_trade, now=101.1)
    hub.process_coinbase_message({
        "channel": "market_trades",
        "sequence_num": 11,
        "events": [{
            "type": "update",
            "trades": [{
                "trade_id": "cb-1", "product_id": "BTC-USD",
                "price": "100", "size": "3", "side": "SELL",
                "time": datetime.now(timezone.utc).isoformat(),
            }],
        }],
    }, now=101.0)
    liquidation = {
        "topic": "allLiquidation.BTCUSDT",
        "ts": int(time() * 1000),
        "data": [{"T": int(time() * 1000), "s": "BTCUSDT", "S": "Buy", "v": "4", "p": "99"}],
    }
    hub.process_bybit_message(liquidation, now=101.0)
    hub.process_bybit_message(liquidation, now=101.1)

    snapshot = hub.snapshot("BTCUSDT", now=102.0)
    assert snapshot["flow_confirmed"] is True
    assert snapshot["flow_consensus"] == "BULLISH"
    assert snapshot["observed_liquidations"]["event_count"] == 1
    assert snapshot["observed_liquidations"]["long_liquidated_notional"] == 396.0
    assert snapshot["venues"]["bybit"]["trade_flow"]["trade_count"] == 1
    # Coinbase reports maker side; maker SELL means aggressive BUY.
    assert hub.states[("coinbase", "BTCUSDT")].last_trade_side == "BUY"


def test_identical_liquidations_within_and_across_frames_remain_distinct() -> None:
    hub = MultiVenueMarketDataHub(_settings())
    _seed_books(hub)
    envelope_time = int(time() * 1000)
    event = {
        "T": int(time() * 1000), "s": "BTCUSDT", "S": "Sell",
        "v": "2", "p": "100",
    }
    hub.process_bybit_message({
        "topic": "allLiquidation.BTCUSDT",
        "ts": envelope_time,
        "data": [dict(event), dict(event)],
    }, now=101.0)
    hub.process_bybit_message({
        "topic": "allLiquidation.BTCUSDT",
        "ts": envelope_time + 1,
        "data": [dict(event)],
    }, now=101.5)
    liquidations = hub.snapshot("BTCUSDT", now=102.0)["observed_liquidations"]
    assert liquidations["event_count"] == 3
    assert liquidations["short_liquidated_notional"] == 600.0


def test_coinbase_absolute_updates_heartbeat_and_gap_invalidation() -> None:
    hub = MultiVenueMarketDataHub(_settings())
    hub._set_connected("coinbase", True, now=100.0)
    hub.process_coinbase_message(_coinbase_book(), now=100.0)
    state = hub.states[("coinbase", "BTCUSDT")]
    hub.process_coinbase_message({
        "channel": "l2_data",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sequence_num": 11,
        "events": [{
            "type": "update",
            "product_id": "BTC-USD",
            "updates": [
                {"side": "bid", "price_level": "100.00", "new_quantity": "0"},
                {"side": "offer", "price_level": "100.50", "new_quantity": "2.5"},
            ],
        }],
    }, now=101.0)
    assert 100.0 not in state.bids
    assert state.asks[100.5] == 2.5

    # The top-level sequence is connection-wide across interleaved channels.
    heartbeat_1 = {"channel": "heartbeats", "sequence_num": 12, "events": [{"heartbeat_counter": 1}]}
    heartbeat_2 = {"channel": "heartbeats", "sequence_num": 13, "events": [{"heartbeat_counter": 2}]}
    hub.process_coinbase_message(heartbeat_1, now=102.0)
    hub.process_coinbase_message(heartbeat_2, now=103.0)
    assert state.last_message_at == 101.0
    assert state.last_transport_at == 103.0

    with pytest.raises(SequenceGapError):
        hub.process_coinbase_message({
            "channel": "l2_data", "sequence_num": 15, "timestamp": datetime.now(timezone.utc).isoformat(),
            "events": [{
                "type": "update",
                "product_id": "BTC-USD",
                "updates": [],
            }],
        }, now=104.0)
    assert state.connected is False
    assert state.book_ready is False
    assert state.health_reason == "SEQUENCE_GAP"


def test_partial_or_stale_feed_is_never_called_neutral_confirmation() -> None:
    hub = MultiVenueMarketDataHub(_settings())
    hub._set_connected("bybit", True, now=100.0)
    hub.process_bybit_message(_bybit_book(), now=100.0)
    hub.process_bybit_message({
        "topic": "publicTrade.BTCUSDT",
        "data": [{"T": int(time() * 1000), "s": "BTCUSDT", "S": "Buy", "v": "1", "p": "100", "i": "one"}],
    }, now=101.0)

    partial = hub.snapshot("BTCUSDT", now=102.0)
    assert partial["status"] == "DEGRADED"
    assert partial["flow_consensus"] == "UNAVAILABLE"
    assert partial["single_venue_flow_bias"] == "BULLISH"
    assert partial["observed_liquidations"]["available"] is True

    stale = hub.snapshot("BTCUSDT", now=200.0)
    assert stale["status"] == "UNAVAILABLE"
    assert stale["venues"]["bybit"]["health"] == "STALE"
    assert stale["observed_liquidations"]["observed"] is False


def test_books_events_and_dedupe_indexes_remain_bounded() -> None:
    hub = MultiVenueMarketDataHub(_settings())
    hub._set_connected("bybit", True, now=100.0)
    bids = [[str(100 - index * 0.1), "1"] for index in range(60)]
    asks = [[str(101 + index * 0.1), "1"] for index in range(60)]
    book = _bybit_book()
    book["data"]["b"], book["data"]["a"] = bids, asks
    hub.process_bybit_message(book, now=100.0)
    state = hub.states[("bybit", "BTCUSDT")]
    for index in range(250):
        hub.process_bybit_message({
            "topic": "publicTrade.BTCUSDT",
            "data": [{
                "s": "BTCUSDT", "S": "Buy" if index % 2 else "Sell",
                "T": int(time() * 1000),
                "v": "1", "p": "100", "i": f"trade-{index}",
            }],
        }, now=101.0 + index / 1000)
    assert len(state.bids) <= 20
    assert len(state.asks) <= 20
    assert len(state.trades) == 1
    assert state.trades[0]["trade_count"] == 250
    assert len(state._trade_id_set) == 100
    before = len(hub.states)
    hub.process_bybit_message({
        "topic": "publicTrade.UNKNOWNUSDT",
        "data": [{"s": "UNKNOWNUSDT", "S": "Buy", "v": "1", "p": "1", "i": "unknown"}],
    }, now=102.0)
    assert len(hub.states) == before


def test_selected_symbol_is_registered_automatically_with_bounded_lru_eviction() -> None:
    hub = MultiVenueMarketDataHub(_settings(multi_venue_max_symbols=2))
    generation = hub._subscription_generation

    xrp = hub.snapshot("XRPUSDT")
    assert xrp["status"] == "SUBSCRIBING"
    assert xrp["registration"]["reason"] == "dynamic_registration_started"
    assert "XRPUSDT" in hub.symbols
    assert ("bybit", "XRPUSDT") in hub.states
    assert ("coinbase", "XRPUSDT") in hub.states
    assert hub._subscription_generation == generation + 1

    eth = hub.snapshot("ETHUSDT")
    assert eth["status"] == "SUBSCRIBING"
    assert eth["registration"]["evicted_symbol"] == "BTCUSDT"
    assert hub.symbols == ["XRPUSDT", "ETHUSDT"]
    assert len(hub.states) == 4
    assert hub.metrics["dynamic_symbol_registrations"] == 2
    assert hub.metrics["dynamic_symbol_evictions"] == 1


def test_invalid_dynamic_symbol_does_not_mutate_the_shared_hub() -> None:
    hub = MultiVenueMarketDataHub(_settings())
    before_symbols = list(hub.symbols)
    before_generation = hub._subscription_generation
    snapshot = hub.snapshot("BTC/USD")
    assert snapshot["status"] == "UNAVAILABLE"
    assert snapshot["reason"] == "invalid_usdt_symbol"
    assert hub.symbols == before_symbols
    assert hub._subscription_generation == before_generation


def test_late_bybit_ack_for_evicted_symbol_cannot_mark_replacement_ready() -> None:
    hub = MultiVenueMarketDataHub(_settings(multi_venue_max_symbols=1))
    hub.snapshot("XRPUSDT")
    hub.process_bybit_message(
        {"op": "subscribe", "req_id": "mv:BTCUSDT", "success": True},
        now=100.0,
    )
    assert "BTCUSDT" not in hub.symbols
    assert "XRPUSDT" not in hub._bybit_subscriptions
    assert hub.states[("bybit", "XRPUSDT")].liquidation_stream_ready is False


def test_crossed_book_is_rejected_and_cleared() -> None:
    hub = MultiVenueMarketDataHub(_settings())
    hub._set_connected("bybit", True, now=100.0)
    crossed = _bybit_book()
    crossed["data"]["b"] = [["101", "1"]]
    crossed["data"]["a"] = [["100", "1"]]
    with pytest.raises(BookIntegrityError):
        hub.process_bybit_message(crossed, now=100.0)
    state = hub.states[("bybit", "BTCUSDT")]
    assert state.book_ready is False
    assert not state.bids and not state.asks


def _complete_live(multi_venue: dict) -> dict:
    return {
        "data_complete": True,
        "depth_imbalance": 0.20,
        "taker_buy_sell_ratio": 1.20,
        "funding_rate": 0.0,
        "oi_change_pct": 1.0,
        "price_change_pct": 1.0,
        "spread_bps": 1.0,
        "multi_venue": multi_venue,
    }


def test_confirmed_cross_venue_opposition_vetoes_but_missing_feed_does_not() -> None:
    opposed = {"flow_confirmed": True, "flow_consensus": "BEARISH", "flow_score": -0.5, "fresh_venue_count": 2}
    candidate = {"direction": "BULLISH", "score": 75, "risk_flags": [], "causal_radar": True}
    apply_live_confirmation(candidate, _complete_live(opposed))
    assert candidate["status"] == "LIVE_CONFIRMATION_REJECTED"
    assert candidate["advanced_confirmation"]["cross_venue_evidence"]["status"] == "OPPOSED"
    assert candidate["advanced_confirmation"]["checks"]["cross_venue_not_opposed"] is False

    unavailable = {"available": False, "flow_confirmed": False, "flow_consensus": "UNAVAILABLE"}
    candidate = {"direction": "BULLISH", "score": 75, "risk_flags": [], "causal_radar": True}
    apply_live_confirmation(candidate, _complete_live(unavailable))
    assert candidate["status"] == "LIVE_CONFIRMED_REVIEW"
    assert candidate["advanced_confirmation"]["cross_venue_evidence"]["status"] == "UNAVAILABLE"
    assert any("not counted as neutral" in item for item in candidate["advanced_confirmation"]["supporting_warnings"])


def test_two_venue_displayed_liquidity_instability_vetoes_live_confirmation() -> None:
    unstable = {
        "flow_confirmed": True,
        "flow_consensus": "BULLISH",
        "flow_score": 0.5,
        "fresh_venue_count": 2,
        "displayed_liquidity_stability": {
            "status": "ELEVATED",
            "qualified_venue_count": 2,
            "elevated_venue_count": 2,
            "publication_veto": True,
        },
    }
    candidate = {"direction": "BULLISH", "score": 75, "risk_flags": [], "causal_radar": True}
    apply_live_confirmation(candidate, _complete_live(unstable))
    assert candidate["status"] == "LIVE_CONFIRMATION_REJECTED"
    assert candidate["advanced_confirmation"]["checks"]["displayed_liquidity_stable"] is False
    assert any("liquidity instability" in item.lower() for item in candidate["risk_flags"])


def test_cached_intelligence_refreshes_live_snapshot_without_duplicate_source_labels(monkeypatch) -> None:
    snapshots = iter([
        {"available": True, "status": "HEALTHY", "symbol": "BTCUSDT", "venues": {}},
        {"available": False, "status": "UNAVAILABLE", "symbol": "BTCUSDT", "venues": {}},
    ])
    monkeypatch.setattr(
        data_aggregator,
        "get_multi_venue_snapshot",
        lambda symbol, settings: next(snapshots),
    )
    intelligence = {
        "meta": {
            "sources_available": ["candles", "multi_venue_ws"],
            "sources_failed": ["options", "multi_venue_ws"],
            "total_sources": 4,
        }
    }
    data_aggregator.attach_live_multi_venue_snapshot(intelligence, "BTCUSDT", _settings())
    assert intelligence["meta"]["sources_available"].count("multi_venue_ws") == 1
    data_aggregator.attach_live_multi_venue_snapshot(intelligence, "BTCUSDT", _settings())
    assert "multi_venue_ws" not in intelligence["meta"]["sources_available"]
    assert intelligence["meta"]["sources_failed"].count("multi_venue_ws") == 1


def test_heartbeat_cannot_keep_a_frozen_book_healthy() -> None:
    hub = MultiVenueMarketDataHub(_settings())
    _seed_books(hub, now=100.0)
    hub.process_coinbase_message(
        {"channel": "heartbeats", "sequence_num": 11, "events": [{"heartbeat_counter": 2}]},
        now=120.0,
    )
    snapshot = hub.snapshot("BTCUSDT", now=120.0)
    assert snapshot["venues"]["coinbase"]["transport_age_seconds"] == 0.0
    assert snapshot["venues"]["coinbase"]["book_age_seconds"] == 20.0
    assert snapshot["venues"]["coinbase"]["health"] == "STALE"


def test_coinbase_connection_sequence_accepts_interleaved_products() -> None:
    hub = MultiVenueMarketDataHub(_settings(multi_venue_symbols=["BTCUSDT", "ETHUSDT"]))
    hub._set_connected("coinbase", True, now=100.0)
    btc = _coinbase_book(sequence=10)
    eth = _coinbase_book(sequence=11)
    eth["events"][0]["product_id"] = "ETH-USD"
    hub.process_coinbase_message(btc, now=100.0)
    hub.process_coinbase_message(eth, now=100.0)

    btc_update = _coinbase_book(sequence=12, event_type="update")
    btc_update["events"][0]["updates"] = [
        {"side": "bid", "price_level": "100.00", "new_quantity": "2.5"},
    ]
    hub.process_coinbase_message(btc_update, now=101.0)
    assert hub._coinbase_sequence == 12


def test_disconnect_clears_pre_gap_rolling_evidence() -> None:
    hub = MultiVenueMarketDataHub(_settings())
    _seed_books(hub)
    hub.process_bybit_message({
        "topic": "publicTrade.BTCUSDT",
        "data": [{"T": int(time() * 1000), "s": "BTCUSDT", "S": "Buy", "v": "2", "p": "100", "i": "epoch"}],
    }, now=101.0)
    state = hub.states[("bybit", "BTCUSDT")]
    assert state.trades
    hub._set_connected("bybit", False, reason="TEST_GAP")
    assert not state.trades
    assert not state.book_events
    assert not state._trade_id_set
    assert state.health_reason == "TEST_GAP"


def test_book_snapshot_is_baseline_not_quote_activity() -> None:
    hub = MultiVenueMarketDataHub(_settings())
    hub._set_connected("bybit", True, now=100.0)
    hub.process_bybit_message(_bybit_book(), now=100.0)
    state = hub.states[("bybit", "BTCUSDT")]
    assert not state.book_events
    delta = _bybit_book(message_type="delta", update_id=101, sequence=501)
    delta["data"]["b"] = [["100.00", "2.5"]]
    delta["data"]["a"] = []
    hub.process_bybit_message(delta, now=101.0)
    assert state.book_events[0]["addition_count"] == 1
    decrease = _bybit_book(message_type="delta", update_id=102, sequence=502)
    decrease["data"]["b"] = [["100.00", "2.0"]]
    decrease["data"]["a"] = []
    hub.process_bybit_message(decrease, now=101.5)
    assert state.book_events[0]["addition_count"] == 1
    assert state.book_events[0]["removal_count"] == 1



def test_delayed_provider_trade_is_rejected() -> None:
    hub = MultiVenueMarketDataHub(_settings())
    hub._set_connected("bybit", True, now=100.0)
    hub.process_bybit_message(_bybit_book(), now=100.0)
    hub.process_bybit_message({
        "topic": "publicTrade.BTCUSDT",
        "data": [{"T": 1, "s": "BTCUSDT", "S": "Buy", "v": "1", "p": "100", "i": "stale"}],
    }, now=101.0)
    state = hub.states[("bybit", "BTCUSDT")]
    assert not state.trades
    assert hub.metrics["stale_events_dropped"] == 1


def test_flow_requires_warmup_count_and_notional() -> None:
    hub = MultiVenueMarketDataHub(_settings(
        multi_venue_flow_warmup_seconds=10.0,
        multi_venue_min_flow_trades=2,
        multi_venue_min_flow_notional_usd=1_000.0,
    ))
    hub._set_connected("bybit", True, now=100.0)
    hub.process_bybit_message(_bybit_book(), now=100.0)
    for index in range(2):
        hub.process_bybit_message({
            "topic": "publicTrade.BTCUSDT",
            "data": [{"T": int(time() * 1000), "s": "BTCUSDT", "S": "Buy", "v": "2", "p": "100", "i": f"small-{index}"}],
        }, now=101.0 + index)
    assert hub.snapshot("BTCUSDT", now=111.0)["venues"]["bybit"]["trade_flow"]["available"] is False
    hub.process_bybit_message({
        "topic": "publicTrade.BTCUSDT",
        "data": [{"T": int(time() * 1000), "s": "BTCUSDT", "S": "Buy", "v": "10", "p": "100", "i": "qualified"}],
    }, now=110.0)
    flow = hub.snapshot("BTCUSDT", now=111.0)["venues"]["bybit"]["trade_flow"]
    assert flow["available"] is True
    assert flow["trade_count"] == 3


def test_cached_snapshot_is_returned_as_an_isolated_copy() -> None:
    hub = MultiVenueMarketDataHub(_settings())
    hub._set_connected("bybit", True)
    hub._set_connected("coinbase", True)
    hub.process_bybit_message(_bybit_book())
    hub.process_coinbase_message(_coinbase_book())
    first = hub.snapshot("BTCUSDT")
    first["status"] = "MUTATED_BY_CALLER"
    second = hub.snapshot("BTCUSDT")
    assert second["status"] != "MUTATED_BY_CALLER"


def test_production_rejects_insecure_market_data_transport() -> None:
    with pytest.raises(ValueError, match="wss://"):
        MultiVenueMarketDataHub(_settings(app_env="production", bybit_public_ws_url="ws://example.test"))


def test_delayed_order_book_frame_forces_resynchronization() -> None:
    hub = MultiVenueMarketDataHub(_settings())
    hub._set_connected("bybit", True, now=100.0)
    delayed = _bybit_book()
    delayed["ts"] = 1
    with pytest.raises(BookIntegrityError, match="clean snapshot"):
        hub.process_bybit_message(delayed, now=100.0)
    assert hub.metrics["stale_events_dropped"] == 1


def test_missing_order_book_timestamp_forces_resynchronization() -> None:
    hub = MultiVenueMarketDataHub(_settings())
    hub._set_connected("bybit", True, now=100.0)
    missing_time = _bybit_book()
    missing_time.pop("ts")
    with pytest.raises(BookIntegrityError, match="clean snapshot"):
        hub.process_bybit_message(missing_time, now=100.0)
    assert hub.metrics["stale_events_dropped"] == 1


def test_trade_flow_expires_independently_from_a_fresh_book() -> None:
    hub = MultiVenueMarketDataHub(_settings())
    hub._set_connected("bybit", True, now=100.0)
    hub.process_bybit_message(_bybit_book(), now=100.0)
    hub.process_bybit_message({
        "topic": "publicTrade.BTCUSDT",
        "data": [{
            "T": int(time() * 1000), "s": "BTCUSDT", "S": "Buy",
            "v": "2", "p": "100", "i": "old-flow",
        }],
    }, now=101.0)
    delta = _bybit_book(message_type="delta", update_id=101, sequence=501)
    hub.process_bybit_message(delta, now=116.0)

    venue = hub.snapshot("BTCUSDT", now=117.0)["venues"]["bybit"]
    assert venue["health"] == "HEALTHY"
    assert venue["trade_flow"]["trade_count"] == 1
    assert venue["trade_flow"]["age_seconds"] == 16.0
    assert venue["trade_flow"]["available"] is False


def test_initial_sync_and_stale_book_watchdogs_fail_closed() -> None:
    observed = multi_venue_ws.monotonic()
    hub = MultiVenueMarketDataHub(_settings(
        multi_venue_initial_sync_timeout_seconds=10.0,
        multi_venue_stale_seconds=15.0,
    ))
    hub._set_connected("coinbase", True, now=observed - 11.0)
    with pytest.raises(SubscriptionError, match="initial order-book sync timed out"):
        hub._assert_stream_readiness("coinbase", ["BTCUSDT"], observed - 11.0)

    hub._set_connected("coinbase", True, now=observed - 20.0)
    hub.process_coinbase_message(_coinbase_book(), now=observed - 16.0)
    with pytest.raises(BookIntegrityError, match="Level-2 stream became stale"):
        hub._assert_stream_readiness("coinbase", ["BTCUSDT"], observed - 20.0)


def test_rejected_public_subscriptions_are_quarantined() -> None:
    hub = MultiVenueMarketDataHub(_settings())
    hub._set_connected("bybit", True, now=100.0)
    hub.process_bybit_message({
        "op": "subscribe",
        "success": False,
        "req_id": "mv:BTCUSDT",
        "ret_msg": "unsupported symbol",
    }, now=100.0)
    assert hub.states[("bybit", "BTCUSDT")].health_reason == "SUBSCRIPTION_REJECTED"
    assert hub.quarantined_subscriptions["bybit_symbols"] == ["BTCUSDT"]

    hub._set_connected("coinbase", True, now=101.0)
    hub._bybit_rejected_symbols["BTCUSDT"] = multi_venue_ws.monotonic() - 1.0
    assert hub._active_bybit_symbols() == ["BTCUSDT"]

    with pytest.raises(SubscriptionError, match="rejected"):
        hub.process_coinbase_message({
            "type": "error",
            "channel": "error",
            "sequence_num": 0,
            "message": "BTC-USD is not available",
        }, now=101.0)
    assert hub.states[("coinbase", "BTCUSDT")].health_reason == "SUBSCRIPTION_REJECTED"
    assert hub.quarantined_subscriptions["coinbase_products"] == ["BTC-USD"]


    hub._coinbase_rejected_products["BTC-USD"] = multi_venue_ws.monotonic() - 1.0
    assert hub._active_coinbase_products(["BTC-USD"]) == ["BTC-USD"]

@pytest.mark.asyncio
async def test_coinbase_subscriptions_are_batched_per_channel(monkeypatch) -> None:
    symbols = [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT",
        "AVAXUSDT", "LINKUSDT", "DOTUSDT", "LTCUSDT", "BCHUSDT", "UNIUSDT",
    ]
    hub = MultiVenueMarketDataHub(_settings(multi_venue_symbols=symbols))
    sent: list[dict] = []
    ready = asyncio.Event()

    class FakeSocket:
        async def send(self, raw: str) -> None:
            sent.append(multi_venue_ws.json.loads(raw))
            if len(sent) == 3:
                ready.set()

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Future()

    class FakeConnection:
        async def __aenter__(self):
            return FakeSocket()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr(multi_venue_ws.websockets, "connect", lambda *args, **kwargs: FakeConnection())
    task = asyncio.create_task(hub.run_coinbase())
    try:
        await asyncio.wait_for(ready.wait(), timeout=1.0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert [payload["channel"] for payload in sent] == ["heartbeats", "level2", "market_trades"]
    assert len(sent[1]["product_ids"]) == 12
    assert sent[1]["product_ids"] == sent[2]["product_ids"]


@pytest.mark.asyncio
async def test_dynamic_selection_refreshes_live_coinbase_subscription_set(monkeypatch) -> None:
    hub = MultiVenueMarketDataHub(_settings(multi_venue_max_symbols=2))
    sent: list[dict] = []
    initial_ready = asyncio.Event()
    refreshed_ready = asyncio.Event()

    class FakeSocket:
        async def send(self, raw: str) -> None:
            payload = multi_venue_ws.json.loads(raw)
            sent.append(payload)
            if payload.get("channel") == "market_trades":
                products = payload.get("product_ids", [])
                if "XRP-USD" in products:
                    refreshed_ready.set()
                else:
                    initial_ready.set()

        async def recv(self) -> str:
            await asyncio.Future()

    class FakeConnection:
        async def __aenter__(self):
            return FakeSocket()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr(
        multi_venue_ws.websockets,
        "connect",
        lambda *args, **kwargs: FakeConnection(),
    )
    task = asyncio.create_task(hub.run_coinbase())
    try:
        await asyncio.wait_for(initial_ready.wait(), timeout=1.0)
        selected = hub.snapshot("XRPUSDT")
        assert selected["status"] == "SUBSCRIBING"
        await asyncio.wait_for(refreshed_ready.wait(), timeout=3.0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    refreshed_level2 = [
        payload for payload in sent
        if payload.get("channel") == "level2" and "XRP-USD" in payload.get("product_ids", [])
    ]
    assert refreshed_level2
    assert set(refreshed_level2[-1]["product_ids"]) == {"BTC-USD", "XRP-USD"}


@pytest.mark.asyncio
async def test_dynamic_selection_refreshes_live_bybit_subscription_set(monkeypatch) -> None:
    hub = MultiVenueMarketDataHub(_settings(multi_venue_max_symbols=2))
    sent: list[dict] = []
    initial_ready = asyncio.Event()
    refreshed_ready = asyncio.Event()

    class FakeSocket:
        async def send(self, raw: str) -> None:
            payload = multi_venue_ws.json.loads(raw)
            sent.append(payload)
            if payload.get("req_id") == "mv:BTCUSDT":
                initial_ready.set()
            if payload.get("req_id") == "mv:XRPUSDT":
                refreshed_ready.set()

        async def recv(self) -> str:
            await asyncio.Future()

    class FakeConnection:
        async def __aenter__(self):
            return FakeSocket()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr(
        multi_venue_ws.websockets,
        "connect",
        lambda *args, **kwargs: FakeConnection(),
    )
    task = asyncio.create_task(hub.run_bybit())
    try:
        await asyncio.wait_for(initial_ready.wait(), timeout=1.0)
        selected = hub.snapshot("XRPUSDT")
        assert selected["status"] == "SUBSCRIBING"
        await asyncio.wait_for(refreshed_ready.wait(), timeout=3.0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    xrp_subscription = next(payload for payload in sent if payload.get("req_id") == "mv:XRPUSDT")
    assert set(xrp_subscription["args"]) == {
        "orderbook.50.XRPUSDT",
        "publicTrade.XRPUSDT",
        "allLiquidation.XRPUSDT",
    }
