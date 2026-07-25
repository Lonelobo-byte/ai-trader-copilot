from __future__ import annotations

from typing import Any


def position_size(account_size_usd: float, risk_pct: float, risk_per_unit: float) -> dict[str, float]:
    if account_size_usd <= 0 or risk_pct <= 0 or risk_per_unit <= 0:
        return {"risk_amount_usd": 0.0, "units": 0.0}
    risk_amount = account_size_usd * (risk_pct / 100)
    return {
        "risk_amount_usd": round(risk_amount, 2),
        "units": round(risk_amount / risk_per_unit, 8),
    }


def calculate_kelly_sizing(
    account_size_usd: float,
    win_rate_pct: float,
    risk_reward: float,
    risk_per_unit: float,
    empirical_perf: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Use empirical win rate from database performance tracker if sufficient sample exists
    effective_win_rate = win_rate_pct
    data_source = "theoretical_default"

    if empirical_perf and empirical_perf.get("decided_trades", 0) >= 5:
        effective_win_rate = float(empirical_perf.get("win_rate_pct", win_rate_pct))
        data_source = f"empirical_db ({empirical_perf.get('decided_trades')} trades)"

    if account_size_usd <= 0 or not 0 <= effective_win_rate <= 100 or risk_reward <= 0 or risk_per_unit <= 0:
        return {
            "kelly_pct": 0.0,
            "risk_amount_usd": 0.0,
            "units": 0.0,
            "status": "no_history",
            "effective_win_rate": effective_win_rate,
            "data_source": data_source,
        }
    p = effective_win_rate / 100.0
    b = risk_reward
    # Half-Kelly capped at 2% per trade to protect capital
    f_star = 0.5 * (p - (1.0 - p) / b)
    f_star = min(max(f_star, 0.0), 0.02)
    kelly_pct = max(0.0, f_star * 100.0)
    status = "active" if f_star > 0 else "negative"

    risk_amount = account_size_usd * (kelly_pct / 100.0)
    units = risk_amount / risk_per_unit

    return {
        "kelly_pct": round(kelly_pct, 2),
        "risk_amount_usd": round(risk_amount, 2),
        "units": round(units, 8),
        "status": status,
        "effective_win_rate": round(effective_win_rate, 1),
        "data_source": data_source,
    }


