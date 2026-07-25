from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from app.settings import Settings
from app.data_sources.binance_public import Candle
from app.data_sources.macro import fetch_macro_data
from app.brains.agents import (
    run_tech_analyst,
    run_order_flow_analyst,
    run_macro_analyst,
    run_news_analyst,
    run_devils_advocate,
    run_risk_manager,
    run_pre_mortem_analyst,
    run_cio,
)
from app.db.database import AsyncSessionLocal, init_db
from app.db.models import AnalysisSession
from sqlalchemy import select
from app.data_sources.calendar import fetch_economic_events

logger = logging.getLogger(__name__)

# Track if DB is initialized
_db_initialized = False


def compute_session_similarity(
    s: AnalysisSession,
    symbol: str,
    trend: str,
    funding: float,
    oi: float,
    decision: str,
    regime: str
) -> float:
    score = 0.0
    
    # 1. Symbol match
    if s.symbol == symbol:
        score += 25.0
        
    # 2. Trend match
    s_trend = s.trend or (s.market_conditions.get("trend") if s.market_conditions else None)
    if s_trend == trend:
        score += 20.0
        
    # 3. Market Regime match
    s_regime = s.regime or (s.market_conditions.get("regime") if s.market_conditions else None)
    if s_regime == regime:
        score += 15.0
        
    # 4. Funding rate similarity (normal base is 0.0001)
    s_funding = s.funding if s.funding is not None else (s.market_conditions.get("funding_rate") if s.market_conditions else 0.0)
    funding_diff = abs(s_funding - funding)
    score += max(0.0, 15.0 - (funding_diff * 75000))
    
    # 5. Open interest similarity
    s_oi = s.oi if s.oi is not None else (s.market_conditions.get("open_interest") if s.market_conditions else 0.0)
    if oi > 0:
        oi_diff_pct = abs(s_oi - oi) / oi
        score += max(0.0, 15.0 - (oi_diff_pct * 15))
        
    # 6. Decision match
    s_is_buy = "BUY" in (s.decision or "")
    curr_is_buy = "BUY" in (decision or "")
    if s_is_buy == curr_is_buy:
        score += 10.0
        
    return score


def calculate_dynamic_reliability_weights(sessions: list[AnalysisSession]) -> dict[str, float]:
    completed = [s for s in sessions if s.outcome in ("SUCCESS", "FAILURE")]
    total = len(completed)
    if total < 5:
        # Base fallback weights if memory is too cold
        return {
            "mtf": 0.8,
            "funding_oi": 0.75,
            "liquidation": 0.8,
            "orderflow": 0.7,
            "rag": 0.75,
            "premortem": 0.7,
        }
        
    correct_counts = {
        "mtf": 0,
        "funding_oi": 0,
        "liquidation": 0,
        "orderflow": 0,
        "rag": 0,
        "premortem": 0,
    }
    
    for s in completed:
        if s.mtf_correct:
            correct_counts["mtf"] += 1
        if s.funding_correct:
            correct_counts["funding_oi"] += 1
        if s.liquidation_correct:
            correct_counts["liquidation"] += 1
        if s.orderflow_correct:
            correct_counts["orderflow"] += 1
        if s.premortem_correct:
            correct_counts["premortem"] += 1
            
        mc = s.market_conditions or {}
        rag_win_rate = mc.get("historical_stats", {}).get("historical_win_rate", 50.0)
        if (rag_win_rate >= 50.0 and s.outcome == "SUCCESS") or (rag_win_rate < 50.0 and s.outcome == "FAILURE"):
            correct_counts["rag"] += 1
            
    return {k: v / total for k, v in correct_counts.items()}


