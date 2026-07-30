from __future__ import annotations

from time import time

import pytest

from app.data_sources import data_aggregator
from app.data_sources.execution_tape_ws import (
    BookIntegrityError,
    ExecutionTapeHub,
    SOURCE_SPECS,
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
        "multi_venue_min_book_levels": 1,
        "multi_venue_flow_warmup_seconds": 1.0,
        "multi_venue_min_flow_trades": 1,
        "multi_venue_min_flow_notional_usd": 1.0,
        "multi_venue_max_event_lag_seconds": 10.0,
        "multi_venue_stale_seconds": 15.0,
        "multi_venue_trade_window_seconds": 60.0,
        "multi_venue_liquidation_window_seconds": 300.0,
        "execution_tape_large_trade_notional_usd": 1_000.0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _now_ms() -> int:
    return int(time() * 1_000)


def _connect(hub: ExecutionTapeHub, source: str, now: float = 100.0) -> None:
    hub._set_connected(source, True, now=now)


def _binance_trade(
    *,
    trade_id: int,
    maker_buyer: bool,
    price: str = "100",
    quantity: str = "1",
) -> dict:
    return {
        "stream": "btcusdt@aggTrade",
        "data": {
            "e": "aggTrade",
            "E": _now_ms(),
            "T": _now_ms(),
            "s": "BTCUSDT",
            "a": trade_id,
            "p": price,
            "q": quantity,
            "m": maker_buyer,
        },
    }


def _bybit_trade(
    *,
    trade_id: str,
    side: str,
    price: str = "100",
    quantity: str = "1",
) -> dict:
    return {
        "topic": "publicTrade.BTCUSDT",
        "ts": _now_ms(),
        "data": [{
            "T": _now_ms(),
            "s": "BTCUSDT",
            "S": side,
            "p": price,
            "v": quantity,
            "i": trade_id,
        }],
    }


def test_only_binance_and_bybit_spot_perpetual_sources_are_configured() -> None:
    assert set(SOURCE_SPECS) == {
        "binance_spot",
        "binance_perp",
        "bybit_spot",
        "bybit_perp",
    }
    assert {row["market"] for row in SOURCE_SPECS.values()} == {"SPOT", "PERPETUAL"}


def test_binance_aggtrade_maker_flag_is_inverted_to_taker_side() -> None:
    hub = ExecutionTapeHub(_settings())
    _connect(hub, "binance_spot")
    hub.process_binance_message(
        "binance_spot",
        _binance_trade(trade_id=1, maker_buyer=True, quantity="2"),
        now=101.0,
    )
    hub.process_binance_message(
        "binance_spot",
        _binance_trade(trade_id=2, maker_buyer=False, quantity="3"),
        now=102.0,
    )
    flow = hub.states[("binance_spot", "BTCUSDT")].snapshot(
        stale_seconds=15,
        trade_window_seconds=60,
        liquidation_window_seconds=300,
        flow_warmup_seconds=1,
        min_flow_trades=1,
        min_flow_notional=1,
        now=103.0,
    )["trade_flow"]
    assert flow["sell_notional"] == 200.0
    assert flow["buy_notional"] == 300.0
    assert flow["aggressive_buy_ratio"] == 0.6


def test_bybit_public_trade_side_is_the_taker_side() -> None:
    hub = ExecutionTapeHub(_settings())
    _connect(hub, "bybit_perp")
    hub.process_bybit_message(
        "bybit_perp",
        _bybit_trade(trade_id="buy-1", side="Buy", quantity="2"),
        now=101.0,
    )
    hub.process_bybit_message(
        "bybit_perp",
        _bybit_trade(trade_id="sell-1", side="Sell", quantity="1"),
        now=102.0,
    )
    flow = hub.snapshot("BTCUSDT", now=103.0)["sources"]["bybit_perp"]["trade_flow"]
    assert flow["buy_notional"] == 200.0
    assert flow["sell_notional"] == 100.0
    assert flow["active_aggressor"] == "BUYERS"


def test_trade_ids_are_deduplicated() -> None:
    hub = ExecutionTapeHub(_settings())
    _connect(hub, "binance_spot")
    payload = _binance_trade(trade_id=9, maker_buyer=False)
    hub.process_binance_message("binance_spot", payload, now=101.0)
    hub.process_binance_message("binance_spot", payload, now=102.0)
    flow = hub.snapshot("BTCUSDT", now=103.0)["sources"]["binance_spot"]["trade_flow"]
    assert flow["trade_count"] == 1
    assert flow["buy_notional"] == 100.0


def test_single_qualified_source_produces_low_confidence_actual_flow() -> None:
    hub = ExecutionTapeHub(_settings())
    _connect(hub, "binance_spot")
    hub.process_binance_message(
        "binance_spot",
        _binance_trade(trade_id=1, maker_buyer=False, price="100"),
        now=101.0,
    )
    hub.process_binance_message(
        "binance_spot",
        _binance_trade(trade_id=2, maker_buyer=False, price="101"),
        now=102.0,
    )
    result = hub.snapshot("BTCUSDT", now=103.0)
    assert result["actual_flow"]["available"] is True
    assert result["actual_flow"]["status"] == "BUYING_CONFIRMED"
    assert result["actual_flow"]["confidence"] == "LOW"
    assert result["actual_flow"]["cross_market_alignment"] == "SPOT_ONLY"
    assert result["required_source_count"] == 1


def test_spot_perpetual_agreement_raises_confidence_without_becoming_a_gate() -> None:
    hub = ExecutionTapeHub(_settings())
    for source in ("binance_spot", "bybit_perp"):
        _connect(hub, source)
    hub.process_binance_message(
        "binance_spot",
        _binance_trade(trade_id=1, maker_buyer=False, price="100"),
        now=101.0,
    )
    hub.process_binance_message(
        "binance_spot",
        _binance_trade(trade_id=2, maker_buyer=False, price="101"),
        now=102.0,
    )
    hub.process_bybit_message(
        "bybit_perp",
        _bybit_trade(trade_id="1", side="Buy", price="100"),
        now=101.0,
    )
    hub.process_bybit_message(
        "bybit_perp",
        _bybit_trade(trade_id="2", side="Buy", price="101"),
        now=102.0,
    )
    actual = hub.snapshot("BTCUSDT", now=103.0)["actual_flow"]
    assert actual["status"] == "BUYING_CONFIRMED"
    assert actual["cross_market_alignment"] == "ALIGNED"
    assert actual["confidence"] == "MEDIUM"


def test_aggression_without_price_progress_is_classified_as_absorption() -> None:
    hub = ExecutionTapeHub(_settings())
    _connect(hub, "binance_spot")
    for trade_id in range(1, 5):
        hub.process_binance_message(
            "binance_spot",
            _binance_trade(trade_id=trade_id, maker_buyer=False, price="100"),
            now=100.0 + trade_id,
        )
    actual = hub.snapshot("BTCUSDT", now=106.0)["actual_flow"]
    assert actual["status"] == "BUYERS_ABSORBED"
    assert actual["absorption"] == "BUYERS_ABSORBED"
    assert actual["price_response"] == "NO_UPWARD_PROGRESS"


def test_flow_exhaustion_compares_prior_and_recent_window_halves() -> None:
    hub = ExecutionTapeHub(_settings(multi_venue_stale_seconds=120.0))
    _connect(hub, "binance_spot", now=99.0)
    state = hub.states[("binance_spot", "BTCUSDT")]
    state.record_trade(taker_side="BUY", price=100, size=10, event_id="old", now=110.0)
    state.record_trade(taker_side="BUY", price=101, size=1, event_id="new-buy", now=150.0)
    state.record_trade(taker_side="SELL", price=101, size=1, event_id="new-sell", now=151.0)
    flow = hub.snapshot("BTCUSDT", now=160.0)["sources"]["binance_spot"]["trade_flow"]
    assert flow["exhaustion"] == "BUYER_EXHAUSTION"
    assert flow["verdict"] == "BUYER_EXHAUSTION"


def test_binance_and_bybit_liquidation_side_semantics_are_normalized() -> None:
    hub = ExecutionTapeHub(_settings())
    for source in ("binance_perp", "bybit_perp"):
        _connect(hub, source)
    hub.process_binance_message(
        "binance_perp",
        {
            "stream": "btcusdt@forceOrder",
            "data": {
                "e": "forceOrder",
                "E": _now_ms(),
                "o": {
                    "s": "BTCUSDT",
                    "S": "SELL",
                    "T": _now_ms(),
                    "ap": "100",
                    "z": "2",
                },
            },
        },
        now=101.0,
    )
    hub.process_bybit_message(
        "bybit_perp",
        {
            "topic": "allLiquidation.BTCUSDT",
            "ts": _now_ms(),
            "data": [{
                "T": _now_ms(),
                "s": "BTCUSDT",
                "S": "Sell",
                "p": "100",
                "v": "3",
            }],
        },
        now=102.0,
    )
    liquidations = hub.snapshot("BTCUSDT", now=103.0)["observed_liquidations"]
    assert liquidations["long_liquidated_notional"] == 200.0
    assert liquidations["short_liquidated_notional"] == 300.0


def test_partial_books_are_bounded_and_crossed_books_fail_closed() -> None:
    hub = ExecutionTapeHub(_settings())
    _connect(hub, "binance_spot")
    state = hub.states[("binance_spot", "BTCUSDT")]
    state.apply_book(
        bids=[[str(100 - index), "1"] for index in range(30)],
        asks=[[str(101 + index), "1"] for index in range(30)],
        snapshot=True,
        update_id=1,
        now=101.0,
    )
    assert len(state.bids) == 20
    assert len(state.asks) == 20
    with pytest.raises(BookIntegrityError):
        state.apply_book(
            bids=[["102", "1"]],
            asks=[["101", "1"]],
            snapshot=True,
            update_id=2,
            now=102.0,
        )
    assert state.book_ready is False


def test_dynamic_symbol_registration_uses_lru_and_four_source_states() -> None:
    hub = ExecutionTapeHub(
        _settings(multi_venue_symbols=["BTCUSDT"], multi_venue_max_symbols=2)
    )
    assert hub.ensure_symbol("ETHUSDT")["registered"] is True
    hub._symbol_last_requested["BTCUSDT"] = (
        hub._symbol_last_requested["ETHUSDT"] + 1.0
    )
    result = hub.ensure_symbol("SOLUSDT")
    assert result["evicted_symbol"] == "ETHUSDT"
    assert set(hub.symbols) == {"BTCUSDT", "SOLUSDT"}
    assert all((source, "SOLUSDT") in hub.states for source in SOURCE_SPECS)


def test_invalid_symbols_are_not_registered() -> None:
    hub = ExecutionTapeHub(_settings())
    assert hub.ensure_symbol("BTCUSD")["registered"] is False
    assert hub.ensure_symbol("../BTCUSDT")["registered"] is False


def test_production_rejects_non_tls_public_feed_endpoints() -> None:
    with pytest.raises(ValueError, match="wss"):
        ExecutionTapeHub(
            _settings(
                app_env="production",
                binance_spot_public_ws_url="ws://example.test/stream",
            )
        )


def test_attach_execution_tape_sets_canonical_contract_and_source_meta(monkeypatch) -> None:
    snapshot = {
        "schema_version": "execution_tape.v1",
        "available": True,
        "status": "PARTIAL",
        "actual_flow": {"available": True, "status": "BUYING_CONFIRMED"},
        "sources": {},
    }
    monkeypatch.setattr(
        data_aggregator,
        "get_execution_tape_snapshot",
        lambda *_args, **_kwargs: snapshot,
    )
    intelligence = {
        "meta": {
            "sources_available": ["candles", "multi_venue_ws"],
            "sources_failed": ["execution_tape_ws"],
        }
    }
    result = data_aggregator.attach_live_execution_tape_snapshot(
        intelligence, "BTCUSDT", _settings()
    )
    assert result["execution_tape"] is snapshot
    assert result["multi_venue"] is snapshot
    assert result["meta"]["sources_available"] == ["candles", "execution_tape_ws"]
    assert result["meta"]["sources_failed"] == []


def _candidate(direction: str = "BULLISH") -> dict:
    return {"direction": direction, "score": 80, "risk_flags": []}


def _live(tape: dict, *, taker_ratio: float = 1.2) -> dict:
    return {
        "data_complete": True,
        "depth_imbalance": 0.10,
        "spread_bps": 2.0,
        "funding_rate": 0.0,
        "oi_change_pct": 1.0,
        "price_change_pct": 1.0,
        "taker_buy_sell_ratio": taker_ratio,
        "execution_tape": tape,
        "planned_notional_usd": 100.0,
        "opposing_depth_notional": 10_000.0,
    }


def test_live_tape_overrides_conflicting_legacy_taker_ratio() -> None:
    tape = {
        "actual_flow": {
            "available": True,
            "status": "SELLING_CONFIRMED",
            "bias": "BEARISH",
            "active_aggressor": "SELLERS",
        },
        "displayed_liquidity_stability": {
            "status": "ELEVATED",
            "publication_veto": False,
        },
    }
    candidate = _candidate()
    apply_live_confirmation(candidate, _live(tape, taker_ratio=1.5))
    assert candidate["advanced_confirmation"]["checks"]["actual_flow_aligned"] is False
    assert candidate["advanced_confirmation"]["checks"]["actual_flow_not_opposed"] is False
    assert candidate["status"] == "LIVE_CONFIRMATION_REJECTED"


def test_confirmed_tape_can_replace_missing_legacy_ratio() -> None:
    tape = {
        "actual_flow": {
            "available": True,
            "status": "BUYING_CONFIRMED",
            "bias": "BULLISH",
            "active_aggressor": "BUYERS",
            "confidence": "LOW",
            "qualified_source_count": 1,
        },
        "displayed_liquidity_stability": {
            "status": "ELEVATED",
            "publication_veto": False,
        },
    }
    candidate = _candidate()
    live = _live(tape)
    live["taker_buy_sell_ratio"] = None
    apply_live_confirmation(candidate, live)
    checks = candidate["advanced_confirmation"]["checks"]
    assert checks["actual_flow_aligned"] is True
    assert checks["execution_evidence_confirmed"] is True
    # Quote cancellation risk remains visible but is not a publication veto.
    assert "displayed_liquidity_stable" not in candidate["advanced_confirmation"]["required_checks"]
    assert candidate["status"] == "LIVE_CONFIRMED_REVIEW"


def test_absorbed_aggression_is_not_directional_confirmation() -> None:
    tape = {
        "actual_flow": {
            "available": True,
            "status": "BUYERS_ABSORBED",
            "bias": "BULLISH",
            "active_aggressor": "BUYERS",
            "absorption": "BUYERS_ABSORBED",
        }
    }
    candidate = _candidate()
    apply_live_confirmation(candidate, _live(tape))
    evidence = candidate["advanced_confirmation"]["actual_flow_evidence"]
    assert evidence["status"] == "BUYERS_ABSORBED"
    assert evidence["aligned"] is False
    assert candidate["status"] == "LIVE_CONFIRMATION_REJECTED"
