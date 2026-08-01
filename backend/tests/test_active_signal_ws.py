from datetime import datetime, timedelta, timezone

import pytest

from app.db.database import AsyncSessionLocal
from app.db.models import TradeSignal
from app.quant import active_signal_ws


@pytest.mark.asyncio
async def test_realtime_monitor_passes_execution_tape_into_entry_confirmation(monkeypatch) -> None:
    """The sub-second path must never turn a price touch into an entry by itself."""
    now = datetime.now(timezone.utc)
    signal = TradeSignal(
        symbol="BTCUSDT",
        timeframe="15m",
        side="LONG",
        status="PENDING_ENTRY",
        decision="BUY_WATCH",
        confidence=82.0,
        entry_low=99.5,
        entry_high=100.5,
        entry_reference=100.0,
        stop_initial=98.0,
        stop_current=98.0,
        target_1=102.0,
        target_2=104.0,
        target_3=106.0,
        target_runner=110.0,
        target_stage=0,
        risk_per_unit=2.0,
        risk_amount_usd=10.0,
        notional_usd=500.0,
        recommended_leverage=2,
        current_price=101.0,
        entry_timeout_at=now + timedelta(hours=1),
        expires_at=now + timedelta(hours=8),
        last_evaluated_at=now,
        events=[],
        context={},
        ai_review={},
    )
    async with AsyncSessionLocal() as db:
        db.add(signal)
        await db.commit()

    tape = {
        "available": True,
        "actual_flow": {
            "available": True,
            "bias": "BULLISH",
            "status": "BUYING_CONFIRMED",
        },
    }
    observed: dict = {}

    monkeypatch.setattr(
        active_signal_ws,
        "get_execution_tape_snapshot",
        lambda symbol, settings: tape,
    )

    def capture_advance(signal_data, *, current_price, market_context=None, now=None):
        observed["price"] = current_price
        observed["market_context"] = market_context
        return signal_data

    monkeypatch.setattr(active_signal_ws, "advance_signal", capture_advance)

    monitor = active_signal_ws.ActiveSignalWSMonitor()
    await monitor._evaluate_open_signals({"BTCUSDT": 100.0})

    assert observed["price"] == 100.0
    assert observed["market_context"]["execution_tape"] is tape
