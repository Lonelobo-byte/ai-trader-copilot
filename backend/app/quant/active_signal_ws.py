"""Active Signal Real-time WebSocket Price Monitor.

Subscribes to Binance WebSocket price streams for all open trade signals,
enabling sub-second detection when entry zones, take-profits (TP1/TP2/TP3),
or stop-loss (SL) levels are reached.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import websockets

from app.brains.signal_lifecycle import OPEN_SIGNAL_STATUSES
from app.db.database import AsyncSessionLocal
from app.db.models import TradeSignal
from app.settings import get_settings
from app.signal_service import advance_signal, _record_data, _apply

logger = logging.getLogger(__name__)

# Combined ticker WebSocket endpoints
_SPOT_MINI_TICKER_WS = "wss://stream.binance.com:9443/ws/!miniTicker@arr"
_FUTURES_MINI_TICKER_WS = "wss://fstream.binance.com/ws/!miniTicker@arr"


class ActiveSignalWSMonitor:
    """Sub-second real-time price monitor using Binance MiniTicker Array WebSockets."""

    def __init__(self):
        self._running = False
        self._last_evaluated: dict[int, float] = {}

    async def start(self) -> None:
        """Start the background WebSocket monitor loop."""
        self._running = True
        logger.info("Starting Real-time Active Signal WebSocket Monitor...")

        # Plans are built from the spot candle/order-book snapshot, so monitor
        # them against the same venue. Mixing a spot plan with a futures price
        # stream can create false stop hits from basis differences.
        ws_url = _SPOT_MINI_TICKER_WS

        while self._running:
            try:
                async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                    logger.info("Real-time WebSocket Price Monitor connected to Binance.")
                    while self._running:
                        msg = await ws.recv()
                        data = json.loads(msg)

                        # data is a list of ticker objects: [{'s': 'BTCUSDT', 'c': '65432.10'}, ...]
                        if isinstance(data, list):
                            ticker_map = {}
                            for item in data:
                                symbol = item.get("s")
                                close_price = item.get("c")
                                if symbol and close_price:
                                    try:
                                        ticker_map[symbol.upper()] = float(close_price)
                                    except ValueError:
                                        continue

                            if ticker_map:
                                await self._evaluate_open_signals(ticker_map)

            except asyncio.CancelledError:
                logger.info("Active Signal WebSocket Monitor task cancelled.")
                self._running = False
                break
            except Exception as exc:
                logger.warning(f"WebSocket Price Monitor connection lost ({exc}). Reconnecting in 3s...")
                await asyncio.sleep(3)

    async def _evaluate_open_signals(self, ticker_map: dict[str, float]) -> None:
        """Evaluate open signals against live price ticks from WebSocket stream."""
        try:
            async with AsyncSessionLocal() as db:
                from sqlalchemy import select
                result = await db.execute(
                    select(TradeSignal).where(TradeSignal.status.in_(OPEN_SIGNAL_STATUSES))
                )
                signals = result.scalars().all()

                if not signals:
                    return

                changes_made = False
                for signal in signals:
                    sym = signal.symbol.upper()
                    if sym not in ticker_map:
                        continue

                    current_price = ticker_map[sym]
                    last_price = self._last_evaluated.get(signal.id)

                    # Only advance if price changed significantly (> 0.01%) or never evaluated
                    if last_price is not None and abs(current_price - last_price) / max(last_price, 1e-9) < 0.0001:
                        continue

                    self._last_evaluated[signal.id] = current_price
                    old_status = signal.status
                    old_stage = signal.target_stage

                    updated = advance_signal(_record_data(signal), current_price=current_price)
                    if _apply(signal, updated):
                        changes_made = True
                        if old_status != signal.status:
                            logger.info(
                                f"⚡ [WS MON] Signal #{signal.id} ({sym}) status changed: "
                                f"{old_status} ➔ {signal.status} @ ${current_price:.4f}"
                            )
                        elif old_stage != signal.target_stage:
                            logger.info(
                                f"🎯 [WS MON] Signal #{signal.id} ({sym}) TP Stage advanced: "
                                f"TP{old_stage} ➔ TP{signal.target_stage} @ ${current_price:.4f}"
                            )

                if changes_made:
                    await db.commit()

        except Exception as exc:
            logger.error(f"Error evaluating open signals in WS monitor: {exc}", exc_info=True)


async def run_active_signal_ws_monitor() -> None:
    """Entry point helper to run the WS monitor in background."""
    monitor = ActiveSignalWSMonitor()
    await monitor.start()
