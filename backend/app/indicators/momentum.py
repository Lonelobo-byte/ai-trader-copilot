"""Momentum indicators — RSI, MACD, Stochastic RSI, and composite scoring."""
from __future__ import annotations

from statistics import mean
from typing import Any

from app.data_sources.binance_public import Candle, completed_candles
from ._math import ema, pct, clamp


def _rsi(values: list[float], period: int = 14) -> float:
    if len(values) <= period:
        return 50.0

    gains: list[float] = []
    losses: list[float] = []
    for previous, current in zip(values, values[1:]):
        change = current - previous
        gains.append(max(change, 0.0))
        losses.append(abs(min(change, 0.0)))

    avg_gain = mean(gains[:period]) if gains[:period] else 0.0
    avg_loss = mean(losses[:period]) if losses[:period] else 0.0
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    relative_strength = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + relative_strength))


def macd(
    candles: list[Candle],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> dict[str, float]:
    """Calculate moving average convergence divergence (MACD)."""
    closed = completed_candles(candles)
    if len(closed) < slow_period + signal_period:
        return {"macd": 0.0, "signal": 0.0, "histogram": 0.0}

    closes = [c.close for c in closed]
    
    # Calculate rolling MACD line values
    macd_values = []
    for i in range(slow_period, len(closes) + 1):
        window = closes[:i]
        # Use last fast_period * 3 data points to stabilize EMA
        macd_line = ema(window[-fast_period * 3:], fast_period) - ema(window[-slow_period * 3:], slow_period)
        macd_values.append(macd_line)

    macd_current = macd_values[-1]
    signal_current = ema(macd_values, signal_period)
    histogram = macd_current - signal_current

    return {
        "macd": round(macd_current, 4),
        "signal": round(signal_current, 4),
        "histogram": round(histogram, 4),
    }


def stochastic_rsi(
    candles: list[Candle],
    period: int = 14,
    smooth_k: int = 3,
    smooth_d: int = 3,
) -> dict[str, float]:
    """Calculate Stochastic RSI."""
    closed = completed_candles(candles)
    min_needed = period + smooth_k + smooth_d + 5
    if len(closed) < min_needed:
        return {"k": 50.0, "d": 50.0}

    closes = [c.close for c in closed]
    
    # Calculate rolling RSI values
    rsi_values = []
    for i in range(period, len(closes) + 1):
        rsi_values.append(_rsi(closes[:i], period))

    # Calculate raw StochRSI values
    stoch_rsi_values = []
    for i in range(period, len(rsi_values) + 1):
        window = rsi_values[i - period:i]
        low_rsi = min(window)
        high_rsi = max(window)
        diff = high_rsi - low_rsi
        val = ((rsi_values[i - 1] - low_rsi) / diff * 100.0) if diff > 0 else 50.0
        stoch_rsi_values.append(val)

    # Smooth %K
    k_values = []
    for i in range(smooth_k, len(stoch_rsi_values) + 1):
        k_values.append(mean(stoch_rsi_values[i - smooth_k:i]))

    # Smooth %D
    d_values = []
    for i in range(smooth_d, len(k_values) + 1):
        d_values.append(mean(k_values[i - smooth_d:i]))

    return {
        "k": round(k_values[-1], 2) if k_values else 50.0,
        "d": round(d_values[-1], 2) if d_values else 50.0,
    }


def analyze_momentum(candles: list[Candle], lookback: int = 12) -> dict[str, Any]:
    closed = completed_candles(candles)
    if len(closed) < max(lookback + 2, 30):
        return {
            "bias": "neutral",
            "passed": False,
            "score": 50.0,
            "reason": "need_more_candles",
        }

    closes = [c.close for c in closed]
    price_change_pct = pct(closes[-1] - closes[-lookback], closes[-lookback]) if closes[-lookback] else 0.0
    rsi = _rsi(closes[-60:], 14)
    ema_fast = ema(closes[-40:], 8)
    ema_slow = ema(closes[-60:], 21)

    recent = closed[-6:]
    baseline = closed[-30:-6]
    recent_volume = mean([c.volume for c in recent]) if recent else 0.0
    baseline_volume = mean([c.volume for c in baseline]) if baseline else recent_volume
    volume_ratio = recent_volume / baseline_volume if baseline_volume > 0 else 1.0
    taker_buy_ratio = mean([c.taker_buy_ratio for c in recent]) if recent else 0.5

    # Run sub-indicators
    macd_data = macd(candles)
    stoch_rsi_data = stochastic_rsi(candles)

    score = 50.0
    score += clamp(price_change_pct * 8.0, -18.0, 18.0)
    score += clamp((rsi - 50.0) * 0.55, -18.0, 18.0)
    score += 9.0 if ema_fast > ema_slow else -9.0 if ema_fast < ema_slow else 0.0
    score += clamp((taker_buy_ratio - 0.5) * 80.0, -10.0, 10.0)
    if volume_ratio >= 1.15:
        score += 5.0
    elif volume_ratio < 0.75:
        score -= 5.0

    # Add minor adjustments from MACD and StochRSI
    # MACD Histogram contribution: if bullish histogram, add +3
    if macd_data["histogram"] > 0:
        score += 3.0
    elif macd_data["histogram"] < 0:
        score -= 3.0

    # StochRSI contribution: oversold (<20) cross up, or overbought (>80) cross down
    if stoch_rsi_data["k"] < 20.0 and stoch_rsi_data["k"] > stoch_rsi_data["d"]:
        score += 4.0
    elif stoch_rsi_data["k"] > 80.0 and stoch_rsi_data["k"] < stoch_rsi_data["d"]:
        score -= 4.0

    score = round(clamp(score, 0.0, 100.0), 2)
    if score >= 58.0:
        bias = "bullish"
    elif score <= 42.0:
        bias = "bearish"
    else:
        bias = "neutral"

    return {
        "bias": bias,
        "passed": bias != "neutral" and volume_ratio >= 0.7,
        "score": score,
        "rsi": round(rsi, 2),
        "price_change_pct": round(price_change_pct, 4),
        "volume_ratio": round(volume_ratio, 3),
        "taker_buy_ratio": round(taker_buy_ratio, 3),
        "ema_fast": round(ema_fast, 4),
        "ema_slow": round(ema_slow, 4),
        "macd": macd_data,
        "stoch_rsi": stoch_rsi_data,
    }
