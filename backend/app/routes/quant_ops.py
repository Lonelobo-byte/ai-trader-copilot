"""Quantitative operations API router for Backtesting, Training, and Live Performance tracking."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.data_sources.binance_public import BinancePublicClient
from app.db.database import AsyncSessionLocal
from app.db.models import AnalysisSession
from app.ml.model import train_walk_forward_model, get_weights_filepath
from app.quant.backtest import run_backtest
from app.settings import get_settings
from app.auth import require_admin
from app.db.models import User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/quant", tags=["Quantitative Operations"])


class BacktestRequest(BaseModel):
    symbol: str = Field(default="BTCUSDT")
    timeframe: str = Field(default="15m")
    candle_limit: int = Field(default=300, ge=60, le=1000)


class TrainRequest(BaseModel):
    symbol: str = Field(default="BTCUSDT")
    timeframe: str = Field(default="15m")
    candle_limit: int = Field(default=500, ge=100, le=1000)
    train_fraction: float = Field(default=0.7, gt=0.5, lt=0.9)


@router.post("/backtest")
async def post_backtest(req: BacktestRequest):
    """Replay historical candles through the analysis pipeline and calculate stats."""
    settings = get_settings()
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
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/train")
async def post_train_model(req: TrainRequest, _: User = Depends(require_admin)):
    """Perform walk-forward model training on historical klines and update model weights."""
    settings = get_settings()
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
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/performance")
async def get_live_performance():
    """Compile win-rate, total trades, and profit/loss metrics from completed SQLite sessions."""
    async with AsyncSessionLocal() as session:
        stmt = select(AnalysisSession).where(AnalysisSession.outcome.in_(["SUCCESS", "FAILURE"]))
        res = await session.execute(stmt)
        records = res.scalars().all()

    if not records:
        return {
            "status": "insufficient_data",
            "total_trades": 0,
            "win_rate": 0.0,
            "expectancy": 0.0,
            "message": "No live outcomes recorded yet.",
        }

    total = len(records)
    wins = sum(1 for r in records if r.outcome == "SUCCESS")
    win_rate = wins / total

    # Compute a simple expectancy metric based on the risk reward of the setups
    returns = []
    for r in records:
        if r.outcome == "SUCCESS":
            # Target hit yields +1.0 or R multiple
            returns.append(r.rr if r.rr is not None else 1.5)
        else:
            returns.append(-1.0)

    avg_return = sum(returns) / total
    import numpy as np
    std_return = float(np.std(returns)) if len(returns) > 1 else 0.0
    sharpe = (avg_return / std_return) * 15.87 if std_return > 0 else 0.0 # sqrt(252) proxy

    return {
        "status": "active",
        "total_trades": total,
        "win_rate": round(win_rate, 4),
        "expectancy": round(avg_return, 4),
        "sharpe_ratio": round(sharpe, 4),
        "wins": wins,
        "losses": total - wins,
    }
