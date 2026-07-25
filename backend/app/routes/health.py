"""Health and AI status endpoints."""
from __future__ import annotations

from fastapi import APIRouter

from ..ai_client import ai_is_configured, get_model_for_task
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
