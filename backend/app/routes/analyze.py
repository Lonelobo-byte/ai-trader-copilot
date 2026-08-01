"""Analysis endpoints – REST and WebSocket."""
from __future__ import annotations

import json
import logging
import asyncio
from contextlib import suppress
from typing import Any, AsyncGenerator

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from ..analysis_pipeline import run_full_analysis
from ..data_sources.binance_public import Candle
from ..data_sources.data_aggregator import fetch_market_intelligence_cached
from ..data_sources.binance_ws import BinanceWSSubscriber
from ..settings import get_settings
from ..quant.research import list_hypotheses, validate_series
from ..auth import require_active_subscription, websocket_subscription
from ..db.models import User
from ..user_ai import UserAIConnectionError, ai_cache_identity, resolve_user_ai_config
from ..rate_limit import enforce_rate_limit
from ..research_capacity import (
    RESEARCH_SLOT_HEARTBEAT_SECONDS,
    ResearchCapacityExceeded,
    acquire_research_slot,
    heartbeat_research_slot,
    research_capacity_view,
    release_research_slot,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _analysis_rate_limit(http_request: Request) -> None:
    # This protects external market-data work before the more specific AI
    # budget guard decides whether a model call may be made.
    enforce_rate_limit(http_request, "analysis", limit=12, window_seconds=60)


async def _reserved_rest_research_slot(user: User = Depends(require_active_subscription)) -> AsyncGenerator[User, None]:
    """Reserve capacity for the full lifetime of a one-shot research request."""
    try:
        lease = await acquire_research_slot(user=user, symbol="REST", timeframe="ONESHOT", channel="rest")
    except ResearchCapacityExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": "20"},
        ) from exc
    try:
        yield user
    finally:
        await release_research_slot(lease.id)


async def _maintain_websocket_research_slot(websocket: WebSocket, lease_id: str) -> None:
    while True:
        await asyncio.sleep(RESEARCH_SLOT_HEARTBEAT_SECONDS)
        if not await heartbeat_research_slot(lease_id):
            await websocket.close(code=4429, reason="Research capacity lease expired")
            return


class AnalyzeRequest(BaseModel):
    symbol: str = Field(default="BTCUSDT", min_length=3, max_length=20)
    timeframe: str = Field(default="15m", min_length=1, max_length=10)
    use_ai: bool = True
    candle_limit: int = Field(default=200, ge=60, le=1000)

class AlphaValidationRequest(BaseModel):
    feature: list[float] = Field(min_length=30, max_length=100_000)
    future_returns: list[float] = Field(min_length=30, max_length=100_000)
    train_fraction: float = Field(default=0.7, gt=0.5, lt=0.9)


@router.get("/research/alpha/hypotheses", dependencies=[Depends(require_active_subscription)])
async def alpha_hypotheses():
    """Research backlog; data requirements prevent misleading proxy claims."""
    return list_hypotheses()


@router.post("/research/alpha/validate", dependencies=[Depends(require_active_subscription)])
async def validate_alpha(request: AlphaValidationRequest):
    try:
        return validate_series(request.feature, request.future_returns, train_fraction=request.train_fraction)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/research/capacity")
async def research_capacity(user: User = Depends(require_active_subscription)):
    """Membership plan and active live-research leases for the current user."""
    return await research_capacity_view(user)

