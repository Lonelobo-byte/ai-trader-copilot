from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import select, text

from app.brains.signal_lifecycle import (
    OPEN_SIGNAL_STATUSES,
    TERMINAL_SIGNAL_STATUSES,
    advance_signal,
    build_signal_seed,
    build_signal_view,
    evaluate_signal_approval,
    market_story_matches_signal,
)
from app.db.database import AsyncSessionLocal
from app.db.models import TradeSignal


_SIGNAL_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}
_MAX_SIGNAL_LOCKS = 2_000
_DB_READY = False
_DB_INIT_LOCK = asyncio.Lock()


async def ensure_signal_database() -> None:
    global _DB_READY
    if _DB_READY:
        return
    async with _DB_INIT_LOCK:
        if _DB_READY:
            return
        from app.db.database import init_db
        await init_db()
        # TP3 used to leave the signal open for an optional runner. The current
        # policy finalises TP3 as success, so migrate only those legacy records
        # once during startup before they can appear as active history again.
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(TradeSignal).where(TradeSignal.status == "TP3_SECURED"))
            legacy_signals = result.scalars().all()
            now = datetime.now(timezone.utc)
            for signal in legacy_signals:
                events = list(signal.events or [])
                events.append({
                    "at": now.isoformat(), "kind": "tp3_finalized",
                    "title": "TP3 finalised — successful trade",
                    "detail": "TP3 was already secured; this legacy signal is now recorded as a successful outcome.",
                })
                signal.status = "COMPLETED"
                signal.target_stage = max(3, signal.target_stage or 0)
                signal.exit_price = signal.target_3
                signal.exit_reason = "TP3 profit target reached. Trade recorded as successful and closed."
                signal.closed_at = now
                signal.last_evaluated_at = now
                signal.events = events
            if legacy_signals:
                await db.commit()
        _DB_READY = True


def _record_data(record: TradeSignal) -> dict[str, Any]:
    return {column.name: getattr(record, column.name) for column in record.__table__.columns}


def _apply(record: TradeSignal, values: Mapping[str, Any]) -> bool:
    changed = False
    for key, value in values.items():
        if not hasattr(record, key):
            continue
        if getattr(record, key) != value:
            setattr(record, key, value)
            changed = True
    return changed


def _view(record: TradeSignal | None, approval: Mapping[str, Any] | None = None) -> dict[str, Any]:
    view = build_signal_view(_record_data(record) if record else None, approval)
    for key in ("entry_timeout_at", "expires_at", "last_evaluated_at"):
        value = view.get(key)
        if isinstance(value, datetime):
            view[key] = value.astimezone(timezone.utc).isoformat() if value.tzinfo else value.replace(tzinfo=timezone.utc).isoformat()
    return view


async def get_active_signal(symbol: str, timeframe: str) -> TradeSignal | None:
    await ensure_signal_database()
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TradeSignal)
            .where(
                TradeSignal.symbol == symbol,
                TradeSignal.timeframe == timeframe,
                TradeSignal.status.in_(OPEN_SIGNAL_STATUSES),
            )
            .order_by(TradeSignal.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def _lock_signal_key(db: Any, symbol: str, timeframe: str) -> None:
    """Serialize an active-signal transition across app processes.

    SQLite development uses the caller's asyncio lock. PostgreSQL production
    additionally uses a transaction-scoped advisory lock, so two containers
    cannot both observe an empty ledger and publish the same signal.
    """
    if db.bind and db.bind.dialect.name == "postgresql":
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"trade-signal:{symbol}:{timeframe}"})


