from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from app.data_sources.gdelt import get_news_query
from app.data_sources.binance_public import Candle
from app.main import app


def test_get_news_query() -> None:
    assert get_news_query("BTCUSDT") == '("BTC" OR "Bitcoin")'
    assert get_news_query("ETHUSDT") == '("ETH" OR "Ethereum")'
    assert get_news_query("SOLUSDT") == '("SOL" OR "Solana")'
    assert get_news_query("ADAUSDT") == '("ADA" OR "Cardano")'
    assert get_news_query("DOGEUSDT") == '("DOGE" OR "Dogecoin")'
    assert get_news_query("XYZUSDT") == '("XYZ")'
    assert get_news_query("BTC") == '("BTC" OR "Bitcoin")'


def test_health_endpoint() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["version"] == "0.6.0"
    assert data["mode"] == "institutional_research_only"


def test_ai_status_endpoint() -> None:
    client = TestClient(app)
    response = client.get("/ai/status")
    assert response.status_code == 200
    data = response.json()
    assert "provider" in data
    assert "configured" in data


def test_kelly_sizing() -> None:
    from app.risk.sizing import calculate_kelly_sizing
    # Test positive expectation Kelly
    # p = 0.8 (80%), b = 2.0 (Reward/Risk)
    # Raw half-Kelly is 35%, but production sizing is capped at 2% risk.
    res = calculate_kelly_sizing(1000.0, 80.0, 2.0, 10.0)
    assert res["kelly_pct"] == 2.0
    assert res["risk_amount_usd"] == 20.0
    assert res["units"] == 2.0
    assert res["status"] == "active"

    # Test negative expectation Kelly
    # p = 0.3 (30%), b = 2.0
    # f* = 0.5 * (0.3 - 0.7 / 2.0) = 0.5 * (0.3 - 0.35) = -0.025 (< 0)
    res_neg = calculate_kelly_sizing(1000.0, 30.0, 2.0, 10.0)
    assert res_neg["kelly_pct"] == 0.0
    assert res_neg["risk_amount_usd"] == 0.0
    assert res_neg["units"] == 0.0
    assert res_neg["status"] == "negative"


def test_market_regime_and_atr() -> None:
    from app.brains.tree_analysis import _atr, detect_market_regime
    # Create dummy candles (20 candles to verify)
    candles = [
        Candle(
            open_time=i * 60000,
            open=100.0,
            high=102.0,
            low=98.0,
            close=100.0 + (i % 2),
            volume=1000.0,
            close_time=(i * 60000) + 59999,
            quote_volume=100000.0,
            trade_count=100,
            taker_buy_base_volume=500.0,
            taker_buy_quote_volume=50000.0
        ) for i in range(50)
    ]
    atr_val = _atr(candles, period=14)
    assert atr_val > 0.0

    # Volatile regime: high ATR pct
    regime_volatile = detect_market_regime(candles, atr=5.0)  # 5.0 / 101.0 is ~4.9% (> 2.5%)
    assert regime_volatile == "HIGH_VOLATILITY"

    # Expansion regime: high volume ratio
    # Recreate candles with small high/low range to avoid high Parkinson Volatility pre-emption
    low_vol_candles = [
        Candle(
            open_time=i * 60000,
            open=100.0,
            high=100.1,
            low=99.9,
            close=100.0 + (i % 2) * 0.05,
            volume=1000.0,
            close_time=(i * 60000) + 59999,
            quote_volume=100000.0,
            trade_count=100,
            taker_buy_base_volume=500.0,
            taker_buy_quote_volume=50000.0
        ) for i in range(50)
    ]
    low_vol_candles[-1] = replace(low_vol_candles[-1], volume=10000.0)
    regime_expansion = detect_market_regime(low_vol_candles, atr=0.1)
    assert regime_expansion == "EXPANSION"


