"""Background loops that run for the lifetime of the application.

These were previously inlined in ``main.py``.  Extracting them keeps the
entry-point file small and makes it easy to test or swap the loops.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select

from .data_sources.binance_public import BinancePublicClient
from .settings import get_settings
from .signal_service import monitor_open_signals

logger = logging.getLogger(__name__)


async def outcome_tracker_loop() -> None:
    """Check PENDING analysis sessions and evaluate if targets or stops were hit."""
    from .db.database import AsyncSessionLocal
    from .db.models import AnalysisSession

    settings = get_settings()
    client = BinancePublicClient(settings.binance_public_base_url)

    logger.info("APEX Outcome Tracker background loop started.")
    while True:
        try:
            await asyncio.sleep(60)

            async with AsyncSessionLocal() as db:
                stmt = select(AnalysisSession).where(AnalysisSession.outcome == "PENDING")
                res = await db.execute(stmt)
                pending_sessions = res.scalars().all()

                if not pending_sessions:
                    continue

                for session in pending_sessions:
                    now = datetime.now(session.timestamp.tzinfo)
                    age_hours = (now - session.timestamp).total_seconds() / 3600.0

                    if age_hours > 24:
                        session.outcome = "EXPIRED"
                        db.add(session)
                        logger.info(f"Session {session.id} ({session.symbol}) expired after 24h.")
                        continue

                    start_ms = int(session.timestamp.timestamp() * 1000)
                    try:
                        candles = await client.klines(session.symbol, session.timeframe, limit=500, startTime=start_ms)
                        if not candles:
                            continue

                        target_hit = False
                        stop_hit = False

                        for c in candles:
                            is_long = "BUY" in session.decision
                            high = float(c.high)
                            low = float(c.low)

                            if is_long:
                                if low <= session.stop_price:
                                    stop_hit = True
                                if high >= session.target_price:
                                    target_hit = True
                            else:
                                if high >= session.stop_price:
                                    stop_hit = True
                                if low <= session.target_price:
                                    target_hit = True

                            if stop_hit and target_hit:
                                session.outcome = "FAILURE"
                                break
                            elif stop_hit:
                                session.outcome = "FAILURE"
                                break
                            elif target_hit:
                                session.outcome = "SUCCESS"
                                break

                        if stop_hit or target_hit:
                            is_success = session.outcome == "SUCCESS"
                            is_long = "BUY" in (session.decision or "")

                            # 1. MTF Correct
                            s_trend = session.trend or (session.market_conditions.get("trend") if session.market_conditions else "mixed")
                            if is_success:
                                session.mtf_correct = (is_long and s_trend == "bullish") or (not is_long and s_trend == "bearish")
                            else:
                                session.mtf_correct = (is_long and s_trend != "bullish") or (not is_long and s_trend != "bearish")

                            # 2. Funding Correct
                            s_funding = session.funding if session.funding is not None else 0.0
                            if is_success:
                                session.funding_correct = (is_long and s_funding < 0.0003) or (not is_long and s_funding > -0.0001)
                            else:
                                session.funding_correct = (is_long and s_funding >= 0.0003) or (not is_long and s_funding <= -0.0001)

                            # 3. Liquidation Correct
                            session.liquidation_correct = is_success

                            # 4. Orderflow Correct
                            of_report = session.order_flow_analyst or {}
                            of_bias = of_report.get("bias", "NEUTRAL")
                            if is_success:
                                session.orderflow_correct = (is_long and of_bias == "BULLISH") or (not is_long and of_bias == "BEARISH") or of_bias == "NEUTRAL"
                            else:
                                session.orderflow_correct = (is_long and of_bias == "BEARISH") or (not is_long and of_bias == "BULLISH")

                            # 5. PreMortem Correct
                            pm_report = session.pre_mortem_analyst or {}
                            severity = pm_report.get("severity_score", 5)
                            if is_success:
                                session.premortem_correct = severity <= 5
                            else:
                                session.premortem_correct = severity >= 6

                            db.add(session)
                            logger.info(f"Outcome determined for Session {session.id} ({session.symbol}): {session.outcome} | Reliability tracked.")

                    except Exception as kline_exc:
                        logger.error(f"Error fetching candles for outcome validation on session {session.id}: {kline_exc}")

                await db.commit()

        except Exception as e:
            logger.error(f"Error in outcome_tracker_loop: {e}", exc_info=True)


async def signal_monitor_loop() -> None:
    """Background signal lifecycle monitor with real-time WebSocket price stream and REST polling fallback."""
    settings = get_settings()
    client = BinancePublicClient(settings.binance_public_base_url)
    logger.info("Signal lifecycle monitor started.")

    # Start the sub-second WebSocket price monitor in the background
    from app.quant.active_signal_ws import run_active_signal_ws_monitor
    ws_task = asyncio.create_task(run_active_signal_ws_monitor())

    async def latest_price(symbol: str) -> float:
        ticker = await client.ticker_24hr(symbol)
        return float(ticker["lastPrice"])

    has_open_signals = False
    while True:
        try:
            # The dedicated websocket monitor handles active signals in real
            # time. This REST path is a low-frequency fallback, not another
            # competing live feed. An idle ledger checks less often.
            await asyncio.sleep(10 if has_open_signals else 30)
            has_open_signals = await monitor_open_signals(latest_price)
        except asyncio.CancelledError:
            ws_task.cancel()
            raise
        except Exception:
            logger.exception("Signal lifecycle monitor failed for this cycle.")
