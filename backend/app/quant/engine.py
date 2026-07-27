"""Composable orchestration for the quantitative research assessment."""
from __future__ import annotations

from typing import Any

from .explainability import explain_forecast
from .microstructure import analyze_microstructure
from .probability import forecast_distribution
from .regimes import classify_market_state
from .risk import build_risk_budget
from .statistics import build_statistical_features


def build_quantitative_assessment(
    candles: list[Any], order_book: dict[str, Any], *, account_value: float,
    max_drawdown_pct: float, max_gross_exposure_pct: float,
    symbol: str = "",
    timeframe: str = "",
    context_features: dict[str, Any] | None = None,
    current_drawdown_pct: float = 0.0,
    gross_exposure_pct: float = 0.0,
) -> dict[str, Any]:
    statistics = build_statistical_features(candles)
    # Keep the probability model tied to the exact evidence snapshot used by
    # the rest of the committee.  Previously funding/OI were requested by the
    # model adapter but silently defaulted to zero because they never reached
    # ``statistics``.
    context_features = context_features or {}
    derivatives = context_features.get("derivatives", {}) or {}
    if statistics.get("available"):
        squeeze = derivatives.get("squeeze", {}) or {}
        statistics.update(
            {
                "funding_rate": float(derivatives.get("funding_rate") or 0.0),
                "open_interest": float(derivatives.get("open_interest") or 0.0),
                "funding_oi_strength": float(squeeze.get("strength") or 50.0),
            }
        )
    microstructure = analyze_microstructure(order_book, candles)
    regime = classify_market_state(statistics, microstructure)
    probability = forecast_distribution(
        statistics, microstructure, regime, symbol=symbol, timeframe=timeframe,
        market_context=context_features.get("market_context", {}),
    )
    risk = build_risk_budget(
        probability, statistics, account_value=account_value, max_drawdown_pct=max_drawdown_pct,
        current_drawdown_pct=current_drawdown_pct,
        gross_exposure_pct=gross_exposure_pct,
        max_gross_exposure_pct=max_gross_exposure_pct,
    )
    explanation = explain_forecast(
        probability, statistics, microstructure, regime,
        market_context=context_features.get("market_context", {}),
    )
    return {
        "platform_mode": "quantitative_research_only",
        "execution_policy": "no_order_submission; probability estimates are not BUY/SELL signals",
        "microstructure": microstructure,
        "statistical_features": statistics,
        "market_state": regime,
        "probability_engine": probability,
        "risk_engine": risk,
        "explainability": explanation,
    }
