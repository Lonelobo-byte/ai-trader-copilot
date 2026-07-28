"""Server-enforced subscription research-capacity tests."""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.auth import utcnow
from app.db.database import AsyncSessionLocal
from app.db.models import Subscription, User
from app.research_capacity import ResearchCapacityExceeded, acquire_research_slot, release_research_slot, research_capacity_view


async def _active_user(plan_code: str) -> User:
    user = User(id=f"user-{plan_code}", email=f"{plan_code}@example.com", password_hash="test", role="member", is_active=True)
    subscription = Subscription(
        id=f"sub-{plan_code}", user_id=user.id, plan_code=plan_code, status="active",
        ends_at=utcnow() + timedelta(days=30),
    )
    async with AsyncSessionLocal() as session:
        session.add_all([user, subscription])
        await session.commit()
    return user


@pytest.mark.asyncio
@pytest.mark.parametrize(("plan_code", "limit"), [("monthly", 1), ("quarterly", 2), ("half_yearly", 4), ("annual", 4)])
async def test_subscription_plan_limits_concurrent_research_slots(plan_code: str, limit: int) -> None:
    user = await _active_user(plan_code)
    leases = []
    try:
        for index in range(limit):
            lease = await acquire_research_slot(user=user, symbol=f"TOKEN{index}USDT", timeframe="15m", channel="websocket")
            leases.append(lease)
            assert lease.limit == limit
            assert lease.active_slots == index + 1
        with pytest.raises(ResearchCapacityExceeded, match="concurrent live research"):
            await acquire_research_slot(user=user, symbol="EXTRAUSDT", timeframe="15m", channel="websocket")
    finally:
        for lease in leases:
            await release_research_slot(lease.id)


@pytest.mark.asyncio
async def test_releasing_a_research_slot_makes_capacity_available_immediately() -> None:
    user = await _active_user("monthly")
    first = await acquire_research_slot(user=user, symbol="BTCUSDT", timeframe="15m", channel="websocket")
    await release_research_slot(first.id)
    second = await acquire_research_slot(user=user, symbol="ETHUSDT", timeframe="1h", channel="websocket")
    try:
        assert second.active_slots == 1
    finally:
        await release_research_slot(second.id)


@pytest.mark.asyncio
async def test_capacity_view_exposes_only_current_users_plan_and_active_slots() -> None:
    user = await _active_user("quarterly")
    lease = await acquire_research_slot(user=user, symbol="SOLUSDT", timeframe="4h", channel="websocket")
    try:
        view = await research_capacity_view(user)
        assert view["plan_code"] == "quarterly"
        assert view["limit"] == 2
        assert view["active_slots"] == 1
        assert len(view["slots"]) == 1
        assert view["slots"][0]["symbol"] == "SOLUSDT"
        assert view["slots"][0]["timeframe"] == "4h"
        assert view["slots"][0]["channel"] == "websocket"
    finally:
        await release_research_slot(lease.id)
