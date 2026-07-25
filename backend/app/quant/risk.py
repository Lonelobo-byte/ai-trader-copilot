"""Portfolio-aware capital constraints for research recommendations."""
from __future__ import annotations

from typing import Any


def build_risk_budget(
    forecast: dict[str, Any], stats: dict[str, Any], *, account_value: float, max_drawdown_pct: float,
    current_drawdown_pct: float = 0.0, gross_exposure_pct: float = 0.0, max_gross_exposure_pct: float = 100.0,
) -> dict[str, Any]:
    volatility = max(float(stats.get("return_volatility", 0.0)), 1e-6)
    edge = max(float(forecast.get("expected_value", 0.0)), 0.0)
    kelly_fraction = min(edge / max(volatility ** 2, 1e-9), 0.25)
    fractional_kelly = kelly_fraction * 0.25
    drawdown_remaining = max(max_drawdown_pct - current_drawdown_pct, 0.0) / max(max_drawdown_pct, 1e-9)
    exposure_remaining = max(max_gross_exposure_pct - gross_exposure_pct, 0.0) / max(max_gross_exposure_pct, 1e-9)
    risk_fraction = min(fractional_kelly, 0.01) * drawdown_remaining * exposure_remaining
    notional = account_value * risk_fraction / volatility
    stop_distance = volatility * (2.0 if float(stats.get("volatility_percentile", 0.5)) > 0.8 else 1.5)
    blockers = []
    if current_drawdown_pct >= max_drawdown_pct:
        blockers.append("Maximum drawdown limit reached.")
    if gross_exposure_pct >= max_gross_exposure_pct:
        blockers.append("Portfolio gross-exposure limit reached.")
    if edge <= 0:
        blockers.append("Expected value is non-positive after estimated risk.")
    return {
        "allocation_status": "blocked" if blockers else "research_eligible",
        "blockers": blockers,
        "fractional_kelly": round(fractional_kelly, 5),
        "volatility_adjusted_risk_fraction": round(risk_fraction, 5),
        "illustrative_max_notional_usd": round(max(notional, 0.0), 2),
        "dynamic_invalidation_distance_pct": round(stop_distance * 100, 4),
        "max_drawdown_pct": max_drawdown_pct,
        "current_drawdown_pct": current_drawdown_pct,
        "gross_exposure_pct": gross_exposure_pct,
        "max_gross_exposure_pct": max_gross_exposure_pct,
        "note": "Sizing is a portfolio constraint for a validated strategy, not an instruction to trade.",
    }
