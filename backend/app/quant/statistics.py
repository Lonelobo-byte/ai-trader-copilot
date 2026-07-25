"""Statistical feature engineering for market observations."""
from __future__ import annotations

from math import log, sqrt
from typing import Any

import numpy as np

from app.indicators.quantitative import (
    hurst_exponent,
    parkinson_volatility,
    return_autocorrelation,
    rolling_z_score,
)


def _safe_float(value: float) -> float:
    return float(value) if np.isfinite(value) else 0.0


def _adf_style_t_stat(returns: np.ndarray) -> float | None:
    """Small OLS ADF-style diagnostic; a diagnostic, not a formal p-value."""
    if len(returns) < 25:
        return None
    prices = np.cumsum(returns)
    y = np.diff(prices)
    x = prices[:-1]
    design = np.column_stack([np.ones(len(x)), x])
    try:
        beta, _, _, _ = np.linalg.lstsq(design, y, rcond=None)
        residual = y - design @ beta
        variance = residual.dot(residual) / max(len(y) - 2, 1)
        covariance = variance * np.linalg.inv(design.T @ design)
        return _safe_float(beta[1] / sqrt(max(covariance[1, 1], 1e-12)))
    except np.linalg.LinAlgError:
        return None


def build_statistical_features(candles: list[Any]) -> dict[str, Any]:
    closes = np.asarray([float(c.close) for c in candles if float(c.close) > 0], dtype=float)
    if len(closes) < 12:
        return {"available": False, "reason": "At least 12 observations are required."}
    returns = np.diff(np.log(closes))
    window = returns[-min(40, len(returns)):]
    mean = float(np.mean(window))
    volatility = float(np.std(window, ddof=1)) if len(window) > 1 else 0.0
    price_window = closes[-min(20, len(closes)):]
    price_std = float(np.std(price_window, ddof=1)) if len(price_window) > 1 else 0.0
    z_score = (float(closes[-1]) - float(np.mean(price_window))) / price_std if price_std else 0.0
    ac1 = float(np.corrcoef(window[:-1], window[1:])[0, 1]) if len(window) > 3 and np.std(window[:-1]) and np.std(window[1:]) else 0.0
    centered = window - mean
    skew = float(np.mean(centered ** 3) / max(np.std(window) ** 3, 1e-12))
    kurtosis = float(np.mean(centered ** 4) / max(np.std(window) ** 4, 1e-12) - 3)
    hist, _ = np.histogram(window, bins=min(10, max(3, len(window) // 4)), density=False)
    probabilities = hist[hist > 0] / max(hist.sum(), 1)
    entropy = float(-(probabilities * np.log(probabilities)).sum() / log(len(hist))) if len(hist) > 1 else 0.0
    rolling_vol = [float(np.std(returns[max(0, i - 19):i + 1], ddof=1)) for i in range(19, len(returns))]
    vol_percentile = float(np.mean(np.asarray(rolling_vol) <= volatility)) if rolling_vol else 0.5
    short = float(np.mean(closes[-10:]))
    long = float(np.mean(closes[-min(50, len(closes)):]))
    trend_strength = (short / long - 1.0) / max(volatility * sqrt(10), 1e-9)
    volumes = np.asarray([float(c.volume) for c in candles if float(c.close) > 0], dtype=float)
    return {
        "available": True,
        "observations": int(len(returns)),
        "return_mean": round(mean, 8),
        "return_volatility": round(volatility, 8),
        "annualized_volatility_proxy": round(volatility * sqrt(365 * 24 * 4), 4),
        "volatility_percentile": round(vol_percentile, 4),
        "price_z_score": round(z_score, 4),
        "return_autocorrelation_lag1": round(_safe_float(ac1), 4),
        "distribution_skew": round(_safe_float(skew), 4),
        "excess_kurtosis": round(_safe_float(kurtosis), 4),
        "normalized_entropy": round(entropy, 4),
        "trend_strength_z": round(_safe_float(trend_strength), 4),
        # Shared fields for the ML probability adapter. Keeping these in the
        # statistical snapshot prevents silent zero-filled inference inputs.
        "hurst_exponent": round(_safe_float(hurst_exponent(closes)), 4),
        "parkinson_volatility": round(_safe_float(parkinson_volatility(candles)), 6),
        "return_autocorrelation_lag3": round(_safe_float(return_autocorrelation(closes, lag=3)), 4),
        "price_z_score_volume": round(_safe_float(rolling_z_score(volumes)), 4) if len(volumes) else 0.0,
        "adf_style_t_stat": None if _adf_style_t_stat(returns) is None else round(_adf_style_t_stat(returns), 4),
        "stationarity_note": "ADF-style diagnostic only; validate p-values and assumptions in the research pipeline.",
        "returns": returns.tolist(),
    }
