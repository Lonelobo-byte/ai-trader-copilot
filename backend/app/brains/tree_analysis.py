"""Backward-compatibility shim.

All functions formerly in this file have been relocated:
  - Indicators  → ``app.indicators.*``
  - Decision logic → ``app.brains.decision_engine``

This module re-exports every public name so that existing imports like
``from app.brains.tree_analysis import analyze_trend`` continue to work.
"""
from __future__ import annotations

# ── Indicators ────────────────────────────────────────────────────────────────
from app.indicators.trend import analyze_trend, detect_market_regime, calculate_alignment_score  # noqa: F401
from app.indicators.momentum import analyze_momentum  # noqa: F401
from app.indicators.volatility import atr as _atr  # noqa: F401
from app.indicators.liquidity import (  # noqa: F401
    analyze_liquidity,
    analyze_order_book,
    detect_liquidity_sweep,
)
from app.indicators.funding import analyze_funding_oi_divergence  # noqa: F401
from app.indicators.liquidation_heatmap import calculate_liquidation_heatmap  # noqa: F401

# ── Decision engine ──────────────────────────────────────────────────────────
from app.brains.decision_engine import (  # noqa: F401
    check_data_freshness,
    build_risk_idea,
    calculate_confidence_engine,
    decide_report,
    build_trade_setup,
    build_signal_profile,
)