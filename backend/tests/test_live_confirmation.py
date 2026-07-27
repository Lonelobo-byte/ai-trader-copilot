from app.data_sources.binance_public import Candle
from app.quant.live_confirmation import verify_main_signal_snapshot
from unittest.mock import patch


def _candles(*, start: float, step: float, last_volume: float) -> list[Candle]:
    result = []
    for index in range(60):
        close = start + index * step
        if index == 59:
            close += 1.0  # completed breakout beyond the preceding candle high
        volume = last_volume if index == 59 else 1_000.0
        open_price = close - (0.50 if index == 59 else 0.15)
        high = close + 0.10 if index == 59 else close + 0.25
        low = close - 0.20 if index == 59 else close - 0.25
        result.append(Candle(
            open_time=index * 60_000,
            open=open_price, high=high, low=low, close=close,
            volume=volume, close_time=(index + 1) * 60_000, quote_volume=volume * close,
            trade_count=100, taker_buy_base_volume=volume * 0.60,
            taker_buy_quote_volume=volume * close * 0.60,
        ))
    return result


def _live_inputs(price: float) -> dict:
    return {
        "order_book": {
            "bids": [[price - 0.01, 30.0], [price - 0.02, 20.0]],
            "asks": [[price + 0.01, 10.0], [price + 0.02, 8.0]],
        },
        "funding": {"funding_rate": 0.0001},
        "derivatives": {
            "oi_history": {"available": True, "oi_change_pct": 1.2},
            "taker_buy_sell_volume": {"available": True, "buy_sell_ratio": 1.10},
        },
    }


def test_main_signal_uses_radar_equivalent_confirmation_gate() -> None:
    primary = _candles(start=100.0, step=0.20, last_volume=2_000.0)
    higher = _candles(start=100.0, step=0.40, last_volume=2_000.0)
    inputs = _live_inputs(primary[-1].close)
    result = verify_main_signal_snapshot(
        symbol="TESTUSDT", timeframe="5m", side="LONG", candles=primary, higher_candles=higher,
        order_book=inputs["order_book"], funding=inputs["funding"], derivatives=inputs["derivatives"],
    )
    assert result["passed"] is True
    assert result["status"] == "LIVE_CONFIRMED_REVIEW"
    assert all(result["structure_checks"].values())
    assert all(result["live_checks"].values())


def test_main_signal_blocks_when_live_depth_opposes_direction() -> None:
    primary = _candles(start=100.0, step=0.20, last_volume=2_000.0)
    higher = _candles(start=100.0, step=0.40, last_volume=2_000.0)
    inputs = _live_inputs(primary[-1].close)
    inputs["order_book"] = {
        "bids": [[primary[-1].close - 0.01, 2.0]],
        "asks": [[primary[-1].close + 0.01, 50.0]],
    }
    result = verify_main_signal_snapshot(
        symbol="TESTUSDT", timeframe="5m", side="LONG", candles=primary, higher_candles=higher,
        order_book=inputs["order_book"], funding=inputs["funding"], derivatives=inputs["derivatives"],
    )
    assert result["passed"] is False
    assert result["status"] == "LIVE_CONFIRMATION_REJECTED"
    assert result["live_checks"]["depth_aligned"] is False


def test_main_signal_accepts_completed_range_sweep_with_context_acceptance() -> None:
    """Neutral/ranging phases must be evaluated as a sweep reversal, not a failed trend."""
    primary = _candles(start=100.0, step=0.20, last_volume=2_000.0)
    higher = _candles(start=100.0, step=0.40, last_volume=2_000.0)
    inputs = _live_inputs(primary[-1].close)
    with (
        patch("app.quant.live_confirmation.classify_market_phase", return_value="RANGING"),
        patch("app.quant.live_confirmation.detect_liquidity_sweep", return_value={"detected": True, "direction": "bullish_reversal_watch"}),
        patch("app.quant.live_confirmation.build_vwap_context", return_value={"available": True, "price_relation": "ABOVE_ALL"}),
        patch("app.quant.live_confirmation.build_volume_profile", return_value={"available": True, "location": "ABOVE_POC_ACCEPTANCE"}),
    ):
        result = verify_main_signal_snapshot(
            symbol="TESTUSDT", timeframe="5m", side="LONG", candles=primary, higher_candles=higher,
            order_book=inputs["order_book"], funding=inputs["funding"], derivatives=inputs["derivatives"],
        )

    assert result["passed"] is True
    assert result["metrics"]["playbook"] == "RANGE_SWEEP_REVERSAL"
    assert result["structure_checks"]["liquidity_sweep_aligned"] is True


def test_main_signal_accepts_accumulation_inside_higher_timeframe_range_with_sweep() -> None:
    """Accumulation within a higher-timeframe range is valid only after acceptance evidence."""
    primary = _candles(start=100.0, step=0.20, last_volume=2_000.0)
    higher = _candles(start=100.0, step=0.40, last_volume=2_000.0)
    inputs = _live_inputs(primary[-1].close)
    with (
        patch("app.quant.live_confirmation.classify_market_phase", side_effect=["ACCUMULATION", "RANGING"]),
        patch("app.quant.live_confirmation.detect_liquidity_sweep", return_value={"detected": True, "direction": "bullish_reversal_watch"}),
        patch("app.quant.live_confirmation.build_vwap_context", return_value={"available": True, "price_relation": "ABOVE_ALL"}),
        patch("app.quant.live_confirmation.build_volume_profile", return_value={"available": True, "location": "ABOVE_POC_ACCEPTANCE"}),
    ):
        result = verify_main_signal_snapshot(
            symbol="TESTUSDT", timeframe="5m", side="LONG", candles=primary, higher_candles=higher,
            order_book=inputs["order_book"], funding=inputs["funding"], derivatives=inputs["derivatives"],
        )

    assert result["passed"] is True
    assert result["metrics"]["playbook"] == "RANGE_AUCTION_SWEEP_REVERSAL"
    assert result["structure_checks"]["primary_range_auction_aligned"] is True


def test_primary_setup_remains_visible_as_tactical_when_higher_timeframe_mismatches() -> None:
    """HTF disagreement blocks publication but must not erase valid LTF evidence."""
    primary = _candles(start=100.0, step=0.20, last_volume=2_000.0)
    higher = _candles(start=100.0, step=0.40, last_volume=2_000.0)
    inputs = _live_inputs(primary[-1].close)
    with patch("app.quant.live_confirmation.classify_market_phase", side_effect=["MARKUP", "RANGING"]):
        result = verify_main_signal_snapshot(
            symbol="TESTUSDT", timeframe="5m", side="LONG", candles=primary, higher_candles=higher,
            order_book=inputs["order_book"], funding=inputs["funding"], derivatives=inputs["derivatives"],
        )

    assert result["passed"] is False
    assert result["scenarios"]["institutional"]["passed"] is False
    assert result["scenarios"]["tactical"]["passed"] is True
    assert result["scenarios"]["tactical"]["status"] == "TACTICAL_CONFIRMED_WATCH"
    assert result["scenarios"]["tactical"]["higher_timeframe_aligned"] is False
