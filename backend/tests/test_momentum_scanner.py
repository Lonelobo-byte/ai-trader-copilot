import pytest
from app.quant.momentum_scanner import (
    _apply_live_confirmation,
    analyze_radar_structure_confluence,
    calculate_ema,
    calculate_rsi,
    calculate_atr,
    calculate_keltner_channels,
    get_breakout_candidates,
)
from app.data_sources.binance_public import Candle

def test_calculate_ema():
    prices = [10.0, 11.0, 12.0, 13.0, 14.0]
    ema = calculate_ema(prices, 3)
    assert len(ema) == len(prices)
    assert ema[0] == 0.0
    assert ema[1] == 0.0
    assert ema[2] > 0.0

def test_calculate_rsi():
    prices = [100.0] * 20
    rsi = calculate_rsi(prices, 14)
    assert len(rsi) == len(prices)
    assert 40 <= rsi[-1] <= 60

def test_calculate_atr():
    highs = [10.0] * 20
    lows = [9.0] * 20
    closes = [9.5] * 20
    atr = calculate_atr(highs, lows, closes, 14)
    assert len(atr) == len(closes)

def test_calculate_keltner_channels():
    closes = [10.0] * 30
    highs = [11.0] * 30
    lows = [9.0] * 30
    basis, upper, lower = calculate_keltner_channels(closes, highs, lows, 20, 2.0)
    assert len(basis) == 30
    assert len(upper) == 30
    assert len(lower) == 30
    assert upper[-1] > basis[-1] > lower[-1]

@pytest.mark.asyncio
async def test_get_breakout_candidates():
    # Mocking system time to ensure candle checks pass
    candidates = await get_breakout_candidates(ltf="15m", htf="4h")
    assert isinstance(candidates, list)
    if candidates:
        candidate = candidates[0]
        assert "symbol" in candidate
        assert "score" in candidate
        assert "rvol" in candidate
        assert "atr_ratio" in candidate
        assert "rsi" in candidate
        assert "price_change_pct" in candidate
        assert "close_price" in candidate
        assert "direction" in candidate
        assert "status" in candidate
        assert "htf_direction" in candidate
        assert "review_status" in candidate
        assert candidate["evaluation_mode"] == "structure_then_live_execution_evidence"
        assert "advanced_confirmation" in candidate
        assert "smc_confluence" in candidate
        if candidate["status"].startswith("CONFIRMED_"):
            assert candidate["structure"]["confirmed"] is True
            assert candidate["mtf_aligned"] is True
            assert candidate["rvol"] >= 1.5


def test_live_confirmation_rejects_opposing_order_flow():
    candidate = {
        "symbol": "TESTUSDT",
        "direction": "BULLISH",
        "score": 70,
        "risk_flags": [],
    }
    _apply_live_confirmation(candidate, {
        "data_complete": True,
        "spread_bps": 2.0,
        "depth_imbalance": -0.12,
        "taker_buy_sell_ratio": 0.86,
        "oi_change_pct": 1.2,
        "funding_rate": 0.0001,
    })
    assert candidate["review_status"] == "WATCH_ONLY"
    assert candidate["status"] == "LIVE_CONFIRMATION_REJECTED"


def test_live_confirmation_requires_all_directional_checks():
    candidate = {
        "symbol": "TESTUSDT",
        "direction": "BEARISH",
        "score": 70,
        "risk_flags": [],
    }
    _apply_live_confirmation(candidate, {
        "data_complete": True,
        "spread_bps": 2.0,
        "depth_imbalance": -0.12,
        "taker_buy_sell_ratio": 0.86,
        "oi_change_pct": 1.2,
        "funding_rate": 0.0001,
    })
    assert candidate["review_status"] == "REVIEW_CANDIDATE"
    assert candidate["status"] == "LIVE_CONFIRMED_REVIEW"
    assert candidate["advanced_confirmation"]["checks"]["depth_aligned"] is True


def test_radar_smc_confluence_is_bounded_context_not_a_signal_gate():
    candles = [
        Candle(
            open_time=index * 60_000,
            open=100.0 + index * 0.2,
            high=100.3 + index * 0.2,
            low=99.9 + index * 0.2,
            close=100.2 + index * 0.2,
            volume=1_000.0 + index,
            close_time=(index + 1) * 60_000,
            quote_volume=100_000.0,
            trade_count=100,
            taker_buy_base_volume=600.0,
            taker_buy_quote_volume=60_000.0,
        )
        for index in range(60)
    ]
    result = analyze_radar_structure_confluence(candles, "BULLISH")
    assert result["phase"] in {"MARKUP", "ACCUMULATION", "DISTRIBUTION", "MARKDOWN", "RANGING"}
    assert -8 <= result["score_adjustment"] <= 12
    assert "liquidity_sweep" in result
    assert "limitations" in result

def test_verify_setup_endpoint_uses_deterministic_evidence():
    from fastapi.testclient import TestClient
    from app.main import app
    from unittest.mock import AsyncMock, patch
    from time import time

    client = TestClient(app)

    now_ms = int(time() * 1000)
    candles = [
        Candle(
            open_time=now_ms - (101 - index) * 60_000,
            open=100.0 + index * 0.1,
            high=100.2 + index * 0.1,
            low=99.9 + index * 0.1,
            close=100.1 + index * 0.1,
            volume=1_000.0,
            close_time=now_ms - (100 - index) * 60_000,
            quote_volume=120_000.0 + index * 50.0,
            trade_count=100,
            taker_buy_base_volume=600.0,
            taker_buy_quote_volume=60_000.0,
        )
        for index in range(100)
    ]

    with patch("app.data_sources.binance_public.BinancePublicClient") as MockBinanceClient:
        mock_instance = MockBinanceClient.return_value
        mock_instance.klines = AsyncMock(side_effect=[candles, candles])
        mock_instance.order_book = AsyncMock(return_value={
            "bids": [[110.0, 500.0]], "asks": [[110.01, 500.0]],
        })

        response = client.post("/quant/verify-setup", json={"symbol": "BTCUSDT"})
        assert response.status_code == 200
        data = response.json()
        assert data["verdict"] in {"REVIEW_CANDIDATE", "WATCH_ONLY"}
        assert data["evaluation_mode"] == "deterministic_manual_review"
        assert "not a probability" in data["confidence_label"].lower()
        assert "liquidity" in data
        assert "spread_pct" in data["liquidity"]
        assert "depth_bids_1pct" in data["liquidity"]
        assert "token_health" not in data
