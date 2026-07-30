"""Timestamp-safe causal feature engineering for research models."""
from __future__ import annotations

from typing import Any

from app.data_sources.binance_public import Candle, completed_candles
from app.indicators.funding import analyze_funding_oi_divergence
from app.indicators.liquidity import analyze_order_book
from app.indicators.market_story import build_market_story
from app.indicators.quantitative import (
    hurst_exponent,
    parkinson_volatility,
    return_autocorrelation,
    return_kurtosis,
    return_skewness,
    rolling_z_score,
)
from app.indicators.structure import classify_market_phase
from app.quant.market_context import (
    build_liquidity_map,
    build_volume_profile,
    build_volatility_context,
    build_vwap_context,
)


FEATURE_CONTRACT = "bare_eye_causal_v1"


def build_ml_features(
    symbol: str,
    timeframe: str,
    candles: list[Candle],
    ticker: dict[str, Any],
    order_book: dict[str, Any],
    extra_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile only observable, timestamp-safe Bare Eye features.

    RSI, MACD, Bollinger and moving-average transforms are intentionally
    absent. Historical training rows use completed price/volume, reconstructed
    structure, liquidity references, price×OI context, and reported taker
    notional. Live order-book fields remain available for genuine timestamped
    inference but are excluded by the current walk-forward trainer.
    """
    closed = completed_candles(candles)
    closes = [c.close for c in closed]
    volumes = [c.volume for c in closed]

    # 1. Market Microstructure Features
    avg_vol = sum(volumes[-20:]) / 20.0 if len(volumes) >= 20 else 0.0
    book_metrics = analyze_order_book(order_book, avg_candle_volume=avg_vol)

    # 2. Statistical Features (Stationary)
    h_exp = hurst_exponent(closes)
    p_vol = parkinson_volatility(closed)
    skew = return_skewness(closes)
    kurt = return_kurtosis(closes)
    acf_1 = return_autocorrelation(closes, lag=1)
    acf_3 = return_autocorrelation(closes, lag=3)
    z_close = rolling_z_score(closes)
    z_volume = rolling_z_score(volumes)

    context = extra_context or {}
    funding_rate = float(context.get("funding_rate", 0.0))
    open_interest = float(context.get("open_interest", 0.0))
    funding_oi = analyze_funding_oi_divergence(funding_rate, open_interest)

    story = build_market_story(closed)
    event = story.get("latest_event") or {}
    phase = classify_market_phase(closed) if closed else "RANGING"
    average_range = (
        sum(max(float(c.high) - float(c.low), 0.0) for c in closed[-14:])
        / max(len(closed[-14:]), 1)
        if closed else 0.0
    )
    liquidity = build_liquidity_map(closed, average_range)
    volatility = build_volatility_context(closed)
    profile = build_volume_profile(closed)
    vwap = build_vwap_context(closed)
    last_close = float(closed[-1].close) if closed else 0.0
    recent = closed[-20:]
    gross_taker_notional = sum(float(c.quote_volume) for c in recent)
    buy_taker_notional = sum(float(c.taker_buy_quote_volume) for c in recent)
    aggressive_buy_ratio = (
        buy_taker_notional / gross_taker_notional
        if gross_taker_notional > 0 else 0.5
    )
    signed_taker_flow = aggressive_buy_ratio * 2.0 - 1.0
    reference_close = float(closed[-6].close) if len(closed) >= 6 else last_close
    price_change_5 = (
        (last_close / reference_close - 1.0)
        if reference_close > 0 else 0.0
    )
    phase_score = {
        "MARKUP": 1.0,
        "ACCUMULATION": 0.5,
        "RANGING": 0.0,
        "DISTRIBUTION": -0.5,
        "MARKDOWN": -1.0,
    }.get(phase, 0.0)
    event_direction = (
        1.0 if event.get("direction") == "BULLISH"
        else -1.0 if event.get("direction") == "BEARISH"
        else 0.0
    )
    profile_location = {
        "ABOVE_POC_ACCEPTANCE": 1.0,
        "BELOW_POC_ACCEPTANCE": -1.0,
    }.get(str(profile.get("location")), 0.0)
    vwap_relation = {
        "ABOVE_ALL": 1.0,
        "BELOW_ALL": -1.0,
    }.get(str(vwap.get("price_relation")), 0.0)
    nearest_above = (liquidity.get("nearest_above") or {}).get("price")
    nearest_below = (liquidity.get("nearest_below") or {}).get("price")

    features = {
        "symbol": symbol,
        "timeframe": timeframe,
        "feature_contract": FEATURE_CONTRACT,
        "micro_imbalance_5": book_metrics.get("imbalance_5", 0.0),
        "micro_imbalance_10": book_metrics.get("imbalance_10", 0.0),
        "micro_imbalance_20": book_metrics.get("imbalance_20", 0.0),
        "micro_spread_pct": book_metrics.get("spread_pct", 0.0),
        "micro_bid_density": book_metrics.get("bid_density_1pct", 0.0),
        "micro_ask_density": book_metrics.get("ask_density_1pct", 0.0),
        "micro_absorption_ratio": book_metrics.get("absorption_ratio", 0.0),
        "stat_hurst_exponent": round(h_exp, 4),
        "stat_parkinson_volatility": round(p_vol, 5),
        "stat_return_skewness": round(skew, 4),
        "stat_return_kurtosis": round(kurt, 4),
        "stat_autocorrelation_lag1": round(acf_1, 4),
        "stat_autocorrelation_lag3": round(acf_3, 4),
        "stat_z_score_close": round(z_close, 4),
        "stat_z_score_volume": round(z_volume, 4),
        "funding_rate": funding_rate,
        "funding_oi_strength": funding_oi.get("strength", 50.0),
        "positioning_oi_change_pct": float(context.get("oi_change_pct", 0.0)),
        "causal_phase_score": phase_score,
        "causal_event_direction": event_direction,
        "causal_event_actionable": 1.0 if event.get("actionable") else 0.0,
        "causal_event_age_bars": float(event.get("age_bars") or 0.0),
        "causal_event_relative_volume": float(event.get("relative_volume") or 0.0),
        "causal_event_decisive": 1.0 if event.get("decisive_candle") else 0.0,
        "causal_liquidity_above_pct": (
            (float(nearest_above) / last_close - 1.0)
            if nearest_above and last_close > 0 else 0.0
        ),
        "causal_liquidity_below_pct": (
            (last_close / float(nearest_below) - 1.0)
            if nearest_below and float(nearest_below) > 0 else 0.0
        ),
        "causal_realized_vol_ratio": float(
            volatility.get("short_to_long_ratio") or 1.0
        ),
        "causal_profile_location": profile_location,
        "causal_poc_distance_pct": (
            (last_close / float(profile.get("poc")) - 1.0)
            if profile.get("poc") and last_close > 0 else 0.0
        ),
        "causal_vwap_relation": vwap_relation,
        "causal_aggressive_buy_ratio": round(aggressive_buy_ratio, 6),
        "causal_signed_taker_flow": round(signed_taker_flow, 6),
        "causal_price_change_5": round(price_change_5, 6),
    }

    return features
