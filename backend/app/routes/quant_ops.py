"""Quantitative operations API router for Backtesting, Training, and Live Performance tracking."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from app.data_sources.binance_public import BinancePublicClient
from app.ml.model import train_walk_forward_model, get_weights_filepath
from app.quant.backtest import run_backtest
from app.quant.performance_tracker import get_historical_performance_summary
from app.settings import get_settings
from app.auth import require_admin
from app.db.models import User
from app.rate_limit import enforce_rate_limit
from fastapi import Request

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/quant", tags=["Quantitative Operations"])


class BacktestRequest(BaseModel):
    symbol: str = Field(default="BTCUSDT", min_length=5, max_length=20, pattern=r"^[A-Za-z0-9]+$")
    timeframe: str = Field(default="15m", pattern=r"^(1m|5m|15m|1h|4h|1d)$")
    candle_limit: int = Field(default=300, ge=60, le=500)


class TrainRequest(BaseModel):
    symbol: str = Field(default="BTCUSDT")
    timeframe: str = Field(default="15m")
    candle_limit: int = Field(default=500, ge=100, le=1000)
    train_fraction: float = Field(default=0.7, gt=0.5, lt=0.9)


@router.post("/backtest")
async def post_backtest(req: BacktestRequest, request: Request, _: User = Depends(require_admin)):
    """Replay historical candles through the analysis pipeline and calculate stats."""
    settings = get_settings()
    enforce_rate_limit(request, "backtest", limit=3, window_seconds=15 * 60)
    client = BinancePublicClient(settings.binance_public_base_url)

    try:
        # Fetch candles
        candles = await client.klines(req.symbol.upper().strip(), req.timeframe, req.candle_limit)
        report = await run_backtest(
            symbol=req.symbol.upper().strip(),
            timeframe=req.timeframe,
            candles=candles,
            settings=settings,
        )
        return report
    except Exception as exc:
        logger.error(f"Backtest execution failed: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="Backtest could not be completed. Check server logs for details.") from exc


@router.post("/train")
async def post_train_model(req: TrainRequest, request: Request, _: User = Depends(require_admin)):
    """Perform walk-forward model training on historical klines and update model weights."""
    settings = get_settings()
    enforce_rate_limit(request, "model_train", limit=2, window_seconds=60 * 60)
    client = BinancePublicClient(settings.binance_public_base_url)

    try:
        symbol = req.symbol.upper().strip()
        candles = await client.klines(symbol, req.timeframe, req.candle_limit)
        ticker = await client.ticker_24hr(symbol)
        order_book = await client.order_book(symbol, limit=100)

        weights = train_walk_forward_model(
            symbol=symbol,
            timeframe=req.timeframe,
            candles=candles,
            ticker=ticker,
            order_book=order_book,
            train_fraction=req.train_fraction,
        )
        weights_file = get_weights_filepath(symbol, req.timeframe)
        return {
            "status": "success",
            "message": f"Successfully trained walk-forward model for {symbol} on {req.timeframe}.",
            "test_ic": weights["test_ic"],
            "train_ic": weights["train_ic"],
            "model_path": str(weights_file),
        }
    except Exception as exc:
        logger.error(f"Model training failed: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="Model training could not be completed. Check server logs for details.") from exc


@router.get("/performance")
async def get_live_performance():
    """Return the authoritative TradeSignal performance ledger."""
    return await get_historical_performance_summary()
