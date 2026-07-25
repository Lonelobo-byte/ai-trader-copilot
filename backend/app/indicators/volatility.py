"""Volatility indicators — ATR (Average True Range) and Bollinger Bands."""
from __future__ import annotations

from statistics import mean, stdev

from app.data_sources.binance_public import Candle, completed_candles


def atr(candles: list[Candle], period: int = 14) -> float:
    """Calculate Average True Range using Wilder's smoothing."""
    closed = completed_candles(candles)
    if len(closed) < period + 1:
        diffs = [abs(c.high - c.low) for c in closed]
        return mean(diffs) if diffs else 0.0

    tr_values = [closed[0].high - closed[0].low]
    for i in range(1, len(closed)):
        high = closed[i].high
        low = closed[i].low
        prev_close = closed[i - 1].close
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )
        tr_values.append(tr)

    result = mean(tr_values[:period])
    for tr in tr_values[period:]:
        result = (result * (period - 1) + tr) / period
    return result


def bollinger_bands(
    candles: list[Candle],
    period: int = 20,
    num_std: float = 2.0,
) -> dict[str, float]:
    """Calculate Bollinger Bands (basis, upper, lower, bandwidth, percent_b)."""
    closed = completed_candles(candles)
    if len(closed) < period:
        return {
            "basis": 0.0,
            "upper": 0.0,
            "lower": 0.0,
            "bandwidth": 0.0,
            "percent_b": 50.0,
        }

    closes = [c.close for c in closed[-period:]]
    basis = mean(closes)
    std = stdev(closes) if len(closes) > 1 else 0.0
    upper = basis + num_std * std
    lower = basis - num_std * std
    bandwidth = (upper - lower) / basis if basis > 0 else 0.0

    current_close = closed[-1].close
    width = upper - lower
    percent_b = ((current_close - lower) / width * 100.0) if width > 0 else 50.0

    return {
        "basis": round(basis, 4),
        "upper": round(upper, 4),
        "lower": round(lower, 4),
        "bandwidth": round(bandwidth, 4),
        "percent_b": round(percent_b, 2),
    }
