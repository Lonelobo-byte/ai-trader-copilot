"""Causal market-discovery Radar routes.

Radar ranks liquid contracts for manual research.  It never produces a trade
order or treats a price-derived indicator as the cause of a move.
"""
from __future__ import annotations

import asyncio
import logging
from time import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from ..data_sources.execution_tape_ws import get_execution_tape_snapshot

from ..data_sources.binance_public import Candle
from ..auth import require_active_subscription, utcnow
from ..db.models import User
from ..indicators.market_story import (
    build_market_story,
    evaluate_story_playbook,
    observable_liquidity_sweep,
    observable_structure_events,
)
from ..indicators.structure import classify_market_phase
from ..quant.market_context import (
    build_liquidity_map,
    build_volume_profile,
    build_volatility_context,
    build_vwap_context,
    score_market_context,
)
from ..rate_limit import enforce_rate_limit
from ..radar_service import SUPPORTED_PAIRS, read_radar_pair

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/quant", tags=["radar"])

@router.get("/breakout-radar", response_model=list[dict[str, Any]])
async def get_radar_breakouts(request: Request, response: Response = None, ltf: str = "5m", htf: str = "1h", use_ai: bool = False):
    """Return the shared demand-aware causal Radar snapshot for a pair."""
    enforce_rate_limit(request, "public_radar", limit=20, window_seconds=60)
    pair = (ltf, htf)
    if pair not in SUPPORTED_PAIRS:
        raise HTTPException(status_code=422, detail="Use one of the supported Radar pairs: 5m/1h, 15m/4h, or 1h/1d.")
    if use_ai:
        logger.info("Ignoring deprecated use_ai Radar parameter; causal deterministic ranking is always used.")

    try:
        shared = await read_radar_pair(ltf, htf)
        if response is not None:
            response.headers["X-Radar-Snapshot-State"] = shared.state
            response.headers["X-Radar-Server-Time"] = utcnow().isoformat()
            # Radar is shared, but it is still live market data. Never let a
            # browser/proxy replay an older HTTP response and desynchronise
            # its countdown from the current shared snapshot.
            response.headers["Cache-Control"] = "no-store, max-age=0"
        if response is not None and shared.captured_at:
            response.headers["X-Radar-Snapshot-At"] = shared.captured_at
        if response is not None and shared.next_refresh_at:
            response.headers["X-Radar-Next-Refresh-At"] = shared.next_refresh_at
        return shared.candidates
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Radar scan failed.")
        raise HTTPException(status_code=503, detail="Shared Radar snapshot is temporarily unavailable. Please retry shortly.") from exc


class VerifySetupRequest(BaseModel):
    symbol: str = Field(min_length=5, max_length=20, pattern=r"^[A-Za-z0-9]+$")
    ltf: str = Field(default="5m", pattern=r"^(5m|15m|1h)$")
    htf: str = Field(default="1h", pattern=r"^(1h|4h|1d)$")


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _depth_snapshot(depth: dict[str, Any]) -> dict[str, Any]:
    bids, asks = depth.get("bids", []) or [], depth.get("asks", []) or []
    best_bid = _number(bids[0][0]) if bids else 0.0
    best_ask = _number(asks[0][0]) if asks else 0.0
    midpoint = (best_bid + best_ask) / 2.0 if best_bid and best_ask else 0.0
    bid_notional = sum(_number(price) * _number(size) for price, size in bids[:20])
    ask_notional = sum(_number(price) * _number(size) for price, size in asks[:20])
    total = bid_notional + ask_notional
    return {
        "available": bool(bids and asks),
        "depth_imbalance": round((bid_notional - ask_notional) / total, 4) if total else 0.0,
        "spread_pct": round((best_ask - best_bid) / midpoint * 100.0, 5) if midpoint else None,
        "bid_depth_notional": round(bid_notional, 2),
        "ask_depth_notional": round(ask_notional, 2),
    }


def _observable_structure_events(candles: list[Candle]) -> dict[str, dict[str, Any]]:
    """Compatibility projection from the canonical completed-candle story."""
    return observable_structure_events(build_market_story(candles))


