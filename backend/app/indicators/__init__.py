"""Indicators package — pure-function technical analysis calculations.

Each submodule contains indicators grouped by category (trend, momentum,
volatility, volume, liquidity, structure).  All functions operate on
``Candle`` lists and return plain dicts — no side effects.
"""
from .trend import (
    analyze_trend,
    calculate_alignment_score,
    detect_market_regime,
)
from .momentum import analyze_momentum, macd, stochastic_rsi
from .volatility import atr, bollinger_bands
from .liquidity import (
    analyze_liquidity,
    analyze_order_book,
    detect_liquidity_sweep,
)
from .funding import analyze_funding_oi_divergence
from .liquidation_heatmap import calculate_liquidation_heatmap
from .structure import (
    detect_bos,
    detect_choch,
    find_order_blocks,
    find_fair_value_gaps,
)
from .quantitative import (
    hurst_exponent,
    parkinson_volatility,
    return_skewness,
    return_kurtosis,
    return_autocorrelation,
    rolling_z_score,
)

__all__ = [
    "analyze_trend",
    "calculate_alignment_score",
    "detect_market_regime",
    "analyze_momentum",
    "macd",
    "stochastic_rsi",
    "atr",
    "bollinger_bands",
    "analyze_liquidity",
    "analyze_order_book",
    "detect_liquidity_sweep",
    "analyze_funding_oi_divergence",
    "calculate_liquidation_heatmap",
    "detect_bos",
    "detect_choch",
    "find_order_blocks",
    "find_fair_value_gaps",
    "hurst_exponent",
    "parkinson_volatility",
    "return_skewness",
    "return_kurtosis",
    "return_autocorrelation",
    "rolling_z_score",
]
