"""Liquidity indicators — sweep detection, order book analysis."""
from __future__ import annotations

from typing import Any

from app.data_sources.binance_public import Candle
from ._math import pct


def detect_liquidity_sweep(candles: list[Candle], lookback: int = 24) -> dict[str, Any]:
    """Return the most recent sweep and its current completed-candle lifecycle."""
    from app.indicators.market_story import build_market_story, observable_liquidity_sweep

    story = build_market_story(candles, sweep_lookback=max(5, lookback))
    return observable_liquidity_sweep(story)


def analyze_liquidity(ticker: dict[str, Any], order_book: dict[str, Any]) -> dict[str, Any]:
    quote_volume = float(ticker.get("quoteVolume", 0.0))
    bid = float(ticker.get("bidPrice", 0.0))
    ask = float(ticker.get("askPrice", 0.0))
    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else float(ticker.get("lastPrice", 0.0))
    spread_pct = pct(ask - bid, mid) if mid > 0 and ask >= bid else 999.0

    bids = order_book.get("bids", [])[:20]
    asks = order_book.get("asks", [])[:20]
    top_bid_notional = sum(float(price) * float(qty) for price, qty in bids)
    top_ask_notional = sum(float(price) * float(qty) for price, qty in asks)

    volume_passed = quote_volume >= 5_000_000
    spread_passed = spread_pct <= 0.10
    min_side_notional = 10_000 if quote_volume >= 100_000_000 else 25_000
    depth_passed = top_bid_notional >= min_side_notional and top_ask_notional >= min_side_notional
    passed = volume_passed and spread_passed and depth_passed

    if passed:
        reason = "liquid"
    elif not volume_passed:
        reason = "low_24h_quote_volume"
    elif not spread_passed:
        reason = "wide_spread"
    else:
        reason = "thin_order_book_snapshot"

    return {
        "passed": passed,
        "quote_volume_24h": round(quote_volume, 2),
        "spread_pct": round(spread_pct, 5),
        "top20_bid_notional": round(top_bid_notional, 2),
        "top20_ask_notional": round(top_ask_notional, 2),
        "min_side_notional": round(min_side_notional, 2),
        "reason": reason,
    }


def analyze_order_book(order_book: dict[str, Any], avg_candle_volume: float = 0.0) -> dict[str, Any]:
    bids = order_book.get("bids", [])
    asks = order_book.get("asks", [])

    if not bids or not asks:
        return {
            "pressure": "balanced",
            "imbalance": 0.0,
            "imbalance_5": 0.0,
            "imbalance_10": 0.0,
            "imbalance_20": 0.0,
            "spread_pct": 0.0,
            "bid_density_1pct": 0.0,
            "ask_density_1pct": 0.0,
            "absorption_ratio": 0.0,
            "bid_notional_top20": 0.0,
            "ask_notional_top20": 0.0,
        }

    # Calculate notional for different levels
    def notional_depth(levels: list, depth: int) -> float:
        return sum(float(p) * float(q) for p, q in levels[:depth])

    b5, a5 = notional_depth(bids, 5), notional_depth(asks, 5)
    b10, a10 = notional_depth(bids, 10), notional_depth(asks, 10)
    b20, a20 = notional_depth(bids, 20), notional_depth(asks, 20)

    # Imbalances
    imb_5 = (b5 - a5) / (b5 + a5) if (b5 + a5) > 0 else 0.0
    imb_10 = (b10 - a10) / (b10 + a10) if (b10 + a10) > 0 else 0.0
    imb_20 = (b20 - a20) / (b20 + a20) if (b20 + a20) > 0 else 0.0

    # Spread analysis
    top_bid = float(bids[0][0])
    top_ask = float(asks[0][0])
    mid = (top_bid + top_ask) / 2.0
    spread_pct = pct(top_ask - top_bid, mid) if mid > 0 else 0.0

    # Book Density: notional within 1% of the mid price
    density_bid = sum(float(p) * float(q) for p, q in bids if float(p) >= mid * 0.99)
    density_ask = sum(float(p) * float(q) for p, q in asks if float(p) <= mid * 1.01)

    # Order absorption indicator
    density_total = density_bid + density_ask
    absorption_ratio = (avg_candle_volume * mid) / density_total if density_total > 0 and avg_candle_volume > 0 else 0.0

    # Combine imbalances to get a weighted overall score
    imbalance = 0.4 * imb_5 + 0.4 * imb_10 + 0.2 * imb_20
    if imbalance > 0.15:
        pressure = "buyers"
    elif imbalance < -0.15:
        pressure = "sellers"
    else:
        pressure = "balanced"

    return {
        "pressure": pressure,
        "imbalance": round(imbalance, 4),
        "imbalance_5": round(imb_5, 4),
        "imbalance_10": round(imb_10, 4),
        "imbalance_20": round(imb_20, 4),
        "spread_pct": round(spread_pct, 5),
        "bid_density_1pct": round(density_bid, 2),
        "ask_density_1pct": round(density_ask, 2),
        "absorption_ratio": round(absorption_ratio, 4),
        "bid_notional_top20": round(b20, 2),
        "ask_notional_top20": round(a20, 2),
    }
