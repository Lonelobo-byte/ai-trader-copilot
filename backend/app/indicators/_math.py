"""Shared math helpers used across indicator modules."""
from __future__ import annotations

from statistics import mean


def ema(values: list[float], period: int) -> float:
    """Exponential Moving Average."""
    if not values:
        return 0.0
    if len(values) < period:
        return mean(values)
    multiplier = 2 / (period + 1)
    result = mean(values[:period])
    for value in values[period:]:
        result = (value - result) * multiplier + result
    return result


def pct(part: float, whole: float) -> float:
    """Percentage of part relative to whole."""
    if whole == 0:
        return 0.0
    return (part / whole) * 100


def clamp(value: float, low: float, high: float) -> float:
    """Clamp value between low and high."""
    return max(low, min(high, value))
