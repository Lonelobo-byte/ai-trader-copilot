"""Analysis endpoints – REST and WebSocket."""
from __future__ import annotations

import json
import logging
import asyncio
from contextlib import suppress
from typing import Any, AsyncGenerator

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError

from ..analysis_pipeline import run_full_analysis
from ..data_sources.binance_public import Candle
from ..data_sources.data_aggregator import fetch_market_intelligence_cached
from ..data_sources.binance_ws import SharedStreamCapacityError, get_shared_binance_stream_hub
from ..settings import get_settings
from ..auth import require_active_subscription, utcnow, websocket_subscription
from ..db.models import User
from ..user_ai import UserAIConnectionError, ai_cache_identity, resolve_user_ai_config
from ..rate_limit import enforce_rate_limit
from ..research_capacity import (
    RESEARCH_SLOT_HEARTBEAT_SECONDS,
    ResearchCapacityExceeded,
    ResearchEntitlementUnavailable,
    acquire_research_slot,
    heartbeat_research_slot,
    research_capacity_view,
    release_research_slot,
)

logger = logging.getLogger(__name__)

router = APIRouter()
_ANALYSIS_SEMAPHORE: asyncio.Semaphore | None = None
_ANALYSIS_SEMAPHORE_LIMIT = 0


class AnalysisCapacityBusy(RuntimeError):
    pass


def _analysis_semaphore() -> asyncio.Semaphore:
    global _ANALYSIS_SEMAPHORE, _ANALYSIS_SEMAPHORE_LIMIT
    limit = max(1, int(get_settings().analysis_compute_max_concurrency))
    if _ANALYSIS_SEMAPHORE is None or _ANALYSIS_SEMAPHORE_LIMIT != limit:
        _ANALYSIS_SEMAPHORE = asyncio.Semaphore(limit)
        _ANALYSIS_SEMAPHORE_LIMIT = limit
    return _ANALYSIS_SEMAPHORE


async def _run_analysis_bounded(**kwargs: Any):
    semaphore = _analysis_semaphore()
    try:
        await asyncio.wait_for(
            semaphore.acquire(),
            timeout=max(1.0, float(get_settings().analysis_compute_wait_seconds)),
        )
    except TimeoutError as exc:
        raise AnalysisCapacityBusy("Research computation is busy. Retry in a few seconds.") from exc
    try:
        return await run_full_analysis(**kwargs)
    finally:
        semaphore.release()


async def _reserved_rest_research_slot(
    request: Request,
    user: User = Depends(require_active_subscription),
) -> AsyncGenerator[User, None]:
    """Reserve capacity for the full lifetime of a one-shot research request."""
    enforce_rate_limit(request, "analysis", limit=12, window_seconds=60, identity=user.id)
    try:
        lease = await acquire_research_slot(user=user, symbol="REST", timeframe="ONESHOT", channel="rest")
    except ResearchEntitlementUnavailable as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ResearchCapacityExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": "20"},
        ) from exc
    heartbeat_task = asyncio.create_task(_maintain_rest_research_slot(lease.id, user.id))
    try:
        yield user
    finally:
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
        await release_research_slot(lease.id)


async def _maintain_rest_research_slot(lease_id: str, user_id: str) -> None:
    while True:
        await asyncio.sleep(RESEARCH_SLOT_HEARTBEAT_SECONDS)
        if not await heartbeat_research_slot(lease_id, user_id=user_id):
            return


async def _maintain_websocket_research_slot(
    websocket: WebSocket,
    lease_id: str,
    user: User,
    handler_task: asyncio.Task[Any] | None,
) -> None:
    while True:
        await asyncio.sleep(RESEARCH_SLOT_HEARTBEAT_SECONDS)
        token_expires_at = int(getattr(user, "_access_token_expires_at", 0) or 0)
        if token_expires_at and token_expires_at <= int(utcnow().timestamp()):
            await websocket.close(code=4401, reason="Access token expired")
            if handler_task is not None:
                handler_task.cancel()
            return
        if not await heartbeat_research_slot(lease_id, user_id=user.id):
            await websocket.close(code=4403, reason="Research entitlement is no longer active")
            if handler_task is not None:
                handler_task.cancel()
            return


