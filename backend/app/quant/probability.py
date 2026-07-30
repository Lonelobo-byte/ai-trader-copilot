"""Probability contract driven by causal market-context evidence."""
from __future__ import annotations

from math import exp, sqrt
from typing import Any


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + exp(-max(min(value, 30.0), -30.0)))


def forecast_distribution(
    stats: dict[str, Any],
    micro: dict[str, Any],
    regime: dict[str, Any],
    symbol: str = "",
    timeframe: str = "",
    market_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Estimate a distribution without allowing derived indicators to vote.

    Research ML artifacts are deliberately excluded from live scoring. Legacy
    artifacts used RSI/MACD/Bollinger fields, while new artifacts carry the
    Bare Eye contract but still require explicit out-of-sample registration
    before they may influence capital decisions.
    """
    if not stats.get("available"):
        return {"model": "unavailable", "probability_up": 0.5, "probability_down": 0.5, "confidence_interval": [0.0, 0.0]}

    context = market_context or {}
    coverage = context.get("coverage", {}) or {}
    required = max(float(coverage.get("required_domains", 4)), 1.0)
    coverage_ratio = min(float(coverage.get("available_domains", 0)) / required, 1.0)
    normalized = float(context.get("normalized_directional_score", 0.0))
    probability_up = _sigmoid(normalized * coverage_ratio * 2.0)
    probability_down = 1.0 - probability_up
    sigma = float(stats["return_volatility"])
    expected_return = float(stats["return_mean"]) + (probability_up - probability_down) * sigma * 0.30
    tail_multiplier = 1.65 if float(stats["excess_kurtosis"]) > 1 else 1.28
    ci = [expected_return - tail_multiplier * sigma, expected_return + tail_multiplier * sigma]
    expected_risk = sigma * (1.0 + float(regime.get("probabilities", {}).get("high_volatility", 0.0)))
    expected_value = probability_up * max(expected_return, 0.0) - probability_down * expected_risk * 0.5
    sample_n = int(stats["observations"])
    calibration_error = sqrt(max(probability_up * probability_down, 0.0) / max(sample_n, 1))

    return {
        "model": "causal_market_context_baseline",
        "model_status": "research baseline — train and calibrate a registered Bare Eye causal model on timestamped evidence before capital allocation",
        "horizon": "next observation",
        "probability_up": round(probability_up, 4),
        "probability_down": round(probability_down, 4),
        "confidence": round(
            0.20 if market_context is None
            else max(0.0, min(0.20, 0.08 + coverage_ratio * 0.12)),
            4,
        ),
        "confidence_interval": [round(ci[0], 6), round(ci[1], 6)],
        "expected_return": round(expected_return, 6),
        "expected_risk": round(expected_risk, 6),
        "expected_value": round(expected_value, 6),
        "calibration_standard_error": round(calibration_error, 4),
        "feature_weights": {name: item.get("weight", 0.0) for name, item in (context.get("components") or {}).items()},
        "causal_context": context,
        "supported_model_families": ["gradient_boosting", "xgboost", "lightgbm", "random_forest", "transformer_time_series"],
    }
