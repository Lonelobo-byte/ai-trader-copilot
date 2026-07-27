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

    Existing ML artifacts are deliberately excluded until they are retrained
    on timestamped versions of this causal feature contract.  Those artifacts
    include RSI/MACD/Bollinger fields and must not influence live decisions.
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
    legacy_model = None
    # Compatibility for callers explicitly using the former standalone
    # research API. The production engine always passes a context object, so
    # historical indicator-trained artifacts cannot enter live scoring.
    if market_context is None:
        from app.ml.model import predict_probability_from_model
        legacy_model = predict_probability_from_model(
            {
                "stat_hurst_exponent": stats.get("hurst_exponent", 0.5),
                "stat_parkinson_volatility": stats.get("parkinson_volatility", stats.get("return_volatility", 0.0)),
                "stat_return_skewness": stats.get("distribution_skew", 0.0),
                "stat_return_kurtosis": stats.get("excess_kurtosis", 0.0),
                "stat_autocorrelation_lag1": stats.get("return_autocorrelation_lag1", 0.0),
                "stat_z_score_close": stats.get("price_z_score", 0.0),
                "micro_imbalance_10": micro.get("depth_imbalance", 0.0),
            }, symbol=symbol, timeframe=timeframe,
        )
        if legacy_model:
            probability_up = float(legacy_model["probability_up"])
            probability_down = float(legacy_model["probability_down"])

    sigma = float(stats["return_volatility"])
    expected_return = float(stats["return_mean"]) + (probability_up - probability_down) * sigma * 0.30
    tail_multiplier = 1.65 if float(stats["excess_kurtosis"]) > 1 else 1.28
    ci = [expected_return - tail_multiplier * sigma, expected_return + tail_multiplier * sigma]
    expected_risk = sigma * (1.0 + float(regime.get("probabilities", {}).get("high_volatility", 0.0)))
    expected_value = probability_up * max(expected_return, 0.0) - probability_down * expected_risk * 0.5
    sample_n = int(stats["observations"])
    calibration_error = sqrt(max(probability_up * probability_down, 0.0) / max(sample_n, 1))

    return {
        "model": legacy_model["model"] if legacy_model else "causal_market_context_baseline",
        "model_status": legacy_model["model_status"] if legacy_model else "research baseline — retrain and calibrate a registered model on timestamped causal features before capital allocation",
        "horizon": "next observation",
        "probability_up": round(probability_up, 4),
        "probability_down": round(probability_down, 4),
        "confidence": round(
            max(0.0, min(1.0, float(legacy_model.get("test_ic", 0.0)) * 4.0)) if legacy_model
            else 0.20 if market_context is None
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
