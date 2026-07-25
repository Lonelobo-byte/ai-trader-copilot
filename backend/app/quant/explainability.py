"""Human-readable evidence ledger for a quantitative forecast."""
from __future__ import annotations

from typing import Any


def explain_forecast(forecast: dict[str, Any], stats: dict[str, Any], micro: dict[str, Any], regime: dict[str, Any]) -> dict[str, Any]:
    weights = forecast.get("feature_weights", {})
    values = {
        "signed_trade_flow": float(micro.get("signed_trade_flow", 0.0)),
        "depth_imbalance": float(micro.get("depth_imbalance", 0.0)),
        "trend_strength_z": float(stats.get("trend_strength_z", 0.0)),
        "return_autocorrelation_lag1": float(stats.get("return_autocorrelation_lag1", 0.0)),
        "mean_reversion_z_score": float(stats.get("price_z_score", 0.0)),
    }
    factors = sorted(
        [{"factor": name, "contribution": round(float(weight) * values.get(name, 0.0), 4), "value": round(values.get(name, 0.0), 4)} for name, weight in weights.items()],
        key=lambda item: abs(item["contribution"]), reverse=True,
    )
    risks = []
    if float(stats.get("excess_kurtosis", 0.0)) > 1:
        risks.append("Fat-tailed return distribution increases tail-risk uncertainty.")
    if float(micro.get("spread_bps", 0.0)) > 15:
        risks.append("Wide spread reduces executable edge.")
    if regime.get("primary") in {"panic", "euphoria", "high_volatility"}:
        risks.append("Current regime is unstable; historical relationships may decay.")
    if not risks:
        risks.append("Forecast remains conditional on calibration and out-of-sample validation.")
    return {
        "top_factors": factors[:5],
        "confidence_score": forecast.get("confidence", 0.0),
        "market_regime": regime.get("primary", "unknown"),
        "risk_factors": risks,
        "statistical_justification": "Forecast combines trade-flow/depth imbalance with return-distribution, autocorrelation, volatility and regime features; estimates include a confidence interval rather than a command.",
    }
