"""Signal management endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..signal_service import (
    cancel_signal,
    get_active_signal,
    list_signal_history,
    _view as signal_service_view,
)

router = APIRouter(prefix="/signals", tags=["signals"])


class SignalCancelRequest(BaseModel):
    reason: str = Field(default="Dismissed by operator.", min_length=1, max_length=300)


@router.get("/active")
async def active_signal(symbol: str = "BTCUSDT", timeframe: str = "15m"):
    signal = await get_active_signal(symbol.upper().strip(), timeframe)
    return {"signal": signal_service_view(signal)}


@router.get("/history")
async def signal_history(limit: int = 20):
    return {"signals": await list_signal_history(max(1, min(limit, 100)))}


@router.post("/{signal_id}/cancel")
async def dismiss_signal(signal_id: int, request: SignalCancelRequest):
    signal = await cancel_signal(signal_id, request.reason)
    if signal is None:
        raise HTTPException(status_code=404, detail="Signal not found.")
    return {"signal": signal}
