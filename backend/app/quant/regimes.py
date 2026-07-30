"""Probabilistic market-state classifier."""
from __future__ import annotations

from typing import Any


REGIMES = ("trending", "mean_reverting", "high_volatility", "low_volatility", "breakout", "compression", "expansion", "panic", "euphoria")


def classify_market_state(stats: dict[str, Any], micro: dict[str, Any]) -> dict[str, Any]:
    if not stats.get("available"):
        return {"primary": "unknown", "probabilities": {}, "confidence": 0.0}
    vol = float(stats["volatility_percentile"])
    z = float(stats["price_z_score"])
    trend = abs(float(stats["trend_strength_z"]))
    autocorr = float(stats["return_autocorrelation_lag1"])
    entropy = float(stats["normalized_entropy"])
    actual_flow = (
        ((micro.get("execution_tape") or {}).get("actual_flow") or {})
        if isinstance(micro.get("execution_tape"), dict)
        else {}
    )
    if actual_flow.get("available"):
        signed_flow = float(actual_flow.get("signed_flow") or 0.0)
        flow_source = "live_execution_tape"
        flow_status = str(actual_flow.get("status", "UNAVAILABLE"))
    else:
        signed_flow = float(micro.get("signed_trade_flow", 0.0))
        flow_source = "completed_candle_taker_fallback"
        flow_status = "HISTORICAL_FALLBACK"
    flow = abs(signed_flow)
    directional_flow = (
        signed_flow
        if flow_status in {"BUYING_CONFIRMED", "SELLING_CONFIRMED", "HISTORICAL_FALLBACK"}
        else 0.0
    )
    scores = {
        "trending": trend * 0.8 + max(autocorr, 0.0),
        "mean_reverting": abs(z) * 0.35 + max(-autocorr, 0.0),
        "high_volatility": vol * 2.0 + max(float(stats["excess_kurtosis"]), 0.0) * 0.08,
        "low_volatility": (1 - vol) * 1.5,
        "breakout": trend * 0.45 + vol + flow,
        "compression": (1 - vol) * 1.2 + (1 - entropy) * 0.5,
        "expansion": vol + flow + trend * 0.2,
        "panic": max(-z - 1.0, 0.0) * 0.7 + vol + max(-directional_flow, 0.0),
        "euphoria": max(z - 1.0, 0.0) * 0.7 + vol + max(directional_flow, 0.0),
    }
    total = sum(max(score, 0.001) for score in scores.values())
    probabilities = {name: round(max(score, 0.001) / total, 4) for name, score in scores.items()}
    primary = max(probabilities, key=probabilities.get)
    return {
        "primary": primary,
        "probabilities": probabilities,
        "confidence": round(probabilities[primary], 4),
        "flow_evidence": {
            "source": flow_source,
            "status": flow_status,
            "signed_flow": round(signed_flow, 4),
            "directional_flow": round(directional_flow, 4),
        },
        "method": "Interpretable, heuristic state classifier. Replace with a validated HMM/change-point model before production capital allocation.",
    }
