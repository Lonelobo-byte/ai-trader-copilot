"""Human-readable evidence ledger for a quantitative forecast."""
from __future__ import annotations

from typing import Any


def explain_forecast(
    forecast: dict[str, Any], stats: dict[str, Any], micro: dict[str, Any], regime: dict[str, Any],
    market_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context = market_context or {}
    components = context.get("components", {}) or {}
    factors = sorted(
        [
            {
                "factor": name,
                "contribution": round(float(item.get("weight", 0.0)) * float(item.get("score", 0.0)), 4),
                "value": item.get("evidence"),
                "bias": item.get("bias", "NEUTRAL"),
                "available": bool(item.get("available")),
            }
            for name, item in components.items()
        ],
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
        "statistical_justification": "Forecast combines regime/structure, liquidity behaviour, positioning, order flow, volatility, and cross-market context. Derived indicators are excluded from directional scoring; estimates remain research evidence, not commands.",
    }
