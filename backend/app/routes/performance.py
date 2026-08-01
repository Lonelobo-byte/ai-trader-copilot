"""Performance & Trade Analytics Endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth import require_active_subscription
from app.quant.performance_tracker import get_historical_performance_summary

router = APIRouter(
    prefix="/performance",
    tags=["performance"],
    dependencies=[Depends(require_active_subscription)],
)


@router.get("/stats")
async def performance_stats():
    """Return comprehensive system win rate %, profit factor, streaks, and regime metrics."""
    summary = await get_historical_performance_summary()
    return summary