@router.post("/analyze", dependencies=[Depends(_analysis_rate_limit)])
async def analyze(request: AnalyzeRequest, user: User = Depends(_reserved_rest_research_slot)):
    settings = get_settings()
    symbol = request.symbol.upper().strip()

    try:
        # Fetch one complete snapshot for the entire analysis. The snapshot
        # includes core data, higher timeframes, derivatives and context.
        intelligence = await fetch_market_intelligence_cached(
            symbol, request.timeframe, settings, candle_limit=request.candle_limit
        )
        candles = intelligence.get("candles", [])
        order_book_raw = intelligence.get("order_book", {"bids": [], "asks": []})
        ticker = intelligence.get("ticker", {})
        if not candles or not ticker:
            raise HTTPException(status_code=502, detail="Core market data unavailable.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:500] if exc.response is not None else str(exc)
        raise HTTPException(status_code=502, detail=f"Binance API error: {detail}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Market data request failed: {exc}") from exc

    try:
        ai_override = await resolve_user_ai_config(user.id)
    except UserAIConnectionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    use_ai = request.use_ai and (ai_override is not None or settings.allow_platform_ai_fallback)
    payload, _ = await run_full_analysis(
        symbol=symbol,
        timeframe=request.timeframe,
        candles=candles,
        ticker=ticker,
        order_book_raw=order_book_raw,
        settings=settings,
        use_ai=use_ai,
        market_intelligence=intelligence,
        ai_override=ai_override,
        ai_cache_key=ai_cache_identity(user.id, ai_override),
        reconcile_signals=True,
    )
    return payload


@router.websocket("/ws/analyze")
async def websocket_analyze(websocket: WebSocket):
    user = await websocket_subscription(websocket)
    if user is None:
        return
    # Echo the non-secret protocol identifier selected by the browser.  The
    # JWT is the second requested subprotocol and is never reflected/logged.
    await websocket.accept(subprotocol="atc-auth")
    subscriber = None
    research_lease = None
    heartbeat_task = None
    try:
        config_msg = await websocket.receive_text()
        config = json.loads(config_msg)
        symbol = config.get("symbol", "BTCUSDT").upper().strip()
        timeframe = config.get("timeframe", "15m")
        use_ai = config.get("use_ai", True)

        try:
            research_lease = await acquire_research_slot(
                user=user, symbol=symbol, timeframe=timeframe, channel="websocket"
            )
        except ResearchCapacityExceeded as exc:
            await websocket.send_json({
                "error": str(exc),
                "code": "research_capacity_exceeded",
                "plan_code": exc.plan_code,
                "limit": exc.limit,
                "active_slots": exc.active_slots,
            })
            await websocket.close(code=4429, reason="Research capacity reached")
            return
        heartbeat_task = asyncio.create_task(_maintain_websocket_research_slot(websocket, research_lease.id))

        settings = get_settings()
        try:
            ai_override = await resolve_user_ai_config(user.id)
        except UserAIConnectionError as exc:
            await websocket.send_json({"error": str(exc), "code": "ai_connection_invalid"})
            await websocket.close(code=4400)
            return
        use_ai = bool(use_ai) and (ai_override is not None or settings.allow_platform_ai_fallback)
        subscriber = BinanceWSSubscriber(symbol, timeframe, settings)

        last_ai_open_time = 0
        # The subscriber supplies fresh price/book data.  Context data (higher
        # timeframes, derivatives, news, macro) is shared briefly across tabs
        # instead of triggering a full upstream fan-out on every websocket tick.
        intelligence = None

        async for event in subscriber.start():
            raw_candles = event["candles"]
            candles = [
                Candle(
                    open_time=c["open_time"],
                    open=c["open"],
                    high=c["high"],
                    low=c["low"],
                    close=c["close"],
                    volume=c["volume"],
                    close_time=c["close_time"],
                    quote_volume=c["quote_volume"],
                    trade_count=c["trade_count"],
                    taker_buy_base_volume=c["taker_buy_base_volume"],
                    taker_buy_quote_volume=c["taker_buy_quote_volume"],
                )
                for c in raw_candles
            ]
            ticker = event["ticker"]
            order_book = event["order_book"]

            if intelligence is None or event["type"] == "init" or bool(event.get("new_candle_closed")):
                intelligence = await fetch_market_intelligence_cached(
                    symbol, timeframe, settings, candle_limit=len(candles)
                )

            payload, last_ai_open_time = await run_full_analysis(
                symbol=symbol,
                timeframe=timeframe,
                candles=candles,
                ticker=ticker,
                order_book_raw=order_book,
                settings=settings,
                use_ai=use_ai,
                is_new_candle=bool(event.get("new_candle_closed")),
                is_init_event=event["type"] == "init",
                last_ai_open_time=last_ai_open_time,
                market_intelligence=intelligence,
                ai_override=ai_override,
                ai_cache_key=ai_cache_identity(user.id, ai_override),
                reconcile_signals=True,
                chart_mode=(
                    "snapshot"
                    if event["type"] == "init"
                    else "rollover"
                    if bool(event.get("new_candle_closed"))
                    else "delta"
                ),
            )
            payload["type"] = event["type"]
            await websocket.send_json(payload)

    except WebSocketDisconnect as exc:
        logger.info("WebSocket client disconnected.", extra={"close_code": exc.code})
    except Exception as e:
        logger.error(f"WebSocket execution error: {e}", exc_info=True)
        try:
            await websocket.send_json({"error": str(e)})
        except Exception:
            pass
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
        if research_lease is not None:
            await release_research_slot(research_lease.id)
