"""CoinGlass-style derivatives intelligence from free Binance Futures endpoints.

All data is sourced from the **public Binance Futures API** (no API key needed).
This module provides derivatives-specific analytics that go beyond the basic
funding rate and OI already in ``binance_futures.py``:

- Long/Short account ratio
- Top-trader position ratio
- Taker buy/sell volume
- Open-interest history (for OI change rate)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_FAPI_BASE = "https://fapi.binance.com"
_TIMEOUT = httpx.Timeout(10.0)


async def _get(path: str, params: dict[str, Any] | None = None, base: str = _FAPI_BASE) -> Any:
    url = f"{base}{path}"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.get(url, params=params)
    response.raise_for_status()
    return response.json()


# ── Long/Short ratio (all accounts) ─────────────────────────────────────────


async def fetch_long_short_ratio(symbol: str, period: str = "5m", limit: int = 10) -> dict[str, Any]:
    """Global long/short account ratio.

    Endpoint: GET /futures/data/globalLongShortAccountRatio
    """
    try:
        data = await _get(
            "/futures/data/globalLongShortAccountRatio",
            {"symbol": symbol.upper(), "period": period, "limit": limit},
        )
        if not data:
            return {"available": False, "ratio": 1.0}

        latest = data[-1] if isinstance(data, list) else data
        long_pct = float(latest.get("longAccount", 0.5))
        short_pct = float(latest.get("shortAccount", 0.5))
        ratio = float(latest.get("longShortRatio", 1.0))

        # Calculate trend from the series
        ratios = [float(d.get("longShortRatio", 1.0)) for d in data] if isinstance(data, list) else [ratio]
        trend = "NEUTRAL"
        if len(ratios) >= 3:
            recent_avg = sum(ratios[-3:]) / 3
            early_avg = sum(ratios[:3]) / 3
            if recent_avg > early_avg * 1.05:
                trend = "LONGS_INCREASING"
            elif recent_avg < early_avg * 0.95:
                trend = "SHORTS_INCREASING"

        return {
            "available": True,
            "long_pct": round(long_pct * 100, 2),
            "short_pct": round(short_pct * 100, 2),
            "ratio": round(ratio, 4),
            "trend": trend,
            "period": period,
            "data_points": len(ratios),
        }
    except Exception as exc:
        logger.warning(f"Failed to fetch long/short ratio for {symbol}: {exc}")
        return {"available": False, "ratio": 1.0, "error": str(exc)}


# ── Top trader position ratio ───────────────────────────────────────────────


async def fetch_top_trader_positions(symbol: str, period: str = "5m", limit: int = 10) -> dict[str, Any]:
    """Top-trader long/short position ratio.

    Endpoint: GET /futures/data/topLongShortPositionRatio
    """
    try:
        data = await _get(
            "/futures/data/topLongShortPositionRatio",
            {"symbol": symbol.upper(), "period": period, "limit": limit},
        )
        if not data:
            return {"available": False}

        latest = data[-1] if isinstance(data, list) else data
        ratio = float(latest.get("longShortRatio", 1.0))
        long_pct = float(latest.get("longAccount", 0.5))
        short_pct = float(latest.get("shortAccount", 0.5))

        # Smart money bias detection
        bias = "NEUTRAL"
        if ratio > 1.5:
            bias = "SMART_MONEY_LONG"
        elif ratio < 0.67:
            bias = "SMART_MONEY_SHORT"

        return {
            "available": True,
            "ratio": round(ratio, 4),
            "long_pct": round(long_pct * 100, 2),
            "short_pct": round(short_pct * 100, 2),
            "bias": bias,
            "period": period,
        }
    except Exception as exc:
        logger.warning(f"Failed to fetch top trader positions for {symbol}: {exc}")
        return {"available": False, "error": str(exc)}


# ── Taker buy/sell volume & CVD (Cumulative Volume Delta) ─────────────────


async def fetch_taker_buy_sell_volume(symbol: str, period: str = "5m", limit: int = 30) -> dict[str, Any]:
    """Taker buy vs sell volume ratio & Cumulative Volume Delta (CVD).

    Endpoint: GET /futures/data/takerlongshortRatio
    """
    try:
        data = await _get(
            "/futures/data/takerlongshortRatio",
            {"symbol": symbol.upper(), "period": period, "limit": limit},
        )
        if not data or not isinstance(data, list):
            return {"available": False, "cvd_trend": "CVD_NEUTRAL"}

        latest = data[-1]
        buy_vol = float(latest.get("buyVol", 0.0))
        sell_vol = float(latest.get("sellVol", 0.0))
        ratio = float(latest.get("buySellRatio", 1.0))

        # Calculate Cumulative Volume Delta (CVD) across time series
        net_deltas = [float(d.get("buyVol", 0.0)) - float(d.get("sellVol", 0.0)) for d in data]
        total_cvd = sum(net_deltas)
        recent_cvd = sum(net_deltas[-3:]) if len(net_deltas) >= 3 else total_cvd

        cvd_trend = "CVD_NEUTRAL"
        if recent_cvd > 0 and total_cvd > 0:
            cvd_trend = "CVD_BULLISH_ACCUMULATION"
        elif recent_cvd < 0 and total_cvd < 0:
            cvd_trend = "CVD_BEARISH_DISTRIBUTION"

        # Aggressive buying/selling detection
        aggression = "NEUTRAL"
        if ratio > 1.2:
            aggression = "AGGRESSIVE_BUYING"
        elif ratio > 1.05:
            aggression = "MILD_BUYING"
        elif ratio < 0.8:
            aggression = "AGGRESSIVE_SELLING"
        elif ratio < 0.95:
            aggression = "MILD_SELLING"

        return {
            "available": True,
            "buy_volume": round(buy_vol, 2),
            "sell_volume": round(sell_vol, 2),
            "ratio": round(ratio, 4),
            "aggression": aggression,
            "cvd_net_volume": round(total_cvd, 2),
            "cvd_trend": cvd_trend,
            "period": period,
        }
    except Exception as exc:
        logger.warning(f"Failed to fetch taker volume/CVD for {symbol}: {exc}")
        return {"available": False, "cvd_trend": "CVD_NEUTRAL", "error": str(exc)}


# ── Open Interest history (for OI change rate & Squeeze Warnings) ─────────


async def fetch_oi_history(symbol: str, period: str = "5m", limit: int = 30) -> dict[str, Any]:
    """Fetch OI history to calculate OI change rate, momentum, and Squeeze Alerts.

    Endpoint: GET /futures/data/openInterestHist
    """
    try:
        data = await _get(
            "/futures/data/openInterestHist",
            {"symbol": symbol.upper(), "period": period, "limit": limit},
        )
        if not data or not isinstance(data, list) or len(data) < 2:
            return {"available": False, "squeeze_warning": "NO_SQUEEZE"}

        oi_values = [float(d.get("sumOpenInterest", 0.0)) for d in data]
        latest_oi = oi_values[-1]
        earliest_oi = oi_values[0]

        # OI change rate
        oi_change_pct = ((latest_oi - earliest_oi) / earliest_oi * 100) if earliest_oi > 0 else 0.0

        # Short-term vs long-term OI momentum
        mid = len(oi_values) // 2
        short_term_avg = sum(oi_values[mid:]) / len(oi_values[mid:]) if oi_values[mid:] else latest_oi
        long_term_avg = sum(oi_values[:mid]) / len(oi_values[:mid]) if oi_values[:mid] else latest_oi

        oi_momentum = "NEUTRAL"
        if short_term_avg > long_term_avg * 1.03:
            oi_momentum = "INCREASING"
        elif short_term_avg < long_term_avg * 0.97:
            oi_momentum = "DECREASING"

        # Squeeze Warning Predictor
        squeeze_warning = "NO_SQUEEZE"
        if oi_change_pct > 2.5 and oi_momentum == "INCREASING":
            squeeze_warning = "SHORT_SQUEEZE_WARNING"
        elif oi_change_pct > 5.0:
            squeeze_warning = "LONG_SQUEEZE_WARNING"

        return {
            "available": True,
            "latest_oi": round(latest_oi, 2),
            "oi_change_pct": round(oi_change_pct, 2),
            "oi_momentum": oi_momentum,
            "squeeze_warning": squeeze_warning,
            "data_points": len(oi_values),
            "period": period,
        }
    except Exception as exc:
        logger.warning(f"Failed to fetch OI history for {symbol}: {exc}")
        return {"available": False, "squeeze_warning": "NO_SQUEEZE", "error": str(exc)}


# ── Aggregated derivatives intelligence ──────────────────────────────────────


async def fetch_derivatives_intelligence(symbol: str) -> dict[str, Any]:
    """Fetch all derivatives data concurrently. Single entry-point for DataAggregator."""
    ls_task = fetch_long_short_ratio(symbol)
    top_task = fetch_top_trader_positions(symbol)
    taker_task = fetch_taker_buy_sell_volume(symbol)
    oi_hist_task = fetch_oi_history(symbol)

    ls_ratio, top_positions, taker_vol, oi_hist = await asyncio.gather(
        ls_task, top_task, taker_task, oi_hist_task, return_exceptions=True
    )

    def safe(res: Any, default: dict) -> dict:
        return res if isinstance(res, dict) else default

    return {
        "long_short_ratio": safe(ls_ratio, {"available": False}),
        "top_trader_positions": safe(top_positions, {"available": False}),
        "taker_buy_sell_volume": safe(taker_vol, {"available": False, "cvd_trend": "CVD_NEUTRAL"}),
        "oi_history": safe(oi_hist, {"available": False, "squeeze_warning": "NO_SQUEEZE"}),
    }
