"""Tests for the new subsystems: Backtesting, walk-forward training, alerts, and CoinDesk WS."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from app.data_sources.binance_public import Candle
from app.data_sources.coindesk_ws import CoindeskWSSubscriber
from app.ml.features import FEATURE_CONTRACT
from app.ml.model import WEIGHTS_FILE, RidgeRegression, train_walk_forward_model, get_weights_filepath, predict_probability_from_model
from app.quant.backtest import run_backtest
from app.quant.feature_engine import compute_quant_features
from app.settings import get_settings
from app.utils.alerts import trigger_system_notification


def _mock_candles() -> list[Candle]:
    rows = []
    for i in range(120):
        price = 100.0 + i * 0.05
        rows.append(Candle(
            open_time=i * 60_000, open=price - 0.04, high=price + 0.10,
            low=price - 0.10, close=price, volume=1000.0 + i,
            close_time=i * 60_000 + 59_999, quote_volume=price * (1000.0 + i),
            trade_count=100 + i, taker_buy_base_volume=550.0,
            taker_buy_quote_volume=price * 550.0,
        ))
    return rows


def test_ridge_regression():
    X = np.array([[1.0, 2.0], [2.0, 3.0], [3.0, 4.0]])
    y = np.array([5.0, 7.0, 9.0])
    model = RidgeRegression(alpha=0.1)
    model.fit(X, y)
    preds = model.predict(X)
    assert len(preds) == 3
    assert abs(preds[0] - 5.0) < 0.1


def test_feature_engine_preserves_aggregated_oi_change_and_structure() -> None:
    """Regression: the aggregator supplies a calculated OI delta, not raw history."""
    candles = _mock_candles()
    features = compute_quant_features({
        "candles": candles,
        "ticker": {"lastPrice": str(candles[-1].close), "quoteVolume": "1000000"},
        "order_book": {"bids": [[106.0, 20.0]], "asks": [[106.1, 15.0]]},
        "multi_tf_candles": {},
        "funding": {"funding_rate": 0.0001},
        "open_interest": {"open_interest": 1_000_000},
        "derivatives": {"oi_history": {"available": True, "oi_change_pct": 12.3}},
        "recent_trades": [],
        "macro": {},
        "sentiment": {},
        "calendar": [],
        "meta": {"sources_available": ["candles", "ticker", "order_book"], "sources_failed": []},
    })

    assert features["derivatives"]["oi_delta"] == {
        "oi_change_pct": 12.3,
        "oi_trend": "RISING_FAST",
    }
    assert "market_structure" in features
    assert features["market_structure"]["phase"] in {
        "ACCUMULATION", "DISTRIBUTION", "MARKUP", "MARKDOWN", "RANGING",
    }


@pytest.mark.anyio
async def test_backtest_engine(monkeypatch):
    async def _unexpected_live_fetch(*_args, **_kwargs):
        raise AssertionError("Historical backtests must not fetch live market intelligence")

    monkeypatch.setattr("app.analysis_pipeline.fetch_market_intelligence", _unexpected_live_fetch)
    settings = get_settings()
    candles = _mock_candles()
    result = await run_backtest("BTCUSDT", "15m", candles, settings, start_offset=80)
    assert "status" in result
    assert result["status"] == "completed"
    assert "total_trades" in result
    assert "win_rate" in result
    assert "sharpe_ratio" in result



def test_walk_forward_training():
    settings = get_settings()
    candles = _mock_candles()
    ticker = {"lastPrice": "105.0", "highPrice": "106.0", "lowPrice": "104.0"}
    book = {"bids": [[105.0, 10.0]], "asks": [[105.1, 10.0]]}

    symbol = "BTCUSDT"
    timeframe = "15m"
    weights_file = get_weights_filepath(symbol, timeframe)

    # Delete weights file if exists to test generation
    if weights_file.exists():
        weights_file.unlink()

    weights = train_walk_forward_model(
        symbol=symbol,
        timeframe=timeframe,
        candles=candles,
        ticker=ticker,
        order_book=book,
        train_fraction=0.7,
    )
    assert weights_file.exists()
    assert "feature_names" in weights
    assert "weights" in weights
    assert "train_ic" in weights
    assert weights["feature_contract"] == FEATURE_CONTRACT
    assert not any(name.startswith("derived_") for name in weights["feature_names"])

    # Cleanup after test
    weights_file.unlink()


def test_model_validation_controls():
    # 1. Mismatch test: model trained for BTCUSDT, loaded for ETHUSDT
    mock_weights = {
        "symbol": "BTCUSDT",
        "timeframe": "15m",
        "feature_contract": FEATURE_CONTRACT,
        "feature_names": ["stat_hurst_exponent"],
        "weights": [1.0],
        "intercept": 0.0,
        "mean": [0.5],
        "std": [0.1],
        "train_ic": 0.12,
        "test_ic": 0.08,
    }

    symbol = "BTCUSDT"
    timeframe = "15m"
    weights_file = get_weights_filepath(symbol, timeframe)

    # Clean up before test
    if weights_file.exists():
        weights_file.unlink()

    with open(weights_file, "w", encoding="utf-8") as f:
        json.dump(mock_weights, f, indent=2)

    try:
        # Load mismatching symbol
        pred_mismatch = predict_probability_from_model(
            {"stat_hurst_exponent": 0.5}, symbol="ETHUSDT", timeframe="15m"
        )
        assert pred_mismatch is None

        # Load mismatching timeframe
        pred_tf_mismatch = predict_probability_from_model(
            {"stat_hurst_exponent": 0.5}, symbol="BTCUSDT", timeframe="1h"
        )
        assert pred_tf_mismatch is None

        # Valid load
        pred_ok = predict_probability_from_model(
            {"stat_hurst_exponent": 0.5}, symbol="BTCUSDT", timeframe="15m"
        )
        assert pred_ok is not None
        assert pred_ok["test_ic"] == 0.08

        # 2. Weak out-of-sample IC test (test_ic = 0.02 < MIN_TEST_IC = 0.05)
        mock_weights["test_ic"] = 0.02
        with open(weights_file, "w", encoding="utf-8") as f:
            json.dump(mock_weights, f, indent=2)

        pred_weak = predict_probability_from_model(
            {"stat_hurst_exponent": 0.5}, symbol="BTCUSDT", timeframe="15m"
        )
        assert pred_weak is None

        # 3. Standalone probability remains the causal baseline. A separately
        # trained research artifact is never allowed to enter live scoring.
        mock_weights["test_ic"] = 0.20
        with open(weights_file, "w", encoding="utf-8") as f:
            json.dump(mock_weights, f, indent=2)

        stats = {
            "available": True,
            "observations": 300,
            "return_volatility": 0.01,
            "return_mean": 0.0001,
            "excess_kurtosis": 0.5,
            "trend_strength_z": 0.0,
            "return_autocorrelation_lag1": 0.0,
            "price_z_score": 0.0,
        }
        micro = {"depth_imbalance": 0.0}
        regime = {"probabilities": {"mean_reverting": 0.0}, "confidence": 0.0}

        from app.quant.probability import forecast_distribution
        forecast_with_artifact_present = forecast_distribution(
            stats, micro, regime, symbol="BTCUSDT", timeframe="15m"
        )
        assert forecast_with_artifact_present["confidence"] == 0.20
        assert (
            forecast_with_artifact_present["model"]
            == "causal_market_context_baseline"
        )

        # Baseline fallback cap verification (no weights file or suppressed weights)
        weights_file.unlink()
        forecast_baseline = forecast_distribution(
            stats, micro, regime, symbol="BTCUSDT", timeframe="15m"
        )
        # Expected baseline confidence should be capped at 0.20
        assert forecast_baseline["confidence"] == 0.20
    finally:
        if weights_file.exists():
            weights_file.unlink()


@patch("subprocess.Popen")
def test_system_notification_popup(mock_popen):
    trigger_system_notification("Test Alert", "This is a test notification")
    # Verify notification method triggers popen call on win32 fallback script
    import sys
    if sys.platform == "win32":
        assert mock_popen.called


def test_coindesk_websocket_subscriber_initialization():
    settings = get_settings()
    subscriber = CoindeskWSSubscriber("BTCUSDT", "15m", settings)
    assert subscriber.websocket_url == "wss://streamer.cryptocompare.com/v2"


def test_dynamic_ai_determined_setup():
    from app.brains.decision_engine import build_trade_setup

    decision = {
        "decision": "BUY_WATCH",
        "confidence": 85.0,
        "trade_grade": "A"
    }
    risk_idea = {
        "entry_reference": 100.0,
        "retail_stop": 98.0,
        "smart_stop": 98.0,
        "smart_target": 104.0,
        "risk_reward": 2.0,
        "setup_type": "trend_continuation",
    }
    ai_result = {
        "suggested_entry": 101.5,
        "suggested_stop": 99.5,
        "suggested_targets": [103.5, 105.5, 107.5]
    }

    setup = build_trade_setup(
        symbol="BTCUSDT",
        timeframe="15m",
        decision=decision,
        risk_idea=risk_idea,
        atr=1.5,
        account_size_usd=1000.0,
        risk_pct=1.0,
        ai_result=ai_result,
    )

    assert setup["status"] == "READY_FOR_MANUAL_REVIEW"
    assert setup["entry"]["reference"] == 101.5
    assert setup["stop"]["selected"] == 99.5
    assert setup["targets"]["tp1_1r"] == 103.5
    assert setup["targets"]["tp2_2r"] == 105.5
    assert setup["targets"]["tp3_3r"] == 107.5


def test_json_helper_repair():
    from app.utils.json_helper import repair_json_string, loads_repaired

    # Test 1: Markdown codeblock wrapper
    raw_1 = "```json\n{\n  \"key\": \"value\"\n}\n```"
    assert repair_json_string(raw_1) == '{\n  "key": "value"\n}'

    # Test 2: Unescaped double quotes inside string value
    raw_2 = '{"explanation": "The order book has a "massive" buy wall."}'
    assert repair_json_string(raw_2) == '{"explanation": "The order book has a \\"massive\\" buy wall."}'
    parsed_2 = loads_repaired(raw_2)
    assert parsed_2["explanation"] == 'The order book has a "massive" buy wall.'

    # Test 3: Raw newlines inside string value
    raw_3 = '{"report_md": "Line 1\nLine 2\nLine 3"}'
    assert repair_json_string(raw_3) == '{"report_md": "Line 1\\nLine 2\\nLine 3"}'
    parsed_3 = loads_repaired(raw_3)
    assert parsed_3["report_md"] == "Line 1\nLine 2\nLine 3"

    # Test 4: Trailing commas
    raw_4 = '{"a": 1, "b": [10, 20,],}'
    parsed_4 = loads_repaired(raw_4)
    assert parsed_4 == {"a": 1, "b": [10, 20]}