def test_alignment_score() -> None:
    from app.brains.tree_analysis import calculate_alignment_score
    # Confluent bullish
    score_bull = calculate_alignment_score("bullish", "bullish", "bullish")
    assert score_bull["bullish_score"] == 100.0
    assert score_bull["bearish_score"] == 0.0

    # Mixed trend
    score_mixed = calculate_alignment_score("bullish", "bearish", "sideways_or_mixed")
    # bullish_score = 100 - 50 (local opposing) - 20 (macro sideways) = 30
    # bearish_score = 10 (selected opposing) - 20 (macro sideways) = 0
    assert score_mixed["bullish_score"] == 30.0
    assert score_mixed["bearish_score"] == 0.0


def test_funding_oi_divergence() -> None:
    from app.brains.tree_analysis import analyze_funding_oi_divergence
    # Neutral
    res_neutral = analyze_funding_oi_divergence(0.0001, 100000.0)
    assert res_neutral["signal"] == "NEUTRAL"

    # Potential Short Squeeze
    res_short = analyze_funding_oi_divergence(-0.0002, 100000.0)
    assert res_short["signal"] == "POTENTIAL_SHORT_SQUEEZE"

    # Potential Long Squeeze
    res_long = analyze_funding_oi_divergence(0.0004, 100000.0)
    assert res_long["signal"] == "POTENTIAL_LONG_SQUEEZE"


def test_liquidation_heatmap() -> None:
    from app.brains.tree_analysis import calculate_liquidation_heatmap
    res = calculate_liquidation_heatmap(
        current_price=100.0,
        previous_high=105.0,
        previous_low=95.0,
        atr=2.0,
        open_interest=50000.0
    )
    assert "nearest_short_magnet" in res
    assert "nearest_long_magnet" in res
    assert "short_distance_pct" in res
    assert "long_distance_pct" in res
    assert "short_magnet_strength" in res
    assert "long_magnet_strength" in res
    assert "estimated_short_liquidity" in res
    assert "estimated_long_liquidity" in res

    # Nearest short magnet price should be current_price + 1.0 * atr = 100.0 + 2.0 = 102.0
    assert res["nearest_short_magnet"] == 102.0
    # Nearest long magnet price should be current_price - 1.0 * atr = 100.0 - 2.0 = 98.0
    assert res["nearest_long_magnet"] == 98.0
    # Volume: 50000 * 0.20 (weight for 1x ATR) = 10000.0
    assert res["estimated_short_liquidity"] == 10000.0
    assert res["estimated_long_liquidity"] == 10000.0


def test_confidence_engine() -> None:
    from app.brains.tree_analysis import calculate_confidence_engine
    # Base weights confidence calculation
    # base weights: mtf=0.25, funding_oi=0.20, liquidation=0.15, orderflow=0.10, rag=0.10, premortem=0.05, quant=0.15
    # All scores 100.0 -> Confidence 100.0 -> A+
    res_max = calculate_confidence_engine(100.0, 100.0, 100.0, 100.0, 100.0, 100.0, quant_score=100.0)
    assert res_max["confidence"] == 100
    assert res_max["trade_grade"] == "A+"

    # Mixed scores with dynamic reliability weights
    rel_weights = {
        "mtf": 0.9,
        "funding_oi": 0.8,
        "liquidation": 0.7,
        "orderflow": 0.6,
        "rag": 0.5,
        "premortem": 0.4,
        "quant": 0.7,
    }
    res_dyn = calculate_confidence_engine(
        mtf_score=80.0,
        funding_oi_score=70.0,
        liq_score=60.0,
        order_flow_score=90.0,
        rag_score=50.0,
        premortem_score=40.0,
        reliability_weights=rel_weights,
        quant_score=75.0,
    )
    assert 0 <= res_dyn["confidence"] <= 100
    assert "trade_grade" in res_dyn
    assert "quant" in res_dyn["weights_used"]




