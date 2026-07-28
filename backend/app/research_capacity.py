"""Server-side concurrent research capacity for subscription plans."""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, func, select, update

from .auth import subscription_is_active, utcnow
from .db.database import AsyncSessionLocal
from .db.models import ResearchSlot, Subscription, User

PLAN_RESEARCH_LIMITS = {
    "monthly": 1,
    "quarterly": 2,
    "half_yearly": 4,
    "annual": 4,
}
RESEARCH_SLOT_TTL_SECONDS = 75
RESEARCH_SLOT_HEARTBEAT_SECONDS = 20
_USER_CAPACITY_LOCKS: dict[str, asyncio.Lock] = {}
_MAX_USER_CAPACITY_LOCKS = 5_000


def _user_lock(user_id: str) -> asyncio.Lock:
    if len(_USER_CAPACITY_LOCKS) >= _MAX_USER_CAPACITY_LOCKS:
        for stale_id, stale_lock in list(_USER_CAPACITY_LOCKS.items()):
            if not stale_lock.locked():
                _USER_CAPACITY_LOCKS.pop(stale_id, None)
            if len(_USER_CAPACITY_LOCKS) < _MAX_USER_CAPACITY_LOCKS:
                break
    return _USER_CAPACITY_LOCKS.setdefault(user_id, asyncio.Lock())


class ResearchCapacityExceeded(RuntimeError):
    def __init__(self, *, plan_code: str, limit: int, active_slots: int):
        self.plan_code = plan_code
        self.limit = limit
        self.active_slots = active_slots
        super().__init__(
            f"Your {plan_code.replace('_', ' ')} membership allows {limit} concurrent live research "
            f"{'tab' if limit == 1 else 'tabs'}. Close another active research tab and try again."
        )


@dataclass(frozen=True)
class ResearchSlotLease:
    id: str
    plan_code: str
    limit: int
    active_slots: int


def _research_plan(subscriptions: list[Subscription], *, is_admin: bool) -> tuple[str, int]:
    active_plans = [subscription.plan_code for subscription in subscriptions if subscription_is_active(subscription)]
    if active_plans:
        plan_code = max(active_plans, key=lambda code: PLAN_RESEARCH_LIMITS.get(code, 1))
        return plan_code, PLAN_RESEARCH_LIMITS.get(plan_code, 1)
    # Local development can deliberately disable subscription enforcement. It
    # still gets the most conservative one-slot behavior rather than bypassing
    # concurrency controls. The explicitly trusted platform admin gets the
    # highest advertised capacity.
    return ("annual", 4) if is_admin else ("monthly", 1)


def _selected_active_subscription(subscriptions: list[Subscription]) -> Subscription | None:
    active = [subscription for subscription in subscriptions if subscription_is_active(subscription)]
    return max(active, key=lambda subscription: PLAN_RESEARCH_LIMITS.get(subscription.plan_code, 1), default=None)


async def acquire_research_slot(*, user: User, symbol: str, timeframe: str, channel: str) -> ResearchSlotLease:
    """Atomically reserve one live-research slot for a user.

    PostgreSQL serializes this by locking the user row. The per-user asyncio
    lock closes SQLite/local races within a process; expired leases make an
    abandoned browser tab self-heal even when it cannot send a close frame.
    """
    lock = _user_lock(user.id)
    async with lock:
        now = utcnow()
        expiry = now + timedelta(seconds=RESEARCH_SLOT_TTL_SECONDS)
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.scalar(select(User).where(User.id == user.id).with_for_update())
                await session.execute(delete(ResearchSlot).where(ResearchSlot.user_id == user.id, ResearchSlot.expires_at <= now))
                rows = await session.scalars(select(Subscription).where(Subscription.user_id == user.id))
                plan_code, limit = _research_plan(list(rows), is_admin=user.role == "admin")
                active_slots = int(await session.scalar(select(func.count()).select_from(ResearchSlot).where(ResearchSlot.user_id == user.id, ResearchSlot.expires_at > now)) or 0)
                if active_slots >= limit:
                    raise ResearchCapacityExceeded(plan_code=plan_code, limit=limit, active_slots=active_slots)
                lease = ResearchSlot(
                    id=str(uuid.uuid4()), user_id=user.id, symbol=symbol, timeframe=timeframe,
                    channel=channel, heartbeat_at=now, expires_at=expiry,
                )
                session.add(lease)
                return ResearchSlotLease(id=lease.id, plan_code=plan_code, limit=limit, active_slots=active_slots + 1)


async def heartbeat_research_slot(lease_id: str) -> bool:
    now = utcnow()
    expiry = now + timedelta(seconds=RESEARCH_SLOT_TTL_SECONDS)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            update(ResearchSlot)
            .where(ResearchSlot.id == lease_id, ResearchSlot.expires_at > now)
            .values(heartbeat_at=now, expires_at=expiry)
        )
        await session.commit()
    return bool(result.rowcount)


async def release_research_slot(lease_id: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(delete(ResearchSlot).where(ResearchSlot.id == lease_id))
        await session.commit()


async def research_capacity_view(user: User) -> dict:
    """Return the authenticated user's plan and only their active leases."""
    lock = _user_lock(user.id)
    async with lock:
        now = utcnow()
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(delete(ResearchSlot).where(ResearchSlot.user_id == user.id, ResearchSlot.expires_at <= now))
                subscriptions = list(await session.scalars(select(Subscription).where(Subscription.user_id == user.id)))
                plan_code, limit = _research_plan(subscriptions, is_admin=user.role == "admin")
                subscription = _selected_active_subscription(subscriptions)
                slots = list(await session.scalars(
                    select(ResearchSlot)
                    .where(ResearchSlot.user_id == user.id, ResearchSlot.expires_at > now)
                    .order_by(ResearchSlot.acquired_at.asc())
                ))
                return {
                    "plan_code": plan_code,
                    "plan_ends_at": subscription.ends_at if subscription else None,
                    "status": subscription.status if subscription else "development",
                    "limit": limit,
                    "active_slots": len(slots),
                    "slots": [
                        {
                            "symbol": slot.symbol,
                            "timeframe": slot.timeframe,
                            "channel": slot.channel,
                            "expires_at": slot.expires_at,
                        }
                        for slot in slots
                    ],
                }