@router.post("/verify-setup")
async def verify_setup(payload: VerifySetupRequest, request: Request, user: User = Depends(require_active_subscription)):
    """Return an auditable causal research brief for one contract.

    The endpoint deliberately reports missing positioning/order-flow domains
    rather than filling them with a synthetic verdict.
    """
    from ..data_sources.binance_public import BinancePublicClient
    from ..settings import get_settings

    enforce_rate_limit(request, f"radar_research:{user.id}", limit=12, window_seconds=60)
    settings = get_settings()
    symbol = payload.symbol.upper().strip()
    pair = (payload.ltf, payload.htf)
    if pair not in SUPPORTED_PAIRS:
        raise HTTPException(status_code=422, detail="Use one of the supported Radar pairs: 5m/1h, 15m/4h, or 1h/1d.")
    client = BinancePublicClient(settings.binance_futures_base_url, market="futures")
    try:
        candles_ltf, candles_htf, depth = await asyncio.gather(
            client.klines(symbol, payload.ltf, limit=200),
            client.klines(symbol, payload.htf, limit=200),
            client.order_book(symbol, limit=50),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Unable to fetch current Radar review data.") from exc

    now_ms = int(time() * 1000)
    candles_ltf = [candle for candle in candles_ltf if candle.close_time <= now_ms]
    candles_htf = [candle for candle in candles_htf if candle.close_time <= now_ms]
    if len(candles_ltf) < 55 or len(candles_htf) < 55:
        raise HTTPException(status_code=502, detail="Insufficient completed candles for causal Radar review.")

    average_range = sum(max(0.0, candle.high - candle.low) for candle in candles_ltf[-14:]) / 14.0
    liquidity = build_liquidity_map(candles_ltf, average_range)
    microstructure = _depth_snapshot(depth)
    story = build_market_story(candles_ltf)
    higher_story = build_market_story(candles_htf)
    events = observable_structure_events(story)
    sweep = observable_liquidity_sweep(story)
    structure = {
        "phase": classify_market_phase(candles_ltf),
        "higher_timeframe_phase": classify_market_phase(candles_htf),
        "bos": events["bos"],
        "choch": events["choch"],
        "liquidity_sweep": sweep,
        "story_state": story.get("current_state"),
        "story_actionability": story.get("actionability", {}),
        "story_as_of_close_time": story.get("as_of_close_time"),
    }
    execution_tape = get_execution_tape_snapshot(symbol, settings)
    actual_flow = execution_tape.get("actual_flow", {}) or {}
    flow_confirmed = bool(actual_flow.get("available"))
    trade_flow = {
        "available": flow_confirmed,
        "buy_ratio": actual_flow.get("aggressive_buy_ratio"),
        "bias": actual_flow.get("bias", "UNAVAILABLE"),
        "status": actual_flow.get("status", "UNAVAILABLE"),
        "active_aggressor": actual_flow.get("active_aggressor", "UNAVAILABLE"),
        "net_delta_usd": actual_flow.get("net_delta_usd"),
        "cvd_trend": actual_flow.get("cvd_trend", "UNAVAILABLE"),
        "price_response": actual_flow.get("price_response", "UNAVAILABLE"),
        "absorption": actual_flow.get("absorption", "NOT_DETECTED"),
        "exhaustion": actual_flow.get("exhaustion", "NONE"),
        "source": "binance_bybit_spot_perpetual_public_tape" if flow_confirmed else None,
    }

    features = {
        "market_structure": structure,
        "market_story": story,
        "liquidity_map": liquidity,
        "sweep": sweep,
        "microstructure": microstructure,
        "trade_flow": trade_flow,
        "execution_tape": execution_tape,
        "positioning": {"available": False, "state": "UNKNOWN"},
        "volatility_context": build_volatility_context(candles_ltf),
        "volume_profile": build_volume_profile(candles_ltf),
        "vwap_context": build_vwap_context(candles_ltf),
    }
    context = score_market_context(features)
    direction = context["direction"]
    normalized_direction = {"LONG": "BULLISH", "SHORT": "BEARISH"}.get(direction, "NEUTRAL")
    structure_confirmation = evaluate_story_playbook(
        primary_story=story,
        higher_story=higher_story,
        direction=normalized_direction,
        primary_phase=structure["phase"],
        higher_phase=structure["higher_timeframe_phase"],
        vwap_context=features["vwap_context"],
        volume_profile=features["volume_profile"],
    )
    target_pool = liquidity.get("nearest_above" if direction == "LONG" else "nearest_below") if direction != "WAIT" else None
    verdict = (
        "REVIEW_CANDIDATE"
        if context["status"] == "SETUP_CANDIDATE" and structure_confirmation["passed"]
        else "WATCH_ONLY"
    )
    limitations = list(context.get("limitations", []))
    limitations.append("Price×OI, funding and aggressive taker flow require the Radar live-confirmation snapshot; they are unavailable in this single-symbol review request.")
    if flow_confirmed:
        limitations[-1] = (
            "Price x OI and funding require the Radar live-confirmation snapshot; "
            "normalized Binance/Bybit aggressor flow is included."
        )
    else:
        limitations[-1] = (
            "Price x OI, funding and the live Binance/Bybit execution tape are "
            "unavailable and are not treated as neutral evidence."
        )

    return {
        "symbol": symbol,
        "verdict": verdict,
        "direction": direction,
        "evidence_score": context["score"],
        "confidence_pct": context["score"],
        "confidence_label": "Causal evidence score, not a probability of profit.",
        "evaluation_mode": "causal_manual_review",
        "market_context": context,
        "execution_tape": execution_tape,
        "liquidity_map": liquidity,
        "positioning": features["positioning"],
        "volatility_context": features["volatility_context"],
        "volume_profile": features["volume_profile"],
        "vwap_context": features["vwap_context"],
        "market_structure": structure,
        "market_story": story,
        "higher_timeframe_story": higher_story,
        "structure_confirmation": structure_confirmation,
        "liquidity": microstructure,
        "target_pool": target_pool,
        "levels": {
            "key_resistance": (liquidity.get("nearest_above") or {}).get("price"),
            "key_support": (liquidity.get("nearest_below") or {}).get("price"),
                "invalidation_level": ((liquidity.get("nearest_below") or {}) if direction == "LONG" else (liquidity.get("nearest_above") or {}) if direction == "SHORT" else {}).get("price"),
        },
        "reasoning": [
            f"Regime: {structure['phase'].replace('_', ' ').lower()}.",
            story.get("what_happened", "No recent completed-candle event was confirmed."),
            story.get("what_is_happening", "Current event lifecycle is unavailable."),
            f"Volatility: {features['volatility_context'].get('state', 'unknown').replace('_', ' ').lower()}.",
            f"VWAP relation: {features['vwap_context'].get('price_relation', 'unknown').replace('_', ' ').lower()}.",
        ],
        "risk_flags": list(dict.fromkeys([
            *context.get("contradictions", []),
            *([] if structure_confirmation["passed"] else [structure_confirmation["reason"]]),
        ])),
        "limitations": limitations,
        "trade_instruction": "Open the dashboard dossier before taking any trade; Radar does not authorize execution.",
    }