def _trend_candles(direction: str = "up") -> list[Candle]:
    candles = []
    for i in range(90):
        base = 100.0 + (i * 0.45 if direction == "up" else -i * 0.45)
        candles.append(
            Candle(
                open_time=i * 60000,
                open=base - 0.2 if direction == "up" else base + 0.2,
                high=base + 0.2,
                low=base - 0.2,
                close=base,
                volume=1000.0 + i,
                close_time=(i * 60000) + 59999,
                quote_volume=100000.0,
                trade_count=100,
                taker_buy_base_volume=650.0 if direction == "up" else 350.0,
                taker_buy_quote_volume=65000.0 if direction == "up" else 35000.0,
            )
        )
    return candles


def test_trend_continuation_can_build_signal_without_sweep() -> None:
    from app.brains.tree_analysis import (
        analyze_momentum,
        analyze_order_book,
        analyze_trend,
        build_risk_idea,
        decide_report,
        detect_liquidity_sweep,
    )

    candles = _trend_candles("up")
    trend = analyze_trend(candles)
    momentum = analyze_momentum(candles)
    order_book_raw = {
        "bids": [[138.5, 30.0], [137.8, 12.0], [136.0, 8.0]],
        "asks": [[141.0, 8.0], [142.0, 6.0], [143.0, 5.0]],
    }
    order_book = analyze_order_book(order_book_raw)
    sweep = detect_liquidity_sweep(candles)

    risk_idea = build_risk_idea(
        candles,
        sweep,
        min_rr=1.5,
        order_book=order_book_raw,
        trend=trend,
        order_book_pressure=order_book,
        momentum=momentum,
    )

    assert sweep["detected"] is False
    assert trend["status"] == "bullish"
    assert momentum["bias"] == "bullish"
    assert risk_idea is not None
    assert risk_idea["side"] == "long_watch"
    assert risk_idea["setup_type"] == "trend_continuation"

    decision = decide_report(
        data_freshness={"passed": True},
        liquidity={"passed": True},
        trend=trend,
        order_book=order_book,
        sweep=sweep,
        risk_idea=risk_idea,
        confidence_engine={"confidence": 72, "trade_grade": "B"},
    )
    assert decision["decision"] == "BUY_WATCH"


def test_standard_indicators() -> None:
    from app.indicators import macd, stochastic_rsi, bollinger_bands
    
    candles = _trend_candles("up")
    
    # Verify MACD
    macd_res = macd(candles)
    assert "macd" in macd_res
    assert "signal" in macd_res
    assert "histogram" in macd_res
    
    # Verify Stochastic RSI
    stoch_res = stochastic_rsi(candles)
    assert "k" in stoch_res
    assert "d" in stoch_res
    assert 0 <= stoch_res["k"] <= 100
    assert 0 <= stoch_res["d"] <= 100
    
    # Verify Bollinger Bands
    bb_res = bollinger_bands(candles)
    assert "basis" in bb_res
    assert "upper" in bb_res
    assert "lower" in bb_res
    assert "bandwidth" in bb_res
    assert "percent_b" in bb_res
    assert bb_res["upper"] > bb_res["lower"]


def test_smc_market_structure() -> None:
    from app.indicators import detect_bos, detect_choch, find_order_blocks, find_fair_value_gaps
    from app.indicators.structure import find_swing_points
    
    candles = _trend_candles("up")
    
    # Verify Swing Points
    highs, lows = find_swing_points(candles)
    assert isinstance(highs, list)
    assert isinstance(lows, list)
    
    # Verify BOS
    bos_res = detect_bos(candles)
    assert "detected" in bos_res
    assert "direction" in bos_res
    
    # Verify CHoCH
    choch_res = detect_choch(candles)
    assert "detected" in choch_res
    assert "direction" in choch_res
    
    # Verify FVG
    fvg_res = find_fair_value_gaps(candles)
    assert isinstance(fvg_res, list)
    
    # Verify Order Blocks
    ob_res = find_order_blocks(candles)
    assert isinstance(ob_res, list)


