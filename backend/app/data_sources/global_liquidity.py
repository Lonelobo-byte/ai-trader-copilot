"""Crypto risk-appetite proxy from the public Fear & Greed feed.

This source does not provide global net liquidity, stablecoin flows, or central
bank balance-sheet data. It must not be labelled as such by downstream code.
"""
from __future__ import annotations

import logging
from typing import Any
import httpx

logger = logging.getLogger(__name__)

_FEAR_GREED_API = "https://api.alternative.me/fng/"
_TIMEOUT = httpx.Timeout(5.0)


async def fetch_global_liquidity_index() -> dict[str, Any]:
    """Fetch a clearly-labelled Fear & Greed risk-appetite proxy."""
    try:
        score = 65  # Base default
        fng_val = 50
        fng_classification = "Neutral"

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(_FEAR_GREED_API, params={"limit": 1})
            if resp.status_code == 200:
                data = resp.json()
                if "data" in data and len(data["data"]) > 0:
                    fng_val = int(data["data"][0].get("value", 50))
                    fng_classification = data["data"][0].get("value_classification", "Neutral")

        score = min(100, max(0, fng_val))
        status = "RISK_APPETITE_NEUTRAL"
        if score >= 60:
            status = "RISK_APPETITE_POSITIVE"
        elif score <= 40:
            status = "RISK_APPETITE_NEGATIVE"

        return {
            "available": True,
            "source": "Alternative.me Fear & Greed Index",
            "risk_appetite_score": score,
            "risk_appetite_status": status,
            "fear_and_greed_val": fng_val,
            "fear_and_greed_label": fng_classification,
            "limitations": "Fear & Greed is a sentiment proxy, not a global-liquidity measure.",
        }
    except Exception as exc:
        logger.warning(f"Failed to fetch global liquidity index: {exc}")
        return {
            "available": False,
            "risk_appetite_score": 50,
            "risk_appetite_status": "RISK_APPETITE_NEUTRAL",
            "error": str(exc),
        }
