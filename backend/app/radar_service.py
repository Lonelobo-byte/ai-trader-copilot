"""Shared, demand-aware market Radar snapshots.

Radar discovery is public market information: it must be calculated once per
timeframe pair, not once per browser. PostgreSQL persists the snapshot and
refresh lease so separate Docker workers share the same work. A stale snapshot
is deliberately served while one worker refreshes it in the background.
"""
from __future__ import annotations

import asyncio
import copy
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, text

from app.auth import as_utc, utcnow
from app.db.database import AsyncSessionLocal
from app.db.models import RadarSnapshot
from app.quant.momentum_scanner import get_breakout_candidates
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)

SUPPORTED_PAIRS = {("5m", "1h"), ("15m", "4h"), ("1h", "1d")}
_LOCAL_REFRESH_LOCKS: dict[str, asyncio.Lock] = {}
_MAX_LOCAL_REFRESH_LOCKS = 32
_BACKGROUND_REFRESH_TASKS: dict[str, asyncio.Task[Any]] = {}


@dataclass(frozen=True)
class RadarRead:
    candidates: list[dict[str, Any]]
    captured_at: str | None
    next_refresh_at: str | None
    state: str  # FRESH, STALE_REFRESHING, INITIAL


def pair_key(ltf: str, htf: str) -> str:
    return f"{ltf}:{htf}"


def freshness_seconds(ltf: str, htf: str, settings: Settings) -> int:
    values = {
        ("5m", "1h"): settings.radar_fresh_5m_1h_seconds,
        ("15m", "4h"): settings.radar_fresh_15m_4h_seconds,
        ("1h", "1d"): settings.radar_fresh_1h_1d_seconds,
    }
    return max(5, int(values[(ltf, htf)]))


def _is_fresh(snapshot: RadarSnapshot, *, now, settings: Settings) -> bool:
    captured_at = as_utc(snapshot.captured_at)
    return bool(snapshot.payload and captured_at and now - captured_at <= timedelta(seconds=freshness_seconds(snapshot.ltf, snapshot.htf, settings)))


def _next_refresh_at(snapshot: RadarSnapshot, *, settings: Settings, stale: bool = False) -> str | None:
    """Return one server-owned countdown target for every browser."""
    if stale:
        # A refresh may finish well before its safety lease. Poll at the next
        # globally aligned 15-second boundary, rather than making each
        # browser invent its own countdown or wait for the full lease.
        now = utcnow()
        next_epoch = (int(now.timestamp() // 15) + 1) * 15
        return datetime.fromtimestamp(next_epoch, timezone.utc).isoformat()
    captured_at = as_utc(snapshot.captured_at)
    if captured_at:
        return (captured_at + timedelta(seconds=freshness_seconds(snapshot.ltf, snapshot.htf, settings))).isoformat()
    return None


def _refresh_lock(key: str) -> asyncio.Lock:
    if len(_LOCAL_REFRESH_LOCKS) >= _MAX_LOCAL_REFRESH_LOCKS:
        for old_key, lock in list(_LOCAL_REFRESH_LOCKS.items()):
            if not lock.locked():
                _LOCAL_REFRESH_LOCKS.pop(old_key, None)
    return _LOCAL_REFRESH_LOCKS.setdefault(key, asyncio.Lock())


async def _advisory_lock(db: Any, key: str) -> None:
    if db.bind and db.bind.dialect.name == "postgresql":
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"atc:radar:{key}"})


async def _record_demand(ltf: str, htf: str) -> RadarSnapshot | None:
    """Record interest in a pair. This drives warm refreshes, not entitlement."""
    key = pair_key(ltf, htf)
    now = utcnow()
    async with AsyncSessionLocal() as db:
        snapshot = await db.get(RadarSnapshot, key)
        if snapshot is None:
            return None
        # Demand is a heat signal, not an audit log. Debouncing avoids one DB
        # write per public browser poll while preserving the active-window
        # semantics that keep a pair warm.
        last_requested = as_utc(snapshot.last_requested_at)
        if last_requested is None or now - last_requested >= timedelta(seconds=15):
            snapshot.last_requested_at = now
            snapshot.demand_count = int(snapshot.demand_count or 0) + 1
            await db.commit()
        return snapshot


async def _claim_refresh(ltf: str, htf: str, settings: Settings) -> tuple[bool, RadarSnapshot | None]:
    """Acquire a short durable refresh lease without holding a DB transaction during I/O."""
    key = pair_key(ltf, htf)
    now = utcnow()
    lease_until = now + timedelta(seconds=max(30, int(settings.radar_refresh_lease_seconds)))
    async with AsyncSessionLocal() as db:
        await _advisory_lock(db, key)
        snapshot = await db.get(RadarSnapshot, key, with_for_update=True)
        if snapshot is None:
            snapshot = RadarSnapshot(key=key, ltf=ltf, htf=htf, last_requested_at=now, demand_count=1)
            db.add(snapshot)
            await db.flush()
        if _is_fresh(snapshot, now=now, settings=settings):
            return False, snapshot
        refreshing_until = as_utc(snapshot.refreshing_until)
        if refreshing_until and refreshing_until > now:
            return False, snapshot
        snapshot.refreshing_until = lease_until
        snapshot.last_error = None
        await db.commit()
        return True, snapshot


