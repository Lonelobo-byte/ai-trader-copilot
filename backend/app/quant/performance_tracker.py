"""Performance Tracker & Trade History Analytics Engine.

Aggregates historical outcomes from published trade signals and analysis sessions:
- Win Rate %, Loss Rate %, Win/Loss Streaks
- Profit Factor & Expectancy Score
- Breakdown by Market Regime and Timeframe
- Empirical Win Rate export for Kelly Position Sizing
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.brains.signal_lifecycle import TERMINAL_SIGNAL_STATUSES
from app.db.database import AsyncSessionLocal
from app.db.models import TradeSignal

logger = logging.getLogger(__name__)


async def get_historical_performance_summary() -> dict[str, Any]:
    """Compute comprehensive system performance stats from database trade history."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(TradeSignal).order_by(TradeSignal.id.desc())
            )
            signals = result.scalars().all()

            if not signals:
                return _default_performance_metrics()

            terminal_signals = [s for s in signals if s.status in TERMINAL_SIGNAL_STATUSES]

            total_generated = len(signals)
            total_completed = len(terminal_signals)

            if not terminal_signals:
                return {
                    "total_signals_generated": total_generated,
                    "total_completed": 0,
                    "win_rate_pct": 50.0,  # Default fallback
                    "wins": 0,
                    "losses": 0,
                    "profit_factor": 1.0,
                    "expectancy_r": 0.0,
                    "current_streak": {"type": "NONE", "count": 0},
                    "regime_breakdown": {},
                    "timeframe_breakdown": {},
                    "status": "insufficient_data",
                }

            wins = 0
            losses = 0
            cancelled = 0
            regime_stats: dict[str, dict[str, int]] = {}
            tf_stats: dict[str, dict[str, int]] = {}
            total_r = 0.0

            for sig in terminal_signals:
                status = sig.status
                tf = sig.timeframe or "unknown"
                context = sig.context or {}
                regime = context.get("trend_status") or "unknown"

                tf_entry = tf_stats.setdefault(tf, {"wins": 0, "losses": 0, "total": 0})
                regime_entry = regime_stats.setdefault(regime, {"wins": 0, "losses": 0, "total": 0})

                tf_entry["total"] += 1
                regime_entry["total"] += 1

                if status in {"COMPLETED", "TP1_SECURED", "TP2_SECURED", "TP3_SECURED"}:
                    wins += 1
                    tf_entry["wins"] += 1
                    regime_entry["wins"] += 1
                    stage = getattr(sig, "target_stage", 1) or 1
                    total_r += stage * 1.5
                elif status in {"STOPPED_OUT", "INVALIDATED"}:
                    losses += 1
                    tf_entry["losses"] += 1
                    regime_entry["losses"] += 1
                    total_r -= 1.0
                elif status in {"CANCELLED", "EXPIRED"}:
                    cancelled += 1

            decided_count = wins + losses
            win_rate_pct = (wins / decided_count * 100.0) if decided_count > 0 else 50.0
            profit_factor = (total_r / max(losses, 1.0)) if losses > 0 else (total_r if total_r > 0 else 1.0)
            expectancy_r = (total_r / decided_count) if decided_count > 0 else 0.0

            # Compute current win/loss streak
            streak_type = "NONE"
            streak_count = 0
            for sig in terminal_signals:
                st = sig.status
                if st in {"COMPLETED", "TP1_SECURED", "TP2_SECURED", "TP3_SECURED"}:
                    curr = "WIN"
                elif st in {"STOPPED_OUT", "INVALIDATED"}:
                    curr = "LOSS"
                else:
                    continue

                if streak_type == "NONE":
                    streak_type = curr
                    streak_count = 1
                elif streak_type == curr:
                    streak_count += 1
                else:
                    break

            # Regime win rates
            regime_summary = {}
            for r_name, r_data in regime_stats.items():
                tot = r_data["wins"] + r_data["losses"]
                wr = (r_data["wins"] / tot * 100.0) if tot > 0 else 0.0
                regime_summary[r_name] = {"win_rate_pct": round(wr, 1), "total_trades": tot}

            # Timeframe win rates
            tf_summary = {}
            for t_name, t_data in tf_stats.items():
                tot = t_data["wins"] + t_data["losses"]
                wr = (t_data["wins"] / tot * 100.0) if tot > 0 else 0.0
                tf_summary[t_name] = {"win_rate_pct": round(wr, 1), "total_trades": tot}

            return {
                "total_signals_generated": total_generated,
                "total_completed": total_completed,
                "decided_trades": decided_count,
                "wins": wins,
                "losses": losses,
                "cancelled_or_expired": cancelled,
                "win_rate_pct": round(win_rate_pct, 1),
                "profit_factor": round(max(profit_factor, 0.0), 2),
                "expectancy_r": round(expectancy_r, 2),
                "net_r_multiple": round(total_r, 2),
                "current_streak": {"type": streak_type, "count": streak_count},
                "regime_breakdown": regime_summary,
                "timeframe_breakdown": tf_summary,
                "status": "active",
            }

    except Exception as exc:
        logger.error(f"Error building historical performance summary: {exc}", exc_info=True)
        return _default_performance_metrics()


def _default_performance_metrics() -> dict[str, Any]:
    return {
        "total_signals_generated": 0,
        "total_completed": 0,
        "decided_trades": 0,
        "wins": 0,
        "losses": 0,
        "cancelled_or_expired": 0,
        "win_rate_pct": 50.0,
        "profit_factor": 1.0,
        "expectancy_r": 0.0,
        "net_r_multiple": 0.0,
        "current_streak": {"type": "NONE", "count": 0},
        "regime_breakdown": {},
        "timeframe_breakdown": {},
        "status": "no_history",
    }
