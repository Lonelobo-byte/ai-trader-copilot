"""Trend indicators — EMA-based trend detection, market regime, MTF alignment."""
from __future__ import annotations

from statistics import mean
from typing import Any

from app.data_sources.binance_public import Candle, completed_candles
from ._math import ema, pct


def analyze_trend(candles: list[Candle]) -> dict[str, Any]:
    closed = completed_candles(candles)
    if len(closed) < 50:
        return {"status": "unknown", "passed": False, "reason": "need_at_least_50_closed_candles"}

    closes = [c.close for c in closed]
    current = closes[-1]
    ema9 = ema(closes[-80:], 9)
    ema21 = ema(closes[-100:], 21)
    ema50 = ema(closes[-150:], 50)
    ema21_prev = ema(closes[-110:-10], 21) if len(closes) >= 110 else ema(closes[:-10], 21)
    slope_pct = pct(ema21 - ema21_prev, ema21_prev) if ema21_prev else 0.0

    if current > ema9 > ema21 > ema50 and slope_pct > 0:
        status = "bullish"
    elif current < ema9 < ema21 < ema50 and slope_pct < 0:
        status = "bearish"
    else:
        status = "sideways_or_mixed"

    return {
        "status": status,
        "passed": status in {"bullish", "bearish"},
        "ema9": round(ema9, 4),
        "ema21": round(ema21, 4),
        "ema50": round(ema50, 4),
        "ema21_slope_pct": round(slope_pct, 5),
        "last_close": round(current, 4),
    }


from app.indicators.quantitative import hurst_exponent, parkinson_volatility, return_skewness

def detect_market_regime(candles: list[Candle], atr: float) -> str:
    closed = completed_candles(candles)
    if not closed or len(closed) < 20:
        return "LOW_VOLATILITY"

    closes = [c.close for c in closed]
    close = closes[-1]
    
    # Calculate quant statistics
    h = hurst_exponent(closes)
    p_vol = parkinson_volatility(closed)
    skew_val = return_skewness(closes)
    
    recent_vols = [c.volume for c in closed[-20:]]
    avg_vol = mean(recent_vols) if recent_vols else 0.0
    current_vol = closed[-1].volume
    vol_ratio = current_vol / avg_vol if avg_vol > 0 else 1.0

    # 1. Panic & Euphoria (Extreme volatility + high skewness)
    if p_vol > 0.02:
        if skew_val < -1.2:
            return "PANIC"
        elif skew_val > 1.2:
            return "EUPHORIA"
        return "HIGH_VOLATILITY"

    # 2. Breakout & Expansion (Volume spike + volatility shift)
    if vol_ratio > 2.0:
        if p_vol > 0.015:
            return "BREAKOUT"
        return "EXPANSION"

    # 3. Compression & Low Volatility
    if p_vol < 0.005:
        if h < 0.5:
            return "COMPRESSION"
        return "LOW_VOLATILITY"

    # 4. Trending vs Mean-Reverting regimes (driven by Hurst Exponent)
    if h > 0.55:
        return "TRENDING"
    elif h < 0.45:
        return "MEAN_REVERTING"

    return "RANGING"


def calculate_alignment_score(selected_trend: str, local_trend: str, macro_trend: str) -> dict[str, float]:
    def get_score(direction: str, sel: str, loc: str, mac: str) -> float:
        # Baseline score depending on entry (selected) timeframe trend
        if sel == direction:
            score = 100.0
        elif sel == "sideways_or_mixed" or not sel:
            score = 50.0
        else:  # Opposing entry trend
            score = 10.0

        # Local timeframe trend penalty
        if loc == direction:
            pass
        elif loc == "sideways_or_mixed" or not loc:
            score -= 20.0
        else:  # Opposing local trend
            score -= 50.0

        # Macro timeframe trend penalty
        if mac == direction:
            pass
        elif mac == "sideways_or_mixed" or not mac:
            score -= 20.0
        else:  # Opposing macro trend
            score -= 50.0

        return max(0.0, min(100.0, score))

    return {
        "bullish_score": get_score("bullish", selected_trend, local_trend, macro_trend),
        "bearish_score": get_score("bearish", selected_trend, local_trend, macro_trend),
    }
