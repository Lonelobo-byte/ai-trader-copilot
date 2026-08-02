"""Sentiment data sources — Fear & Greed Index and social sentiment.

All sources used here are **free public APIs** with no authentication required.
Each function returns a dict with a consistent shape even on failure so that
downstream consumers never need to special-case errors.
"""
from __future__ import annotations

import logging
from typing import Any

from .http_client import get_http_client

logger = logging.getLogger(__name__)

# ── Fear & Greed Index (alternative.me — free, no key) ───────────────────────


async def fetch_fear_greed_index() -> dict[str, Any]:
    """Return the current Crypto Fear & Greed Index (0-100).

    Source: https://alternative.me/crypto/fear-and-greed-index/
    Rate limit: ~50 req/day (more than enough for periodic scans).
    """
    url = "https://api.alternative.me/fng/"
    params = {"limit": 1, "format": "json"}
    try:
        client = await get_http_client()
        response = await client.get(url, params=params, timeout=5.0)
        response.raise_for_status()
        data = response.json()
        entry = data.get("data", [{}])[0]
        value = int(entry.get("value", 50))

        # Classify into zones
        if value <= 10:
            zone = "EXTREME_FEAR"
        elif value <= 25:
            zone = "FEAR"
        elif value <= 45:
            zone = "MILD_FEAR"
        elif value <= 55:
            zone = "NEUTRAL"
        elif value <= 75:
            zone = "GREED"
        elif value <= 90:
            zone = "HIGH_GREED"
        else:
            zone = "EXTREME_GREED"

        return {
            "value": value,
            "classification": entry.get("value_classification", zone),
            "zone": zone,
            "timestamp": entry.get("timestamp"),
            "source": "alternative.me",
            "available": True,
        }
    except Exception as exc:
        logger.warning(f"Failed to fetch Fear & Greed Index: {exc}")
        return {
            "value": 50,
            "classification": "Neutral",
            "zone": "NEUTRAL",
            "timestamp": None,
            "source": "alternative.me",
            "available": False,
            "error": str(exc),
        }


# ── CoinGecko trending coins (free, no key) ─────────────────────────────────


async def fetch_trending_coins() -> list[dict[str, Any]]:
    """Fetch top-7 trending coins from CoinGecko (free, no auth).

    Useful for AI to understand market narrative and attention shifts.
    """
    url = "https://api.coingecko.com/api/v3/search/trending"
    try:
        client = await get_http_client()
        response = await client.get(url, timeout=5.0)
        response.raise_for_status()
        data = response.json()
        coins = []
        for item in data.get("coins", []):
            coin = item.get("item", {})
            coins.append({
                "name": coin.get("name"),
                "symbol": coin.get("symbol", "").upper(),
                "market_cap_rank": coin.get("market_cap_rank"),
                "price_btc": coin.get("price_btc"),
                "score": coin.get("score"),
            })
        return coins
    except Exception as exc:
        logger.warning(f"Failed to fetch trending coins: {exc}")
        return []


# ── Aggregated sentiment snapshot ────────────────────────────────────────────


async def fetch_sentiment_snapshot() -> dict[str, Any]:
    """Return a combined sentiment snapshot from all free sources.

    This is the single entry-point used by the DataAggregator.
    """
    import asyncio

    fear_greed_task = fetch_fear_greed_index()
    trending_task = fetch_trending_coins()

    fear_greed, trending = await asyncio.gather(
        fear_greed_task, trending_task, return_exceptions=True
    )

    if isinstance(fear_greed, Exception):
        logger.error(f"Fear & Greed fetch exception: {fear_greed}")
        fear_greed = {"value": 50, "zone": "NEUTRAL", "available": False}
    if isinstance(trending, Exception):
        logger.error(f"Trending coins fetch exception: {trending}")
        trending = []

    return {
        "fear_greed": fear_greed,
        "trending_coins": trending,
    }
