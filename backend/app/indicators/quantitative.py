"""Quantitative and statistical feature engineering indicators.

Implements Hurst exponent estimation, Parkinson volatility, rolling stats,
Z-scores, return autocorrelation, skewness, and kurtosis using NumPy.
"""
from __future__ import annotations

import numpy as np

from app.data_sources.binance_public import Candle, completed_candles


def hurst_exponent(prices: list[float]) -> float:
    """Estimate the Hurst Exponent of a price series to classify regime type.

    H > 0.5 indicates trending behavior (persistent).
    H < 0.5 indicates mean-reverting behavior (anti-persistent).
    H ~ 0.5 indicates random walk.
    """
    if len(prices) < 20:
        return 0.5

    returns = np.diff(np.log(prices))
    if np.all(returns == 0) or np.std(returns) == 0:
        return 0.5

    lags = [5, 10, 15, 20]
    lags = [l for l in lags if l < len(returns)]
    if len(lags) < 2:
        return 0.5

    rs_values = []
    for lag in lags:
        num_windows = len(returns) // lag
        rs_tmp = []
        for w in range(num_windows):
            window = returns[w * lag : (w + 1) * lag]
            mean_val = np.mean(window)
            cum_dev = np.cumsum(window - mean_val)
            r = np.max(cum_dev) - np.min(cum_dev)
            s = np.std(window)
            if s > 0:
                rs_tmp.append(r / s)
        if rs_tmp:
            rs_values.append(np.mean(rs_tmp))
        else:
            rs_values.append(1.0)

    try:
        valid_idx = [i for i, val in enumerate(rs_values) if val > 0]
        if len(valid_idx) < 2:
            return 0.5
        x = np.log([lags[i] for i in valid_idx])
        y = np.log([rs_values[i] for i in valid_idx])
        poly = np.polyfit(x, y, 1)
        return float(poly[0])
    except Exception:
        return 0.5


def parkinson_volatility(candles: list[Candle], period: int = 14) -> float:
    """Calculate Parkinson high-low range volatility estimator.

    Provides a more sample-efficient estimate than standard deviation.
    """
    closed = completed_candles(candles)
    if len(closed) < period:
        return 0.0

    sum_sq = 0.0
    count = 0
    for c in closed[-period:]:
        if c.low > 0:
            val = np.log(c.high / c.low)
            sum_sq += val**2
            count += 1

    if count == 0:
        return 0.0

    factor = 4.0 * np.log(2.0)
    variance = sum_sq / (factor * count)
    return float(np.sqrt(variance))


def rolling_z_score(values: list[float], period: int = 20) -> float:
    """Calculate rolling Z-score of the last value relative to history."""
    if len(values) < period:
        return 0.0
    window = values[-period:]
    mean_val = np.mean(window)
    std_val = np.std(window)
    if std_val == 0:
        return 0.0
    return float((values[-1] - mean_val) / std_val)


def return_autocorrelation(prices: list[float], lag: int = 1, period: int = 20) -> float:
    """Calculate return autocorrelation at a given lag."""
    if len(prices) < period + lag + 1:
        return 0.0

    returns = np.diff(np.log(prices))
    window = returns[-period:]
    if len(window) < lag + 2:
        return 0.0

    x = window[:-lag]
    y = window[lag:]
    std_x = np.std(x)
    std_y = np.std(y)

    if std_x == 0 or std_y == 0:
        return 0.0

    mean_x = np.mean(x)
    mean_y = np.mean(y)
    cov = np.mean((x - mean_x) * (y - mean_y))
    return float(cov / (std_x * std_y))


def return_skewness(prices: list[float], period: int = 20) -> float:
    """Calculate rolling return distribution skewness (3rd moment)."""
    if len(prices) < period + 1:
        return 0.0

    returns = np.diff(np.log(prices))
    window = returns[-period:]
    mean_val = np.mean(window)
    std_val = np.std(window)

    if std_val == 0:
        return 0.0

    m3 = np.mean((window - mean_val) ** 3)
    return float(m3 / (std_val**3))


def return_kurtosis(prices: list[float], period: int = 20) -> float:
    """Calculate rolling return distribution excess kurtosis (4th moment)."""
    if len(prices) < period + 1:
        return 0.0

    returns = np.diff(np.log(prices))
    window = returns[-period:]
    mean_val = np.mean(window)
    std_val = np.std(window)

    if std_val == 0:
        return 0.0

    m4 = np.mean((window - mean_val) ** 4)
    return float(m4 / (std_val**4) - 3.0)
