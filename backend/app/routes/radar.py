"""Deterministic opportunity Radar routes.

Radar ranks liquid markets for review. It does not publish trades or use an
LLM to invent market structure, token health, or trap certainty.
"""
from __future__ import annotations

import asyncio
import copy
import logging
from time import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..quant.momentum_scanner import (
    calculate_ema,
    calculate_rsi,
    get_breakout_candidates,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/quant", tags=["radar"])

_RADAR_CACHE_TTL_SECONDS = 30.0
_RADAR_CACHE: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_RADAR_LOCK = asyncio.Lock()
_SUPPORTED_PAIRS = {("5m", "1h"), ("15m", "4h"), ("1h", "1d")}


@router.get("/breakout-radar", response_model=list[dict[str, Any]])
async def get_radar_breakouts(ltf: str = "5m", htf: str = "1h", use_ai: bool = False):
    """Return a cached deterministic ranking for one supported timeframe pair."""
    pair = (ltf, htf)
    if pair not in _SUPPORTED_PAIRS:
        raise HTTPException(
            status_code=422,
            detail="Use one of the supported Radar pairs: 5m/1h, 15m/4h, or 1h/1d.",
        )
    if use_ai:
        logger.info("Ignoring deprecated use_ai Radar parameter; deterministic ranking is always used.")

    now = time()
    cached = _RADAR_CACHE.get(pair)
    if cached and now - cached[0] < _RADAR_CACHE_TTL_SECONDS:
        return copy.deepcopy(cached[1])

    try:
        async with _RADAR_LOCK:
            cached = _RADAR_CACHE.get(pair)
            now = time()
            if cached and now - cached[0] < _RADAR_CACHE_TTL_SECONDS:
                return copy.deepcopy(cached[1])
            candidates = await get_breakout_candidates(ltf=ltf, htf=htf, use_ai=False)
            _RADAR_CACHE[pair] = (now, candidates)
            return copy.deepcopy(candidates)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Radar scan failed.")
        raise HTTPException(status_code=502, detail="Radar market-data scan failed. Please retry.") from exc


class VerifySetupRequest(BaseModel):
    symbol: str


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@router.post("/verify-setup")
async def verify_setup(payload: VerifySetupRequest):
    """Provide a source-backed manual-review checklist for one symbol.

    This replaces the former AI structure/token-health narrative. The result is
    explicitly an evidence score, not a probability of profit or trade order.
    """
    from ..data_sources.binance_public import BinancePublicClient
    from ..settings import get_settings

    settings = get_settings()
    symbol = payload.symbol.upper().strip()
    # Radar scans USDⓈ-M perpetuals, so review data must come from Futures as
    # well. Never silently switch a shared client between venues.
    client = BinancePublicClient(settings.binance_futures_base_url, market="futures")
    try:
        candles_ltf, candles_htf, depth = await asyncio.gather(
            client.klines(symbol, "5m", limit=100),
            client.klines(symbol, "1h", limit=100),
            client.order_book(symbol, limit=50),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Unable to fetch current Radar review data.") from exc

    now_ms = int(time() * 1000)
    candles_ltf = [c for c in candles_ltf if c.close_time <= now_ms]
    candles_htf = [c for c in candles_htf if c.close_time <= now_ms]
    if len(candles_ltf) < 55 or len(candles_htf) < 55:
        raise HTTPException(status_code=502, detail="Insufficient completed candles for deterministic Radar review.")

    ltf_close = [_number(c.close) for c in candles_ltf]
    htf_close = [_number(c.close) for c in candles_htf]
    ltf_high = [_number(c.high) for c in candles_ltf]
    ltf_low = [_number(c.low) for c in candles_ltf]
    ltf_volume = [_number(c.quote_volume) for c in candles_ltf]
    ltf_ema20, ltf_ema50 = calculate_ema(ltf_close, 20)[-1], calculate_ema(ltf_close, 50)[-1]
    htf_ema20, htf_ema50 = calculate_ema(htf_close, 20)[-1], calculate_ema(htf_close, 50)[-1]
    rsi = calculate_rsi(ltf_close, 14)[-1]
    ltf_direction = "BULLISH" if ltf_close[-1] >= ltf_ema20 and ltf_ema20 > ltf_ema50 else "BEARISH" if ltf_close[-1] <= ltf_ema20 and ltf_ema20 < ltf_ema50 else "NEUTRAL"
    htf_direction = "BULLISH" if htf_close[-1] >= htf_ema20 and htf_ema20 > htf_ema50 else "BEARISH" if htf_close[-1] <= htf_ema20 and htf_ema20 < htf_ema50 else "NEUTRAL"
    aligned = ltf_direction != "NEUTRAL" and ltf_direction == htf_direction
    average_volume = sum(ltf_volume[-21:-1]) / 20.0
    rvol = ltf_volume[-1] / average_volume if average_volume > 0 else 0.0

    bids = depth.get("bids", []) or []
    asks = depth.get("asks", []) or []
    best_bid = _number(bids[0][0]) if bids else 0.0
    best_ask = _number(asks[0][0]) if asks else 0.0
    mid = (best_bid + best_ask) / 2.0 if best_bid and best_ask else 0.0
    spread_pct = ((best_ask - best_bid) / mid * 100.0) if mid else 999.0
    bid_depth = sum(_number(p) * _number(q) for p, q in bids if _number(p) >= mid * 0.99)
    ask_depth = sum(_number(p) * _number(q) for p, q in asks if _number(p) <= mid * 1.01)
    liquid = spread_pct <= 0.15 and min(bid_depth, ask_depth) >= 15_000

    score = 20
    evidence: list[str] = ["Completed 5m and 1h candles were used; no AI narrative was used."]
    risk_flags: list[str] = []
    if aligned:
        score += 35
        evidence.append(f"5m and 1h trend align {ltf_direction.lower()}.")
    else:
        risk_flags.append(f"Timeframes conflict: 5m={ltf_direction.lower()}, 1h={htf_direction.lower()}.")
    momentum_aligned = (ltf_direction == "BULLISH" and 52 <= rsi <= 72) or (ltf_direction == "BEARISH" and 28 <= rsi <= 48)
    if momentum_aligned:
        score += 15
        evidence.append(f"RSI {rsi:.1f} supports the aligned direction without an extreme reading.")
    else:
        risk_flags.append(f"RSI {rsi:.1f} does not provide clean directional confirmation.")
    if rvol >= 1.2:
        score += 15
        evidence.append(f"Relative volume is {rvol:.2f}x the recent average.")
    else:
        risk_flags.append(f"Relative volume is only {rvol:.2f}x.")
    if liquid:
        score += 15
        evidence.append(f"Spread is {spread_pct:.4f}% with balanced 1% depth.")
    else:
        risk_flags.append("Spread or displayed order-book depth is insufficient for manual review.")

    verdict = "REVIEW_CANDIDATE" if score >= 70 and aligned and liquid else "WATCH_ONLY"
    direction = ltf_direction if aligned else "NEUTRAL"
    support, resistance = min(ltf_low[-20:]), max(ltf_high[-20:])
    invalidation = support if direction == "BULLISH" else resistance if direction == "BEARISH" else None
    return {
        "symbol": symbol,
        "verdict": verdict,
        "direction": direction,
        "evidence_score": score,
        # Kept for older dashboard clients; the label prevents it being read as a win probability.
        "confidence_pct": score,
        "confidence_label": "Deterministic evidence score, not a probability of profit.",
        "evaluation_mode": "deterministic_manual_review",
        "levels": {"key_resistance": resistance, "key_support": support, "invalidation_level": invalidation},
        "liquidity": {"spread_pct": round(spread_pct, 4), "depth_bids_1pct": round(bid_depth, 2), "depth_asks_1pct": round(ask_depth, 2)},
        "metrics": {"ltf_direction": ltf_direction, "htf_direction": htf_direction, "mtf_aligned": aligned, "rsi": round(rsi, 1), "rvol": round(rvol, 2)},
        "reasoning": evidence,
        "risk_flags": risk_flags,
        "trade_instruction": "Open Full Review before taking any trade; Radar does not authorize execution.",
    }
