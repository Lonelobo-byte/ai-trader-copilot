"""Performance & Trade Analytics Endpoints."""
from __future__ import annotations

from fastapi import APIRouter

from app.quant.performance_tracker import get_historical_performance_summary

router = APIRouter(prefix="/performance", tags=["performance"])


@router.get("/stats")
async def performance_stats():
    """Return comprehensive system win rate %, profit factor, streaks, and regime metrics."""
    summary = await get_historical_performance_summary()
    return summary
