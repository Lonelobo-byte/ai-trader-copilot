"""Liquidation heatmap / magnet estimation."""
from __future__ import annotations

from typing import Any


def calculate_liquidation_heatmap(
    current_price: float,
    previous_high: float,
    previous_low: float,
    atr: float,
    open_interest: float,
) -> dict[str, Any]:
    # 4 potential Long levels below current price
    longs = [
        {"price": current_price - 1.0 * atr, "weight": 0.20},
        {"price": current_price - 2.0 * atr, "weight": 0.15},
        {"price": previous_low - 0.5 * atr, "weight": 0.40},
        {"price": previous_low - 1.5 * atr, "weight": 0.25},
    ]
    # 4 potential Short levels above current price
    shorts = [
        {"price": current_price + 1.0 * atr, "weight": 0.20},
        {"price": current_price + 2.0 * atr, "weight": 0.15},
        {"price": previous_high + 0.5 * atr, "weight": 0.40},
        {"price": previous_high + 1.5 * atr, "weight": 0.25},
    ]

    for p in longs:
        dist = ((current_price - p["price"]) / current_price) * 100 if current_price > 0 else 999.0
        p["distance_pct"] = max(0.0, dist)
        p["strength"] = round(max(30.0, min(99.0, 100.0 - (p["distance_pct"] * 12.0) + (p["weight"] * 40.0))))

    for p in shorts:
        dist = ((p["price"] - current_price) / current_price) * 100 if current_price > 0 else 999.0
        p["distance_pct"] = max(0.0, dist)
        p["strength"] = round(max(30.0, min(99.0, 100.0 - (p["distance_pct"] * 12.0) + (p["weight"] * 40.0))))

    nearest_long = min(longs, key=lambda x: x["distance_pct"])
    nearest_short = min(shorts, key=lambda x: x["distance_pct"])

    return {
        "nearest_short_magnet": round(nearest_short["price"], 4),
        "short_distance_pct": round(nearest_short["distance_pct"], 2),
        "short_magnet_strength": int(nearest_short["strength"]),
        "estimated_short_liquidity": round(open_interest * nearest_short["weight"], 2),
        "nearest_long_magnet": round(nearest_long["price"], 4),
        "long_distance_pct": round(nearest_long["distance_pct"], 2),
        "long_magnet_strength": int(nearest_long["strength"]),
        "estimated_long_liquidity": round(open_interest * nearest_long["weight"], 2),
    }