async def refresh_radar_pair(ltf: str, htf: str, *, settings: Settings | None = None) -> RadarRead | None:
    """Refresh exactly one shared pair if its durable lease can be claimed."""
    if (ltf, htf) not in SUPPORTED_PAIRS:
        raise ValueError("Unsupported Radar timeframe pair.")
    settings = settings or get_settings()
    key = pair_key(ltf, htf)
    lock = _refresh_lock(key)
    async with lock:
        claimed, existing = await _claim_refresh(ltf, htf, settings)
        if not claimed:
            if existing and existing.payload:
                captured_at = as_utc(existing.captured_at)
                state = "FRESH" if _is_fresh(existing, now=utcnow(), settings=settings) else "STALE_REFRESHING"
                return RadarRead(
                    copy.deepcopy(existing.payload), captured_at.isoformat() if captured_at else None,
                    _next_refresh_at(existing, settings=settings, stale=state == "STALE_REFRESHING"), state,
                )
            return None
        try:
            candidates = await get_breakout_candidates(ltf=ltf, htf=htf, use_ai=False)
        except Exception as exc:
            logger.exception("Shared Radar refresh failed for %s", key)
            async with AsyncSessionLocal() as db:
                snapshot = await db.get(RadarSnapshot, key, with_for_update=True)
                if snapshot:
                    snapshot.refreshing_until = None
                    snapshot.last_error = "Market-data refresh failed. The last valid shared snapshot remains available."
                    await db.commit()
            raise

        captured_at = utcnow()
        async with AsyncSessionLocal() as db:
            snapshot = await db.get(RadarSnapshot, key, with_for_update=True)
            if snapshot is None:
                # Defensive recovery if an operator manually removed a row
                # while a refresh was in-flight.
                snapshot = RadarSnapshot(key=key, ltf=ltf, htf=htf)
                db.add(snapshot)
            snapshot.payload = candidates
            snapshot.captured_at = captured_at
            snapshot.refreshing_until = None
            snapshot.last_error = None
            await db.commit()
        return RadarRead(
            copy.deepcopy(candidates), captured_at.isoformat(),
            (captured_at + timedelta(seconds=freshness_seconds(ltf, htf, settings))).isoformat(), "FRESH",
        )


def _background_refresh(ltf: str, htf: str, settings: Settings) -> None:
    """Schedule a non-blocking refresh; the durable lease deduplicates workers."""
    key = pair_key(ltf, htf)
    existing = _BACKGROUND_REFRESH_TASKS.get(key)
    if existing is not None and not existing.done():
        return
    task = asyncio.create_task(refresh_radar_pair(ltf, htf, settings=settings))
    _BACKGROUND_REFRESH_TASKS[key] = task

    def log_failure(completed: asyncio.Task[Any]) -> None:
        if _BACKGROUND_REFRESH_TASKS.get(key) is completed:
            _BACKGROUND_REFRESH_TASKS.pop(key, None)
        try:
            completed.result()
        except Exception:
            logger.debug("Background Radar refresh ended without a new snapshot.", exc_info=True)

    task.add_done_callback(log_failure)


async def read_radar_pair(ltf: str, htf: str, *, settings: Settings | None = None) -> RadarRead:
    """Return shared data immediately, refreshing only on demand.

    The first request for an unseen pair blocks once to establish a truthful
    baseline. Later stale reads return the last valid snapshot immediately and
    trigger one background refresh shared by every visitor.
    """
    if (ltf, htf) not in SUPPORTED_PAIRS:
        raise ValueError("Unsupported Radar timeframe pair.")
    settings = settings or get_settings()
    snapshot = await _record_demand(ltf, htf)
    now = utcnow()
    if snapshot and snapshot.payload:
        captured_at = as_utc(snapshot.captured_at)
        if _is_fresh(snapshot, now=now, settings=settings):
            return RadarRead(
                copy.deepcopy(snapshot.payload), captured_at.isoformat() if captured_at else None,
                _next_refresh_at(snapshot, settings=settings), "FRESH",
            )
        _background_refresh(ltf, htf, settings)
        return RadarRead(
            copy.deepcopy(snapshot.payload), captured_at.isoformat() if captured_at else None,
            _next_refresh_at(snapshot, settings=settings, stale=True), "STALE_REFRESHING",
        )

    refreshed = await refresh_radar_pair(ltf, htf, settings=settings)
    if refreshed is None:
        # Another worker may own the first refresh. Give it a short chance to
        # publish instead of launching duplicate market scans.
        for _ in range(10):
            await asyncio.sleep(0.2)
            async with AsyncSessionLocal() as db:
                snapshot = await db.get(RadarSnapshot, pair_key(ltf, htf))
                if snapshot and snapshot.payload:
                    captured_at = as_utc(snapshot.captured_at)
                    return RadarRead(
                        copy.deepcopy(snapshot.payload), captured_at.isoformat() if captured_at else None,
                        _next_refresh_at(snapshot, settings=settings), "INITIAL",
                    )
        raise RuntimeError("The shared Radar snapshot is being prepared. Please retry shortly.")
    return refreshed


async def warm_requested_radar_pairs(*, settings: Settings | None = None) -> int:
    """Refresh only pairs with recent user demand; idle pairs remain dormant."""
    settings = settings or get_settings()
    cutoff = utcnow() - timedelta(seconds=max(30, int(settings.radar_demand_window_seconds)))
    async with AsyncSessionLocal() as db:
        rows = await db.scalars(select(RadarSnapshot).where(RadarSnapshot.last_requested_at >= cutoff))
        pairs = [(row.ltf, row.htf) for row in rows if (row.ltf, row.htf) in SUPPORTED_PAIRS]
    for ltf, htf in pairs:
        _background_refresh(ltf, htf, settings)
    return len(pairs)


async def radar_warm_loop() -> None:
    """Keep recently requested pairs warm without scanning dormant timeframes."""
    settings = get_settings()
    while True:
        try:
            await warm_requested_radar_pairs(settings=settings)
        except Exception:
            logger.exception("Demand-aware Radar warm cycle failed.")
        await asyncio.sleep(max(5, int(settings.radar_warm_check_seconds)))
