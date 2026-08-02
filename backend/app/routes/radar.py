"""Public causal Radar and synchronized premium research routes."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..auth import require_active_subscription, utcnow
from ..db.models import User
from ..rate_limit import enforce_rate_limit
from ..radar_service import SUPPORTED_PAIRS, read_radar_pair
# Compatibility export for the invariant test: all surfaces derive structure
# events from this one shared implementation, even though Research now reads
# the already-computed Radar row instead of rebuilding it.
from ..quant.live_confirmation import _observable_structure_events

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/quant", tags=["radar"])


def _selected(source: dict[str, Any], *keys: str) -> dict[str, Any]:
    """Copy only fields required by the public Radar flight deck."""
    return {key: source[key] for key in keys if key in source}


def _compact_event(value: Any) -> dict[str, Any]:
    event = value if isinstance(value, dict) else {}
    return _selected(
        event,
        "detected",
        "event_id",
        "type",
        "direction",
        "age_bars",
        "break_level",
        "broken_level",
        "swept_level",
        "invalidation_level",
        "state",
    )


def _compact_actionability(value: Any) -> dict[str, Any]:
    actionability = value if isinstance(value, dict) else {}
    result = _selected(
        actionability,
        "status",
        "state",
        "direction",
        "actionable",
        "chase_prohibited",
        "reason",
        "event_type",
        "event_id",
        "event_age_bars",
        "event_level",
    )
    for key in ("aligned_event", "selected_event"):
        if key in actionability:
            result[key] = _compact_event(actionability.get(key))
    return result


def _radar_card_row(row: dict[str, Any]) -> dict[str, Any]:
    """Project a persisted dossier into the compact public discovery contract.

    Full evidence remains in PostgreSQL and is returned only by the protected
    ``verify-setup`` route.  Sending it for all 40 candidates made every public
    refresh transfer and parse nearly two megabytes of JSON.
    """
    result = _selected(
        row,
        "symbol",
        "score",
        "review_status",
        "status",
        "direction",
        "close_price",
        "price_change_pct",
        "atr_ratio",
        "volume_usdt",
        "quality_badge",
    )

    context = row.get("market_context") if isinstance(row.get("market_context"), dict) else {}
    context_result = _selected(context, "status", "direction", "score")
    if "actionability" in context:
        context_result["actionability"] = _compact_actionability(context.get("actionability"))
    order_flow = ((context.get("components") or {}).get("order_flow") or {})
    if order_flow:
        context_result["components"] = {
            "order_flow": _selected(order_flow, "available", "status", "bias")
        }
    if context_result:
        result["market_context"] = context_result

    story = row.get("market_story") if isinstance(row.get("market_story"), dict) else {}
    story_result = _selected(
        story,
        "available",
        "current_state",
        "what_happened",
        "what_is_happening",
    )
    if "latest_event" in story:
        story_result["latest_event"] = _compact_event(story.get("latest_event"))
    if "actionability" in story:
        story_result["actionability"] = _compact_actionability(story.get("actionability"))
    scenarios = story.get("next_scenarios")
    if isinstance(scenarios, list):
        story_result["next_scenarios"] = [
            _selected(item, "scenario", "condition")
            for item in scenarios
            if isinstance(item, dict)
        ]
    if story_result:
        result["market_story"] = story_result

    structure = row.get("market_structure") if isinstance(row.get("market_structure"), dict) else {}
    if structure:
        result["market_structure"] = _selected(structure, "phase")
    positioning = row.get("positioning") if isinstance(row.get("positioning"), dict) else {}
    if positioning:
        result["positioning"] = _selected(positioning, "available", "state", "oi_change_pct")
    volatility = row.get("volatility_context") if isinstance(row.get("volatility_context"), dict) else {}
    if volatility:
        result["volatility_context"] = _selected(volatility, "available", "state")
    coverage = row.get("coverage") or context.get("coverage")
    if isinstance(coverage, dict):
        result["coverage"] = _selected(coverage, "available_domains", "required_domains", "coverage_ratio")
    target_pool = row.get("target_pool")
    if isinstance(target_pool, dict):
        result["target_pool"] = _selected(target_pool, "kind", "price", "side")

    advanced = row.get("advanced_confirmation") if isinstance(row.get("advanced_confirmation"), dict) else {}
    advanced_result = _selected(advanced, "state")
    actual_flow = advanced.get("actual_flow_evidence")
    if isinstance(actual_flow, dict):
        advanced_result["actual_flow_evidence"] = _selected(
            actual_flow,
            "available",
            "status",
            "active_aggressor",
            "bias",
            "net_delta_usd",
            "confidence",
        )
    live_location = advanced.get("market_story_live_location")
    if isinstance(live_location, dict):
        advanced_result["market_story_live_location"] = _selected(
            live_location, "evaluated", "passed", "distance_atr"
        )
    if advanced_result:
        result["advanced_confirmation"] = advanced_result

    for key in ("contradictions", "risk_flags"):
        values = row.get(key)
        if isinstance(values, list) and values:
            result[key] = values
    return result


@router.get("/breakout-radar", response_model=list[dict[str, Any]])
async def get_radar_breakouts(
    request: Request,
    response: Response,
    ltf: str = "5m",
    htf: str = "1h",
    use_ai: bool = False,
):
    """Return the shared demand-aware causal Radar snapshot for a pair."""
    # Radar reads one shared persisted snapshot, so a generous read ceiling is
    # safer for offices/mobile carriers whose users share one public NAT IP.
    enforce_rate_limit(request, "public_radar", limit=300, window_seconds=60)
    pair = (ltf, htf)
    if pair not in SUPPORTED_PAIRS:
        raise HTTPException(
            status_code=422,
            detail="Use one of the supported Radar pairs: 5m/1h, 15m/4h, or 1h/1d.",
        )
    if use_ai:
        logger.info("Ignoring deprecated use_ai Radar parameter; causal deterministic ranking is always used.")

    try:
        shared = await read_radar_pair(ltf, htf)
        response.headers["X-Radar-Snapshot-State"] = shared.state
        response.headers["X-Radar-Server-Time"] = utcnow().isoformat()
        response.headers["Cache-Control"] = "no-store, max-age=0"
        if shared.captured_at:
            response.headers["X-Radar-Snapshot-At"] = shared.captured_at
        if shared.next_refresh_at:
            response.headers["X-Radar-Next-Refresh-At"] = shared.next_refresh_at
        response.headers["X-Radar-Payload-Mode"] = "cards-v1"
        return [_radar_card_row(row) for row in shared.candidates]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Radar scan failed.")
        raise HTTPException(
            status_code=503,
            detail="Shared Radar snapshot is temporarily unavailable. Please retry shortly.",
        ) from exc


class VerifySetupRequest(BaseModel):
    symbol: str = Field(min_length=5, max_length=20, pattern=r"^[A-Za-z0-9]+$")
    ltf: str = Field(default="5m", pattern=r"^(5m|15m|1h)$")
    htf: str = Field(default="1h", pattern=r"^(1h|4h|1d)$")


def _number(value: Any, default: float | None = 0.0) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _synchronized_research_view(row: dict[str, Any], *, ltf: str, htf: str) -> dict[str, Any]:
    """Project the exact Radar row into the premium research-modal contract."""
    context = row.get("market_context") or {}
    story = row.get("market_story") or {}
    structure = row.get("market_structure") or {}
    advanced = row.get("advanced_confirmation") or {}
    actual_flow = advanced.get("actual_flow_evidence") or {}
    metrics = advanced
    coverage = row.get("coverage") or context.get("coverage") or {}
    contradictions = list(dict.fromkeys([
        *(row.get("contradictions") or []),
        *(row.get("risk_flags") or []),
    ]))
    spread_bps = _number(metrics.get("spread_bps"), None)
    liquidity = {
        "available": bool((advanced.get("checks") or {}).get("data_complete")),
        "depth_imbalance": metrics.get("depth_imbalance"),
        "spread_bps": spread_bps,
        "spread_pct": spread_bps / 100.0 if spread_bps is not None else None,
        "bid_depth_notional": metrics.get("bid_depth_notional"),
        "ask_depth_notional": metrics.get("ask_depth_notional"),
    }
    limitations = list(context.get("limitations") or [])
    if not actual_flow.get("available"):
        limitations.append("The shared Binance/Bybit execution tape is not qualified for this symbol yet.")
    if not (row.get("positioning") or {}).get("available"):
        limitations.append("Price and open-interest positioning is not currently qualified.")

    return {
        "symbol": row.get("symbol"),
        "verdict": row.get("review_status", "WATCH_ONLY"),
        "direction": row.get("direction", "NEUTRAL"),
        "evidence_score": int(round(_number(row.get("score"), 0.0) or 0.0)),
        "confidence_pct": int(round(_number(row.get("score"), 0.0) or 0.0)),
        "confidence_label": "Causal evidence score, not a probability of profit.",
        "evaluation_mode": "shared_causal_radar_snapshot",
        "timeframes": {"primary": ltf, "higher": htf},
        "market_context": context,
        "execution_tape": {
            "available": bool(actual_flow.get("available")),
            "actual_flow": actual_flow,
            "source": "shared_radar_live_confirmation",
        },
        "live_confirmation": advanced,
        "publication_coverage": row.get("publication_coverage") or {},
        "liquidity_map": row.get("liquidity_map") or {},
        "positioning": row.get("positioning") or {},
        "volatility_context": row.get("volatility_context") or {},
        "volume_profile": row.get("volume_profile") or {},
        "vwap_context": row.get("vwap_context") or {},
        "market_structure": structure,
        "market_story": story,
        "higher_timeframe_story": row.get("higher_timeframe_story") or {},
        "structure_confirmation": row.get("structure_confirmation") or {},
        "liquidity": liquidity,
        "target_pool": row.get("target_pool"),
        "levels": {
            "key_resistance": ((row.get("liquidity_map") or {}).get("nearest_above") or {}).get("price"),
            "key_support": ((row.get("liquidity_map") or {}).get("nearest_below") or {}).get("price"),
            "invalidation_level": (
                ((context.get("actionability") or {}).get("selected_event") or {}).get("invalidation_level")
                or ((story.get("latest_event") or {}).get("invalidation_level"))
            ),
        },
        "reasoning": [
            f"Regime: {str(structure.get('phase', 'unknown')).replace('_', ' ').lower()}.",
            story.get("what_happened", "No recent completed-candle event was confirmed."),
            story.get("what_is_happening", "Current event lifecycle is unavailable."),
            f"Actual flow: {str(actual_flow.get('status', 'unavailable')).replace('_', ' ').lower()}.",
        ],
        "risk_flags": contradictions,
        "limitations": list(dict.fromkeys(limitations)),
        "trade_instruction": "Open the dashboard dossier before taking any trade; Radar does not authorize execution.",
    }


@router.post("/verify-setup")
async def verify_setup(
    payload: VerifySetupRequest,
    request: Request,
    user: User = Depends(require_active_subscription),
):
    """Return a detail view built from the same shared row shown by Radar."""
    enforce_rate_limit(request, f"radar_research:{user.id}", limit=12, window_seconds=60)
    pair = (payload.ltf, payload.htf)
    if pair not in SUPPORTED_PAIRS:
        raise HTTPException(
            status_code=422,
            detail="Use one of the supported Radar pairs: 5m/1h, 15m/4h, or 1h/1d.",
        )
    shared = await read_radar_pair(payload.ltf, payload.htf)
    if shared.state != "FRESH":
        raise HTTPException(
            status_code=409,
            detail="Radar is refreshing this timeframe pair. Retry when the synchronized snapshot is current.",
        )
    symbol = payload.symbol.upper().strip()
    row = next((item for item in shared.candidates if str(item.get("symbol", "")).upper() == symbol), None)
    if row is None:
        raise HTTPException(status_code=404, detail="This symbol is not present in the current shared Radar snapshot.")
    return _synchronized_research_view(row, ltf=payload.ltf, htf=payload.htf)
