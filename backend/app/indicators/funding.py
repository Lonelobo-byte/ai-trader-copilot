"""Funding rate & Open Interest divergence analysis."""
from __future__ import annotations

from typing import Any


def analyze_funding_oi_divergence(funding_rate: float, open_interest: float) -> dict[str, Any]:
    if funding_rate < -0.0001:
        signal = "POTENTIAL_SHORT_SQUEEZE"
        description = f"Negative funding rate ({funding_rate*100:.4f}%) suggests crowded shorts. High squeeze risk on upward move."
        strength = min(100.0, abs(funding_rate) * 500000.0)
    elif funding_rate > 0.0003:
        signal = "POTENTIAL_LONG_SQUEEZE"
        description = f"Highly positive funding rate ({funding_rate*100:.4f}%) suggests crowded longs. High flush risk on downward move."
        strength = min(100.0, funding_rate * 200000.0)
    else:
        signal = "NEUTRAL"
        description = f"Funding rate ({funding_rate*100:.4f}%) is within normal bounds."
        strength = 50.0

    return {
        "signal": signal,
        "description": description,
        "strength": round(strength, 2),
        "funding_rate": funding_rate,
        "open_interest": open_interest,
    }
