from app.data_sources.binance_public import Candle
from app.quant.market_context import (
    build_liquidity_map,
    build_volume_profile,
    build_volatility_context,
    build_vwap_context,
    classify_positioning,
    score_market_context,
)


def _candles() -> list[Candle]:
    rows = []
    for index in range(80):
        # Repeat local extremes so equal-high/equal-low clustering is testable.
        price = 100.0 + index * 0.15
        high = 105.0 if index in {20, 30} else price + 0.25
        low = 98.0 if index in {21, 31} else price - 0.25
        rows.append(Candle(
            open_time=index * 3_600_000,
            open=price - 0.05,
            high=high,
            low=low,
            close=price,
            volume=1_000.0 + index * 10,
            close_time=(index + 1) * 3_600_000 - 1,
            quote_volume=price * (1_000.0 + index * 10),
            trade_count=100,
            taker_buy_base_volume=600.0,
            taker_buy_quote_volume=price * 600.0,
        ))
    return rows


def test_positioning_distinguishes_new_longs_from_short_covering() -> None:
    candles = _candles()
    building = classify_positioning(candles, {
        "funding_rate": 0.0001,
        "oi_history": {"available": True, "oi_change_pct": 2.0},
        "taker_volume": {"cvd_trend": "CVD_BULLISH_ACCUMULATION"},
    })
    covering = classify_positioning(candles, {
        "funding_rate": -0.0001,
        "oi_history": {"available": True, "oi_change_pct": -2.0},
        "taker_volume": {"cvd_trend": "CVD_BULLISH_ACCUMULATION"},
    })
    assert building["state"] == "BUILDING_LONGS"
    assert covering["state"] == "SHORT_COVERING"


def test_context_score_ignores_derived_indicator_values() -> None:
    features = {
        "market_structure": {"phase": "MARKUP", "bos": {"detected": True, "direction": "bullish"}},
        "liquidity_map": {"available": True},
        "sweep": {"direction": "bullish_reversal_watch"},
        "positioning": {"available": True, "bias": "BULLISH", "crowding": "NEUTRAL", "delta_divergence": "NONE", "state": "BUILDING_LONGS"},
        "microstructure": {"available": True, "depth_imbalance": 0.30},
        "trade_flow": {"buy_ratio": 0.70},
        "volatility_context": {"available": True, "state": "EXPANSION"},
        "volume_profile": {"available": True, "location": "ABOVE_POC_ACCEPTANCE"},
        "vwap_context": {"available": True, "price_relation": "ABOVE_ALL"},
        "cross_asset": {"risk_environment": "RISK_ON"},
        "momentum": {"rsi": 1.0, "macd": {"histogram": -999.0}},
    }
    first = score_market_context(features)
    features["momentum"] = {"rsi": 99.0, "macd": {"histogram": 999.0}}
    second = score_market_context(features)
    assert first["direction"] == "LONG"
    assert first["status"] == "SETUP_CANDIDATE"
    assert first["score"] == second["score"]


def test_liquidity_profile_volatility_and_vwap_contexts_are_explicit() -> None:
    candles = _candles()
    liquidity = build_liquidity_map(candles, atr=0.8)
    profile = build_volume_profile(candles)
    volatility = build_volatility_context(candles)
    vwap = build_vwap_context(candles)
    assert liquidity["available"] is True
    assert liquidity["pools"]
    assert profile["available"] is True and profile["poc"] > 0
    assert volatility["available"] is True
    assert vwap["available"] is True and vwap["daily"] > 0