class AnalyzeRequest(BaseModel):
    symbol: str = Field(default="BTCUSDT", min_length=3, max_length=20, pattern=r"^[A-Za-z0-9]+$")
    timeframe: str = Field(default="15m", pattern=r"^(1m|5m|15m|1h|4h|1d)$")
    use_ai: bool = True
    candle_limit: int = Field(default=200, ge=60, le=1000)


@router.get("/research/capacity")
async def research_capacity(user: User = Depends(require_active_subscription)):
    """Membership plan and active live-research leases for the current user."""
    return await research_capacity_view(user)

@router.post("/analyze")
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
    try:
        payload, _ = await _run_analysis_bounded(
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
    except AnalysisCapacityBusy as exc:
        raise HTTPException(status_code=503, detail=str(exc), headers={"Retry-After": "5"}) from exc
    return payload


@router.websocket("/ws/analyze")
async def websocket_analyze(websocket: WebSocket):
    user = await websocket_subscription(websocket)
    if user is None:
        return
    # Echo the non-secret protocol identifier selected by the browser.  The
    # JWT is the second requested subprotocol and is never reflected/logged.
    await websocket.accept(subprotocol="atc-auth")
    research_lease = None
    heartbeat_task = None
    try:
        settings = get_settings()
        config_msg = await asyncio.wait_for(
            websocket.receive_text(),
            timeout=max(1.0, float(settings.analysis_websocket_config_timeout_seconds)),
        )
        if len(config_msg.encode("utf-8")) > 4_096:
            await websocket.close(code=4400, reason="Research configuration is too large")
            return
        try:
            config = AnalyzeRequest.model_validate_json(config_msg)
        except ValidationError as exc:
            details = [
                {"field": ".".join(str(item) for item in error.get("loc", [])), "message": error.get("msg"), "type": error.get("type")}
                for error in exc.errors(include_url=False)
            ]
            await websocket.send_json({"error": "Invalid research configuration.", "code": "invalid_configuration", "details": details})
            await websocket.close(code=4400)
            return
        symbol = config.symbol.upper().strip()
        timeframe = config.timeframe
        use_ai = config.use_ai

        try:
            research_lease = await acquire_research_slot(
                user=user, symbol=symbol, timeframe=timeframe, channel="websocket"
            )
        except ResearchEntitlementUnavailable as exc:
            await websocket.send_json({"error": str(exc), "code": "subscription_required"})
            await websocket.close(code=4403, reason="Research entitlement is no longer active")
            return
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
        heartbeat_task = asyncio.create_task(
            _maintain_websocket_research_slot(
                websocket,
                research_lease.id,
                user,
                asyncio.current_task(),
            )
        )

        try:
            ai_override = await resolve_user_ai_config(user.id)
        except UserAIConnectionError as exc:
            await websocket.send_json({"error": str(exc), "code": "ai_connection_invalid"})
            await websocket.close(code=4400)
            return
        use_ai = bool(use_ai) and (ai_override is not None or settings.allow_platform_ai_fallback)
        stream_hub = get_shared_binance_stream_hub(settings)

        last_ai_open_time = 0
        # The subscriber supplies fresh price/book data.  Context data (higher
        # timeframes, derivatives, news, macro) is shared briefly across tabs
        # instead of triggering a full upstream fan-out on every websocket tick.
        intelligence = None

        async for event in stream_hub.events(symbol, timeframe):
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

            payload, last_ai_open_time = await _run_analysis_bounded(
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
    except (TimeoutError, json.JSONDecodeError, SharedStreamCapacityError, AnalysisCapacityBusy) as exc:
        logger.warning("WebSocket research request rejected: %s", exc)
        try:
            await websocket.send_json({"error": str(exc), "code": "research_temporarily_unavailable"})
            await websocket.close(code=1013, reason="Research service temporarily busy")
        except Exception:
            pass
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
