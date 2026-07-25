"""Analysis endpoints – REST and WebSocket."""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from ..analysis_pipeline import run_full_analysis
from ..data_sources.binance_public import BinancePublicClient, Candle
from ..data_sources.data_aggregator import fetch_market_intelligence
from ..data_sources.binance_ws import BinanceWSSubscriber
from ..settings import get_settings
from ..quant.research import list_hypotheses, validate_series
from ..auth import require_active_subscription, websocket_subscription

logger = logging.getLogger(__name__)

router = APIRouter()


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

@router.post("/analyze", dependencies=[Depends(require_active_subscription)])
async def analyze(request: AnalyzeRequest):
    settings = get_settings()
    symbol = request.symbol.upper().strip()
    client = BinancePublicClient(settings.binance_public_base_url)

    try:
        # Fetch one complete snapshot for the entire analysis. The snapshot
        # includes core data, higher timeframes, derivatives and context.
        intelligence = await fetch_market_intelligence(
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

    payload, _ = await run_full_analysis(
        symbol=symbol,
        timeframe=request.timeframe,
        candles=candles,
        ticker=ticker,
        order_book_raw=order_book_raw,
        settings=settings,
        use_ai=request.use_ai,
        market_intelligence=intelligence,
    )
    return payload


@router.websocket("/ws/analyze")
async def websocket_analyze(websocket: WebSocket):
    if await websocket_subscription(websocket) is None:
        return
    # Echo the non-secret protocol identifier selected by the browser.  The
    # JWT is the second requested subprotocol and is never reflected/logged.
    await websocket.accept(subprotocol="atc-auth")
    subscriber = None
    try:
        config_msg = await websocket.receive_text()
        config = json.loads(config_msg)
        symbol = config.get("symbol", "BTCUSDT").upper().strip()
        timeframe = config.get("timeframe", "15m")
        use_ai = config.get("use_ai", True)

        settings = get_settings()
        subscriber = BinanceWSSubscriber(symbol, timeframe, settings)

        last_ai_open_time = 0

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
