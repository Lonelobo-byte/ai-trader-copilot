"""Health and AI status endpoints."""
from __future__ import annotations

from fastapi import APIRouter

from ..ai_client import ai_is_configured, get_model_for_task
from ..data_sources.multi_venue_ws import get_multi_venue_hub
from ..settings import get_settings

router = APIRouter()


@router.get("/health")
def health():
    settings = get_settings()
    return {
        "status": "ok",
        "mode": "institutional_research_only",
        "real_trading_enabled": False,
        "ai_provider": settings.ai_provider,
        "ai_configured": ai_is_configured(settings),
        "version": "0.6.0",
    }


@router.get("/health/market-data")
async def public_market_data_health():
    """Expose optional public-feed readiness without leaking credentials."""
    settings = get_settings()
    if not settings.multi_venue_ws_enabled:
        return {"enabled": False, "status": "DISABLED", "symbols": {}}
    hub = get_multi_venue_hub(settings)
    symbols = {}
    for symbol in list(hub.symbols):
        snapshot = hub.snapshot(symbol, register=False, touch=False)
        symbols[symbol] = {
            "status": snapshot.get("status", "UNAVAILABLE"),
            "fresh_venue_count": snapshot.get("fresh_venue_count", 0),
            "flow_venue_count": snapshot.get("flow_venue_count", 0),
            "flow_confirmed": bool(snapshot.get("flow_confirmed")),
            "venue_health": {
                venue: payload.get("health", "UNAVAILABLE")
                for venue, payload in (snapshot.get("venues") or {}).items()
            },
        }
    states = [item["status"] for item in symbols.values()]
    status = "HEALTHY" if states and all(item == "HEALTHY" for item in states) else (
        "DEGRADED" if any(item in {"HEALTHY", "DEGRADED"} for item in states) else "STARTING_OR_UNAVAILABLE"
    )
    return {
        "enabled": True,
        "status": status,
        "symbols": symbols,
        "subscribed_symbols": list(hub.symbols),
        "dynamic_subscription_capacity": {
            "active_symbols": len(hub.symbols),
            "maximum_symbols": hub.max_symbols,
        },
        "quarantined_subscriptions": hub.quarantined_subscriptions,
        "metrics": dict(hub.metrics),
    }


@router.get("/ai/status")
def ai_status():
    settings = get_settings()
    configured = ai_is_configured(settings)
    return {
        "provider": settings.ai_provider,
        "configured": configured,
        "scanner_model": get_model_for_task(settings, "scanner") if configured else None,
        "judge_model": get_model_for_task(settings, "judge") if configured else None,
        "max_calls_per_analysis": settings.ai_max_calls_per_analysis,
        "monthly_budget_usd": settings.ai_monthly_budget_usd,
    }
