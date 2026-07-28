"""Persistent guardrails for operator-funded AI calls."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select

from .db.database import AsyncSessionLocal
from .db.models import PlatformAIUsage
from .settings import Settings


class AIBudgetExceededError(RuntimeError):
    """Raised before an operator-funded call can exceed its monthly ceiling."""


_reservation_lock = asyncio.Lock()


def _period_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


async def reserve_platform_ai_budget(settings: Settings) -> None:
    """Reserve the full permitted attempt budget before a platform AI request.

    Model-specific live pricing is intentionally not guessed. Instead, the
    configured conservative per-call estimate is reserved persistently, which
    gives the operator a hard ceiling even when a provider call retries.
    """
    allowed_calls = max(0, int(settings.ai_max_calls_per_analysis))
    if allowed_calls <= 0:
        raise AIBudgetExceededError("Platform AI is disabled because AI_MAX_CALLS_PER_ANALYSIS is 0.")
    per_call = max(0.0, float(settings.ai_estimated_cost_per_call_usd))
    reservation = allowed_calls * per_call
    budget = max(0.0, float(settings.ai_monthly_budget_usd))
    period = _period_key()

    # Docker runs a single app process by default. The row lock also protects
    # production deployments that add workers against a same-month overspend.
    async with _reservation_lock:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                row = await session.get(PlatformAIUsage, period, with_for_update=True)
                if row is None:
                    if reservation > budget:
                        raise AIBudgetExceededError("Platform AI monthly budget would be exceeded by this analysis.")
                    row = PlatformAIUsage(
                        period_key=period,
                        reserved_calls=allowed_calls,
                        reserved_cost_usd=reservation,
                    )
                    session.add(row)
                    return
                if float(row.reserved_cost_usd) + reservation > budget + 1e-9:
                    raise AIBudgetExceededError("Platform AI monthly budget has been reached. Add your own OpenRouter key or try next month.")
                row.reserved_calls += allowed_calls
                row.reserved_cost_usd = float(row.reserved_cost_usd) + reservation


async def platform_ai_usage(settings: Settings) -> dict[str, float | int | str]:
    """Return non-sensitive operator usage telemetry for health/admin views."""
    period = _period_key()
    async with AsyncSessionLocal() as session:
        row = await session.get(PlatformAIUsage, period)
    return {
        "period": period,
        "reserved_calls": int(row.reserved_calls) if row else 0,
        "reserved_cost_usd": round(float(row.reserved_cost_usd), 4) if row else 0.0,
        "budget_usd": max(0.0, float(settings.ai_monthly_budget_usd)),
    }
