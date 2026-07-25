"""Machine learning feature engineering layer.

Constructs flat, normalized, and stationary feature dicts from technical
analysis and market microstructure indicators, suitable as input vectors
for gradient boosting (XGBoost/LightGBM) or neural network models.
"""
from __future__ import annotations

from typing import Any

from app.data_sources.binance_public import Candle, completed_candles
from app.indicators.funding import analyze_funding_oi_divergence
from app.indicators.liquidity import analyze_order_book
from app.indicators.momentum import analyze_momentum
from app.indicators.quantitative import (
    hurst_exponent,
    parkinson_volatility,
    return_autocorrelation,
    return_kurtosis,
    return_skewness,
    rolling_z_score,
)
from app.indicators.volatility import atr, bollinger_bands


def build_ml_features(
    symbol: str,
    timeframe: str,
    candles: list[Candle],
    ticker: dict[str, Any],
    order_book: dict[str, Any],
    extra_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile and normalize quantitative and microstructure metrics into a flat feature set."""
    closed = completed_candles(candles)
    closes = [c.close for c in closed]
    volumes = [c.volume for c in closed]

    # 1. Market Microstructure Features
    avg_vol = sum(volumes[-20:]) / 20.0 if len(volumes) >= 20 else 0.0
    book_metrics = analyze_order_book(order_book, avg_candle_volume=avg_vol)

    # 2. Statistical Features (Stationary)
    h_exp = hurst_exponent(closes)
    p_vol = parkinson_volatility(closed)
    skew = return_skewness(closes)
    kurt = return_kurtosis(closes)
    acf_1 = return_autocorrelation(closes, lag=1)
    acf_3 = return_autocorrelation(closes, lag=3)
    z_close = rolling_z_score(closes)
    z_volume = rolling_z_score(volumes)

    # 3. Macro & Funding Features
    funding_rate = float((extra_context or {}).get("funding_rate", 0.0))
    open_interest = float((extra_context or {}).get("open_interest", 0.0))
    funding_oi = analyze_funding_oi_divergence(funding_rate, open_interest)

    # 4. Derived Traditional Indicators (as raw normalized features)
    mom = analyze_momentum(candles)
    bb = bollinger_bands(candles)
    macd_hist = mom.get("macd", {}).get("histogram", 0.0)

    # Combine into flat features dictionary
    features = {
        "symbol": symbol,
        "timeframe": timeframe,
        "micro_imbalance_5": book_metrics.get("imbalance_5", 0.0),
        "micro_imbalance_10": book_metrics.get("imbalance_10", 0.0),
        "micro_imbalance_20": book_metrics.get("imbalance_20", 0.0),
        "micro_spread_pct": book_metrics.get("spread_pct", 0.0),
        "micro_bid_density": book_metrics.get("bid_density_1pct", 0.0),
        "micro_ask_density": book_metrics.get("ask_density_1pct", 0.0),
        "micro_absorption_ratio": book_metrics.get("absorption_ratio", 0.0),
        "stat_hurst_exponent": round(h_exp, 4),
        "stat_parkinson_volatility": round(p_vol, 5),
        "stat_return_skewness": round(skew, 4),
        "stat_return_kurtosis": round(kurt, 4),
        "stat_autocorrelation_lag1": round(acf_1, 4),
        "stat_autocorrelation_lag3": round(acf_3, 4),
        "stat_z_score_close": round(z_close, 4),
        "stat_z_score_volume": round(z_volume, 4),
        "funding_rate": funding_rate,
        "open_interest": open_interest,
        "funding_oi_strength": funding_oi.get("strength", 50.0),
        "derived_rsi": mom.get("rsi", 50.0),
        "derived_macd_histogram": macd_hist,
        "derived_bb_bandwidth": bb.get("bandwidth", 0.0),
        "derived_bb_percent_b": bb.get("percent_b", 50.0),
    }

    return features