async def run_ai_analysis(
    symbol: str,
    timeframe: str,
    candles: list[Candle],
    ticker: dict[str, Any],
    order_book: dict[str, Any],
    sweep: dict[str, Any],
    risk_idea: dict[str, Any] | None,
    news_articles: list[dict[str, Any]],
    settings: Settings,
    extra_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    global _db_initialized
    if not _db_initialized:
        await init_db()
        _db_initialized = True

    logger.info(f"Starting APEX Tier-3 Agentic Analysis for {symbol} on {timeframe}")

    # 1. Fetch Macro & Economic Calendar Events concurrently
    macro_task = fetch_macro_data()
    calendar_task = fetch_economic_events(settings.app_env)
    macro_data, calendar_events = await asyncio.gather(macro_task, calendar_task)

    # Calculate Macro Volatility Blockouts
    blockout_active = False
    blockout_reason = ""
    now_utc = datetime.utcnow()
    for event in calendar_events:
        if event.get("importance") == "HIGH":
            try:
                ev_time_str = event.get("time").replace("Z", "")
                ev_time = datetime.fromisoformat(ev_time_str)
                time_diff_mins = (ev_time - now_utc).total_seconds() / 60.0
                if 0 <= time_diff_mins <= 120:  # Within 2 hours
                     blockout_active = True
                     blockout_reason = f"High impact event '{event.get('title')}' in {int(time_diff_mins)} mins."
                     break
            except Exception as exc:
                logger.warning(f"Error parsing calendar event date '{event.get('time')}': {exc}")

    # 2. Query Memory Layer for Similar Historical Setups (RAG)
    similar_count = 0
    historical_win_rate = 50.0
    matched_buy = 0
    matched_sell = 0
    top_20 = []
    dynamic_weights = {}

    try:
        async with AsyncSessionLocal() as db_sess:
            # Query completed setups (SUCCESS / FAILURE)
            stmt = select(AnalysisSession).where(
                AnalysisSession.outcome.in_(["SUCCESS", "FAILURE"])
            )
            res = await db_sess.execute(stmt)
            sessions = res.scalars().all()
            
            # Compute similarity to current context
            regime = extra_context.get("regime", "RANGING") if extra_context else "RANGING"
            funding = extra_context.get("funding_rate", 0.0) if extra_context else 0.0
            oi = extra_context.get("open_interest", 0.0) if extra_context else 0.0
            current_trend = ticker.get("trend_status", "sideways_or_mixed")
            decision_dir = sweep.get("direction", "HOLD")

            scored_sessions = []
            for s in sessions:
                sim = compute_session_similarity(
                    s=s,
                    symbol=symbol,
                    trend=current_trend,
                    funding=funding,
                    oi=oi,
                    decision=decision_dir,
                    regime=regime
                )
                scored_sessions.append((sim, s))
            
            # Sort by similarity descending and select top 20
            scored_sessions.sort(key=lambda x: x[0], reverse=True)
            top_20 = [s for _, s in scored_sessions[:20]]
            similar_count = len(top_20)
            
            if similar_count > 0:
                successes = len([s for s in top_20 if s.outcome == "SUCCESS"])
                historical_win_rate = (successes / similar_count) * 100
                logger.info(f"RAG: Found {similar_count} similar completed setups. Win Rate: {historical_win_rate:.2f}%")
            else:
                historical_win_rate = 50.0
                
            # Compute dynamimc reliability weights based on all completed sessions
            dynamic_weights = calculate_dynamic_reliability_weights(sessions)
    except Exception as exc:
        logger.error(f"Failed to query database historical stats: {exc}")

    historical_stats = {
        "similar_setups_count": similar_count,
        "historical_win_rate": round(historical_win_rate, 2),
        "matched_buy": matched_buy,
        "matched_sell": matched_sell
    }

    macro_blockout = {
        "active": blockout_active,
        "reason": blockout_reason
    }

    # Step 3: Run Sub-Agents concurrently
    tech_task = run_tech_analyst(symbol, timeframe, candles, ticker, sweep, settings)
    order_flow_task = run_order_flow_analyst(symbol, timeframe, order_book, settings)
    macro_task_agent = run_macro_analyst(symbol, timeframe, macro_data, settings)
    news_task = run_news_analyst(symbol, timeframe, news_articles, settings)
    
    # Pack temporary stats/blockouts into risk idea for evaluation
    risk_payload = (risk_idea or {}).copy()
    if risk_payload:
        risk_payload["macro_blockout"] = macro_blockout
        risk_payload["historical_stats"] = historical_stats
        
    risk_task = run_risk_manager(symbol, risk_payload, settings)

    results = await asyncio.gather(
        tech_task, order_flow_task, macro_task_agent, news_task, risk_task, return_exceptions=True
    )
    
    # Handle possible exceptions from gather
    def get_result(res):
        return res if not isinstance(res, Exception) else {"error": str(res)}

    # Inject RAG setup textual history
    rag_setups_text = ""
    for idx, s in enumerate(top_20[:5]):
        s_trend = s.trend or (s.market_conditions.get("trend") if s.market_conditions else "mixed")
        s_regime = s.regime or (s.market_conditions.get("regime") if s.market_conditions else "mixed")
        rag_setups_text += (
            f"- Setup #{idx+1}: {s.symbol} ({s.timeframe}) | Decision: {s.decision} | "
            f"Regime: {s_regime} | Trend: {s_trend} | Outcome: {s.outcome} | "
            f"Entry: {s.entry_price}, Target: {s.target_price}, Stop: {s.stop_price}\n"
        )

    reports = {
        "technical_analyst": get_result(results[0]),
        "order_flow_analyst": get_result(results[1]),
        "macro_analyst": get_result(results[2]),
        "news_analyst": get_result(results[3]),
        "risk_manager": get_result(results[4]),
        "historical_stats": historical_stats,
        "historical_rag_setups": rag_setups_text,
        "calendar_events": calendar_events,
        "macro_blockout": macro_blockout,
        "market_regime": extra_context.get("regime", "RANGING") if extra_context else "RANGING",
        "funding_rate": extra_context.get("funding_rate", 0.0) if extra_context else 0.0,
        "open_interest": extra_context.get("open_interest", 0.0) if extra_context else 0.0,
        "liquidation_magnets": extra_context.get("liquidations", {}) if extra_context else {},
        # ── Quantitative engine outputs for CIO ──
        "probability_engine": extra_context.get("probability_engine", {}) if extra_context else {},
        "microstructure": extra_context.get("microstructure", {}) if extra_context else {},
        "statistical_features": extra_context.get("statistical_features", {}) if extra_context else {},
        "market_state": extra_context.get("market_state", {}) if extra_context else {},
        "risk_engine": extra_context.get("risk_engine", {}) if extra_context else {},
    }

    # Run Devil's Advocate and Pre-Mortem Analyst concurrently
    devils_task = run_devils_advocate(symbol, reports, settings)
    pre_mortem_task = run_pre_mortem_analyst(symbol, reports, settings)
    
    devils_res, pre_mortem_res = await asyncio.gather(devils_task, pre_mortem_task)
    
    reports["devils_advocate"] = devils_res if not isinstance(devils_res, Exception) else {"error": str(devils_res)}
    reports["pre_mortem_analyst"] = pre_mortem_res if not isinstance(pre_mortem_res, Exception) else {"error": str(pre_mortem_res)}

    # Step 3: Run Chief Investment Officer
    cio_decision = await run_cio(symbol, timeframe, reports, settings)
    
    decision = cio_decision.get("decision", "HOLD")
    confidence = cio_decision.get("confidence_pct", 50)
    report_md = cio_decision.get("report_md", "Failed to generate CIO report.")
    explanation = cio_decision.get("explanation", "")
    
    suggested_entry = cio_decision.get("suggested_entry")
    suggested_stop = cio_decision.get("suggested_stop")
    suggested_targets = cio_decision.get("suggested_targets")

    # Save to SQLite Memory Layer
    has_setup = (risk_idea is not None) or (suggested_entry is not None)
    entry_val = float(suggested_entry) if suggested_entry is not None else (float(risk_idea.get("entry_reference")) if risk_idea else None)
    stop_val = float(suggested_stop) if suggested_stop is not None else (float(risk_idea.get("smart_stop")) if risk_idea else None)
    
    if suggested_targets and len(suggested_targets) > 0 and suggested_targets[0] is not None:
        target_val = float(suggested_targets[0])
    else:
        target_val = float(risk_idea.get("smart_target")) if risk_idea else None

    outcome_val = "PENDING" if has_setup else "EXPIRED"

    async with AsyncSessionLocal() as session:
        db_session = AnalysisSession(
            symbol=symbol,
            timeframe=timeframe,
            decision=decision,
            confidence=confidence,
            cio_explanation=explanation,
            tech_analyst=reports["technical_analyst"],
            order_flow_analyst=reports["order_flow_analyst"],
            macro_analyst=reports["macro_analyst"],
            news_analyst=reports["news_analyst"],
            devils_advocate=reports["devils_advocate"],
            risk_manager=reports["risk_manager"],
            pre_mortem_analyst=reports.get("pre_mortem_analyst"),
            outcome=outcome_val,
            entry_price=entry_val,
            target_price=target_val,
            stop_price=stop_val,
            regime=extra_context.get("regime") if extra_context else None,
            funding=extra_context.get("funding_rate") if extra_context else None,
            oi=extra_context.get("open_interest") if extra_context else None,
            liquidations=extra_context.get("liquidations") if extra_context else None,
            trend=ticker.get("trend_status"),
            rr=float(risk_idea.get("risk_reward")) if risk_idea else None,
            market_conditions={
                "price": ticker.get("lastPrice"),
                "trend": ticker.get("trend_status"),
                "macro": macro_data,
                "calendar_events": calendar_events,
                "historical_stats": historical_stats,
                "macro_blockout": macro_blockout,
                "market_regime": extra_context.get("regime") if extra_context else None,
            }
        )
        session.add(db_session)
        await session.commit()

    # Write report to reports/ dir as fallback/history
    project_root = Path(__file__).resolve().parents[3]
    reports_dir = project_root / "reports"
    reports_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_filename = f"apex_report_{symbol}_{timeframe}_{timestamp}.md"
    report_path = reports_dir / report_filename

    report_header = (
        f"# APEX Tier-3 CIO Report\n"
        f"**Symbol**: {symbol} | **Timeframe**: {timeframe} | **Date**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"**Decision**: {decision} | **Confidence**: {confidence}%\n\n"
        f"## CIO Executive Summary\n{explanation}\n\n"
        f"---\n\n"
    )
    report_full = report_header + report_md
    report_path.write_text(report_full, encoding="utf-8")

    return {
        "decision": decision,
        "confidence_pct": confidence,
        "sentiment": "NEUTRAL",  # Maintained for backwards compatibility in UI
        "justification": explanation,
        "report_md": report_md,
        "agent_reports": reports,
        "historical_stats": historical_stats,
        "calendar_events": calendar_events,
        "macro_blockout": macro_blockout,
        "dynamic_weights": dynamic_weights,
        "suggested_entry": suggested_entry,
        "suggested_stop": suggested_stop,
        "suggested_targets": suggested_targets,
    }
