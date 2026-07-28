from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request

from app.ai_budget import AIBudgetExceededError, reserve_platform_ai_budget
from app.auth import require_admin
from app.autonomous_scanner import get_scanner_configuration, update_scanner_configuration
from app.billing import PLANS
from app.db.models import User
from app.routes.radar import get_radar_breakouts
from app.radar_service import read_radar_pair
from app.settings import get_settings


@pytest.mark.asyncio
async def test_platform_ai_budget_reserves_persistently_and_blocks_at_limit() -> None:
    settings = get_settings()
    original = settings.ai_max_calls_per_analysis, settings.ai_monthly_budget_usd, settings.ai_estimated_cost_per_call_usd
    try:
        settings.ai_max_calls_per_analysis = 1
        settings.ai_monthly_budget_usd = 0.01
        settings.ai_estimated_cost_per_call_usd = 0.01
        await reserve_platform_ai_budget(settings)
        with pytest.raises(AIBudgetExceededError):
            await reserve_platform_ai_budget(settings)
    finally:
        settings.ai_max_calls_per_analysis, settings.ai_monthly_budget_usd, settings.ai_estimated_cost_per_call_usd = original


@pytest.mark.asyncio
async def test_scanner_configuration_survives_process_state_changes() -> None:
    settings = get_settings()
    original = settings.autonomous_scan_enabled, settings.autonomous_pair_discovery, list(settings.watchlist)
    try:
        settings.autonomous_scan_enabled = False
        settings.autonomous_pair_discovery = False
        settings.watchlist = ["BTCUSDT"]
        await get_scanner_configuration(settings)
        updated = await update_scanner_configuration(enabled=True, discovery=True, watchlist=["ETHUSDT", "SOLUSDT"])
        loaded = await get_scanner_configuration(settings)
        assert updated["enabled"] is True
        assert loaded == updated
    finally:
        settings.autonomous_scan_enabled, settings.autonomous_pair_discovery, settings.watchlist = original


@pytest.mark.asyncio
async def test_platform_mutations_require_an_admin_role() -> None:
    member = User(id="member", email="member@example.com", password_hash="x", role="member", is_active=True)
    admin = User(id="admin", email="admin@example.com", password_hash="x", role="admin", is_active=True)
    with pytest.raises(HTTPException) as exc:
        await require_admin(member)
    assert exc.value.status_code == 403
    assert await require_admin(admin) is admin


@pytest.mark.asyncio
async def test_radar_discovery_is_public_without_a_subscription_dependency() -> None:
    request = Request({"type": "http", "method": "GET", "path": "/quant/breakout-radar", "headers": [], "client": ("127.0.0.1", 9000)})
    expected = [{"symbol": "BTCUSDT", "score": 77}]
    response = Response()
    with patch("app.radar_service.get_breakout_candidates", return_value=expected):
        result = await get_radar_breakouts(request, response=response, ltf="5m", htf="1h")
    assert result == expected
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.headers["x-radar-next-refresh-at"]
    assert response.headers["x-radar-server-time"]


@pytest.mark.asyncio
async def test_radar_snapshot_is_shared_between_visitors() -> None:
    """A second visitor reads the durable shared snapshot without a new scan."""
    expected = [{"symbol": "BTCUSDT", "score": 77}]
    with patch("app.radar_service.get_breakout_candidates", new=AsyncMock(return_value=expected)) as scan:
        first = await read_radar_pair("1h", "1d")
        second = await read_radar_pair("1h", "1d")
    assert first.candidates == expected
    assert second.candidates == expected
    assert first.next_refresh_at == second.next_refresh_at
    assert scan.await_count == 1


def test_new_plan_prices_and_launch_savings_are_exposed_from_one_source() -> None:
    assert [PLANS[code]["amount"] for code in ("monthly", "quarterly", "half_yearly", "annual")] == [5.99, 15.99, 29.99, 55.99]
    assert PLANS["quarterly"]["list_amount"] > PLANS["quarterly"]["amount"]
    assert PLANS["annual"]["list_amount"] > PLANS["annual"]["amount"]