async def reconcile_signal(
    *,
    symbol: str,
    timeframe: str,
    current_price: float,
    decision: Mapping[str, Any],
    trade_setup: Mapping[str, Any],
    risk_idea: Mapping[str, Any] | None,
    trend: Mapping[str, Any],
    momentum: Mapping[str, Any],
    order_book: Mapping[str, Any],
    data_freshness: Mapping[str, Any],
    liquidity: Mapping[str, Any],
    ai_result: Mapping[str, Any] | None,
    council_approval: Mapping[str, Any] | None = None,
    execution_tape: Mapping[str, Any] | None = None,
    causal_market_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Advance the open signal or publish exactly one new, fully approved signal."""
    key = (symbol, timeframe)
    if len(_SIGNAL_LOCKS) >= _MAX_SIGNAL_LOCKS:
        for stale_key, stale_lock in list(_SIGNAL_LOCKS.items()):
            if not stale_lock.locked():
                _SIGNAL_LOCKS.pop(stale_key, None)
            if len(_SIGNAL_LOCKS) < _MAX_SIGNAL_LOCKS:
                break
    lock = _SIGNAL_LOCKS.setdefault(key, asyncio.Lock())
    latest_market_story = trade_setup.get("market_story", {})
    market_context = {
        "market_story": latest_market_story,
        "execution_tape": dict(execution_tape or {}),
        "causal_market_context": dict(causal_market_context or {}),
    }
    async with lock:
        try:
            await ensure_signal_database()
            async with AsyncSessionLocal() as db:
                await _lock_signal_key(db, symbol, timeframe)
                result = await db.execute(
                select(TradeSignal)
                .where(
                    TradeSignal.symbol == symbol,
                    TradeSignal.timeframe == timeframe,
                    TradeSignal.status.in_(OPEN_SIGNAL_STATUSES),
                )
                .order_by(TradeSignal.id.desc())
                .limit(1)
            )
                active = result.scalar_one_or_none()
                if active:
                    old_status = active.status
                    old_stage = active.target_stage
                    active_data = _record_data(active)
                    active_market_context = {
                        **market_context,
                        "market_story": (
                            latest_market_story
                            if market_story_matches_signal(active_data, latest_market_story)
                            else {}
                        ),
                    }
                    updated = advance_signal(
                        active_data,
                        current_price=current_price,
                        market_context=active_market_context,
                    )
                    _apply(active, updated)
                    await db.commit()
                
                    # Check for updates to trigger system notifications
                    from app.utils.alerts import trigger_system_notification
                    if old_status != active.status:
                        trigger_system_notification(
                        f"Signal Update: {symbol} {timeframe}",
                        f"Status changed from {old_status} to {active.status} at price {current_price}",
                    )
                    elif old_stage != active.target_stage:
                        trigger_system_notification(
                        f"Signal Target Hit: {symbol} {timeframe}",
                        f"Advanced to TP{active.target_stage} at price {current_price}",
                    )
                    return _view(active)

                approval = evaluate_signal_approval(
                decision=decision, trade_setup=trade_setup, risk_idea=risk_idea, trend=trend,
                momentum=momentum, order_book=order_book, data_freshness=data_freshness,
                liquidity=liquidity, ai_result=ai_result, current_price=current_price,
                council_approval=council_approval,
                require_council_approval=True,
            )
                if not approval["approved"]:
                    latest = await db.execute(
                    select(TradeSignal)
                    .where(TradeSignal.symbol == symbol, TradeSignal.timeframe == timeframe)
                    .order_by(TradeSignal.id.desc())
                    .limit(1)
                )
                    previous = latest.scalar_one_or_none()
                    return _view(previous if previous and previous.status in TERMINAL_SIGNAL_STATUSES else None, approval)

                seed = build_signal_seed(
                symbol=symbol, timeframe=timeframe, decision=decision, trade_setup=trade_setup,
                approval=approval, current_price=current_price, context=market_context,
                ai_review=ai_result or {},
            )
                signal = TradeSignal(**seed)
                db.add(signal)
                await db.commit()
                await db.refresh(signal)
            
                # Notify new signal
                from app.utils.alerts import trigger_system_notification
                trigger_system_notification(
                f"New Signal: {symbol} {timeframe}",
                f"{signal.side} setup published. Entry Ref: {signal.entry_reference}, Stop: {signal.stop_initial}",
            )
                return _view(signal)
        finally:
            pass


async def cancel_signal(signal_id: int, reason: str = "Dismissed by operator.") -> dict[str, Any] | None:
    await ensure_signal_database()
    async with AsyncSessionLocal() as db:
        signal = await db.get(TradeSignal, signal_id)
        if not signal:
            return None
        if signal.status in OPEN_SIGNAL_STATUSES:
            events = list(signal.events or [])
            now = datetime.now(timezone.utc)
            events.append({
                "at": now.isoformat(), "kind": "operator_cancelled", "title": "Signal cancelled", "detail": reason,
            })
            signal.status = "CANCELLED"
            signal.exit_reason = reason
            signal.closed_at = now
            signal.last_evaluated_at = now
            signal.events = events
            await db.commit()
        return _view(signal)


async def list_signal_history(limit: int = 20) -> list[dict[str, Any]]:
    await ensure_signal_database()
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradeSignal).order_by(TradeSignal.id.desc()).limit(limit))
        return [_view(signal) for signal in result.scalars().all()]


async def monitor_open_signals(
    price_lookup: Any,
    market_context_lookup: Any | None = None,
) -> bool:
    """Background fallback when no dashboard websocket is connected."""
    await ensure_signal_database()
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TradeSignal).where(TradeSignal.status.in_(OPEN_SIGNAL_STATUSES)))
        signals = result.scalars().all()
        if not signals:
            return False
        for signal in signals:
            try:
                price = float(await price_lookup(signal.symbol))
            except Exception:
                continue
            market_context: Mapping[str, Any] | None = None
            if market_context_lookup is not None:
                try:
                    market_context = await market_context_lookup(
                        signal.symbol,
                        signal.timeframe,
                        signal.side,
                    )
                except Exception:
                    # Price protection must continue even if the slower causal
                    # candle refresh is temporarily unavailable.
                    market_context = None
            updated = advance_signal(
                _record_data(signal),
                current_price=price,
                market_context=market_context,
            )
            _apply(signal, updated)
        await db.commit()
        return True
