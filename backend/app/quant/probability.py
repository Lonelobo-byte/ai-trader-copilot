"""Model-agnostic probabilistic forecast interface and baseline model."""
from __future__ import annotations

from math import exp, sqrt
from typing import Any

import numpy as np


def _sigmoid(value: float) -> float:
    value = max(min(value, 30.0), -30.0)
    return 1.0 / (1.0 + exp(-value))


def forecast_distribution(
    stats: dict[str, Any],
    micro: dict[str, Any],
    regime: dict[str, Any],
    symbol: str = "",
    timeframe: str = "",
) -> dict[str, Any]:
    """Return a calibrated-contract forecast using trained ML model if available."""
    if not stats.get("available"):
        return {"model": "unavailable", "probability_up": 0.5, "probability_down": 0.5, "confidence_interval": [0.0, 0.0]}

    # Keep this value available for both the trained-model and baseline paths.
    # The response exposes its weight regardless of which model produced the
    # probability estimate.
    mean_reversion_mass = float(regime.get("probabilities", {}).get("mean_reverting", 0.0))

    # Attempt to predict using a compatible walk-forward trained ML model
    from app.ml.model import predict_probability_from_model
    # Map statistics & microstructure to ml_features names
    feat = {
        "micro_imbalance_5": micro.get("near_touch_imbalance", 0.0),
        "micro_imbalance_10": micro.get("depth_imbalance", 0.0),
        "micro_imbalance_20": micro.get("depth_imbalance", 0.0),
        "micro_spread_pct": float(micro.get("spread_bps", 0.0)) / 100.0,
        "micro_bid_density": micro.get("bid_depth_notional", 0.0),
        "micro_ask_density": micro.get("ask_depth_notional", 0.0),
        "micro_absorption_ratio": micro.get("absorption_proxy", 0.0),
        "stat_hurst_exponent": stats.get("hurst_exponent", 0.5),
        "stat_parkinson_volatility": stats.get("parkinson_volatility", stats.get("return_volatility", 0.0)),
        "stat_return_skewness": stats.get("distribution_skew", 0.0),
        "stat_return_kurtosis": stats.get("excess_kurtosis", 0.0),
        "stat_autocorrelation_lag1": stats.get("return_autocorrelation_lag1", 0.0),
        "stat_autocorrelation_lag3": stats.get("return_autocorrelation_lag3", 0.0),
        "stat_z_score_close": stats.get("price_z_score", 0.0),
        "stat_z_score_volume": stats.get("price_z_score_volume", 0.0),
        "funding_rate": stats.get("funding_rate", 0.0),
        "open_interest": stats.get("open_interest", 0.0),
        "funding_oi_strength": stats.get("funding_oi_strength", 50.0),
        "derived_rsi": stats.get("rsi", 50.0),
        "derived_macd_histogram": stats.get("macd_hist", 0.0),
        "derived_bb_bandwidth": stats.get("bb_bandwidth", 0.0),
        "derived_bb_percent_b": stats.get("bb_percent_b", 50.0),
    }
    
    ml_pred = predict_probability_from_model(feat, symbol=symbol, timeframe=timeframe)
    if ml_pred:
        probability_up = ml_pred["probability_up"]
        probability_down = ml_pred["probability_down"]
        model_name = ml_pred["model"]
        model_status = ml_pred["model_status"]
    else:
        flow = float(micro.get("signed_trade_flow", 0.0))
        imbalance = float(micro.get("depth_imbalance", 0.0))
        trend = float(stats["trend_strength_z"])
        autocorr = float(stats["return_autocorrelation_lag1"])
        z_score = float(stats["price_z_score"])
        # Mean-reversion features only dominate when that state has meaningful mass.
        mean_reversion_mass = float(regime.get("probabilities", {}).get("mean_reverting", 0.0))
        raw_score = 0.70 * flow + 0.45 * imbalance + 0.20 * trend + 0.25 * autocorr - 0.18 * z_score * mean_reversion_mass
        probability_up = _sigmoid(raw_score)
        probability_down = 1.0 - probability_up
        model_name = "baseline_interpretable_probability_model"
        model_status = "research baseline — train and calibrate a registered model before capital allocation"

    sigma = float(stats["return_volatility"])
    expected_return = float(stats["return_mean"]) + (probability_up - probability_down) * sigma * 0.30
    tail_multiplier = 1.65 if float(stats["excess_kurtosis"]) > 1 else 1.28
    ci = [expected_return - tail_multiplier * sigma, expected_return + tail_multiplier * sigma]
    expected_risk = sigma * (1.0 + float(regime.get("probabilities", {}).get("high_volatility", 0.0)))
    expected_value = probability_up * max(expected_return, 0.0) - probability_down * expected_risk * 0.5
    sample_n = int(stats["observations"])
    calibration_error = sqrt(max(probability_up * probability_down, 0.0) / max(sample_n, 1))

    # Calibrate forecast confidence based on demonstrated out-of-sample edge
    if ml_pred:
        # Use ML model's demonstrated out-of-sample edge (test_ic)
        # Bounded between 0.0 and 1.0, adjusted by regime confidence penalty.
        # An IC of 0.25+ corresponds to 1.0 confidence before regime penalties.
        test_ic = ml_pred.get("test_ic", 0.0)
        edge_confidence = test_ic * 4.0
        confidence = max(0.0, min(1.0, edge_confidence - float(regime.get("confidence", 0.0)) * 0.10))
    else:
        # Research baseline model has no demonstrated out-of-sample edge.
        # Cap confidence at 0.20 and adjust by regime confidence.
        confidence = max(0.0, min(0.20, 0.20 - float(regime.get("confidence", 0.0)) * 0.05))

    return {
        "model": model_name,
        "model_status": model_status,
        "horizon": "next observation",
        "probability_up": round(probability_up, 4),
        "probability_down": round(probability_down, 4),
        "confidence": round(confidence, 4),
        "confidence_interval": [round(ci[0], 6), round(ci[1], 6)],
        "expected_return": round(expected_return, 6),
        "expected_risk": round(expected_risk, 6),
        "expected_value": round(expected_value, 6),
        "calibration_standard_error": round(calibration_error, 4),
        "feature_weights": {
            "signed_trade_flow": 0.70,
            "depth_imbalance": 0.45,
            "trend_strength_z": 0.20,
            "return_autocorrelation_lag1": 0.25,
            "mean_reversion_z_score": -0.18 * mean_reversion_mass,
        },
        "supported_model_families": ["gradient_boosting", "xgboost", "lightgbm", "random_forest", "lstm", "transformer_time_series", "reinforcement_learning_future"],
    }
