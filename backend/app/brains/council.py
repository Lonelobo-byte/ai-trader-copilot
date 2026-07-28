"""Institutional investment committee orchestration.

This module orchestrates the full AI-driven analysis pipeline:
  1. DataAggregator fetches all market intelligence
  2. FeatureEngine computes quant features
  3. Independent deterministic specialist engines publish evidence reports
  4. Adversarial Review and Risk Committee apply non-overridable controls
  5. One optional AI CIO writes the final memo within those controls

The language model is a synthesis layer, never the source of measurements or
allocation authority.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.data_sources.data_aggregator import fetch_market_intelligence
from app.quant.feature_engine import compute_quant_features
from app.brains.prompts.loader import load_prompt
from app.ai_client import AIRequestConfig, ai_is_configured, build_async_ai_client, get_model_for_task, safe_async_chat_completion
from app.ai_budget import AIBudgetExceededError, reserve_platform_ai_budget
from app.settings import Settings, get_settings
from app.quant.engine import build_quantitative_assessment
from app.institutional.committee import (
    apply_cio_policy,
    build_deterministic_cio_decision,
    build_institutional_dossier,
    load_portfolio_state,
)

logger = logging.getLogger(__name__)

# Avoid repeatedly calling a judge-model slug that the provider has declared
# unavailable. Transient rate limits and transport failures are not cached.
_PERMANENT_CIO_MODEL_FAILURES: dict[str, str] = {}


# ── Agent runner ─────────────────────────────────────────────────────────────


async def _call_agent(
    system_prompt: str,
    user_prompt: str,
    settings: Settings,
    task: str = "scanner",
    agent_name: str = "unknown",
    ai_override: AIRequestConfig | None = None,
) -> dict[str, Any]:
    """Call a single AI agent and return parsed JSON. Gracefully handles 429 rate limits."""
    client = build_async_ai_client(settings, ai_override)
    if not client:
        return {"error": "AI client not configured.", "bias": "NEUTRAL", "conviction": 0}

    model = get_model_for_task(settings, task, ai_override)
    # The configured cap is a hard cap on provider requests, including retry
    # attempts. Keeping retries within it prevents a single analysis from
    # spending more than the operator explicitly allowed.
    max_attempts = max(1, int(settings.ai_max_calls_per_analysis))
    for attempt in range(1, max_attempts + 1):
        try:
            # Stagger startup of parallel agents to spread load
            if attempt == 1:
                import random
                await asyncio.sleep(random.uniform(0.05, 1.5))

            completion = await safe_async_chat_completion(
                client=client,
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.2,
                max_retries=0,
            )
            content = completion.choices[0].message.content or "{}"
            from app.utils.json_helper import loads_repaired
            result = loads_repaired(content)
            logger.info(f"Agent [{agent_name}] completed: bias={result.get('bias', 'N/A')}, conviction={result.get('conviction', 'N/A')}")
            return result
        except Exception as exc:
            is_quota_exhausted = "free-models-per-day" in str(exc) or "quota exceeded" in str(exc).lower() or "credit limit" in str(exc).lower()
            if is_quota_exhausted:
                logger.error(f"OpenRouter Daily Free Limit Exceeded: {exc}. Please add credits to your OpenRouter account or switch to a paid model.")
                return {"error": "OpenRouter free-tier daily request limit exceeded.", "bias": "NEUTRAL", "conviction": 0}

            is_rate_limit = "429" in str(exc) or "rate limit" in str(exc).lower() or "too many requests" in str(exc).lower()
            if is_rate_limit and attempt < max_attempts:
                import random
                sleep_sec = (5 * attempt) + random.uniform(3.0, 7.0)
                logger.warning(f"Agent [{agent_name}] rate limited (429). Retrying in {sleep_sec:.2f}s... (Attempt {attempt}/{max_attempts})")
                await asyncio.sleep(sleep_sec)
                continue

            logger.error(f"Agent [{agent_name}] failed: {exc}", exc_info=True)
            return {"error": str(exc), "bias": "NEUTRAL", "conviction": 0}


def _truncate_for_prompt(data: Any, max_chars: int = 3000) -> str:
    """JSON-serialize data, truncating if too large for token efficiency."""
    raw = json.dumps(data, default=str)
    if len(raw) <= max_chars:
        return raw
    return raw[:max_chars] + "...(truncated)"


async def _run_institutional_cio(
    symbol: str,
    timeframe: str,
    dossier: dict,
    settings: Settings,
    ai_override: AIRequestConfig | None = None,
) -> dict:
    """Ask one CIO to synthesize measured engine reports; it has no veto power."""
    model = get_model_for_task(settings, "judge", ai_override)
    if model in _PERMANENT_CIO_MODEL_FAILURES:
        return {"error": _PERMANENT_CIO_MODEL_FAILURES[model]}
    prompt = load_prompt("institutional_cio")
    user = (
        f"Symbol: {symbol} ({timeframe})\n"
        f"Institutional Dossier: {_truncate_for_prompt(dossier, 14000)}\n"
    )
    result = await _call_agent(prompt, user, settings, task="judge", agent_name="institutional_cio", ai_override=ai_override)
    error = str(result.get("error", ""))
    if error and ("404" in error or "unavailable" in error.lower() or "not found" in error.lower()):
        _PERMANENT_CIO_MODEL_FAILURES[model] = error
    return result


# ── Committee orchestrator ──────────────────────────────────────────────────


async def run_ai_council(
    symbol: str,
    timeframe: str = "15m",
    settings: Settings | None = None,
    intelligence: dict[str, Any] | None = None,
    ai_override: AIRequestConfig | None = None,
) -> dict[str, Any]:
    """Run the institutional committee analysis.

    Flow:
      1. Fetch ALL market intelligence concurrently
      2. Compute ALL quant features
      3. Run independent deterministic specialist engines
      4. Apply adversarial and risk vetoes
      5. Ask one optional CIO model to synthesize the evidence
    """
    settings = settings or get_settings()
    start_time = datetime.now(timezone.utc)
    logger.info(f"🧠 AI Council convening for {symbol}/{timeframe}...")

    # ── Step 1: Fetch market intelligence ────────────────────────────────
    intelligence = intelligence or await fetch_market_intelligence(symbol, timeframe, settings)
    logger.info(
        f"📊 Data layer: {intelligence['meta']['sources_available'].__len__()}/"
        f"{intelligence['meta']['total_sources']} sources in "
        f"{intelligence['meta']['fetch_time_ms']}ms"
    )

    # ── Step 2: Compute quant features ───────────────────────────────────
    features = compute_quant_features(intelligence)
    logger.info(f"🔬 Feature engine computed: regime={features.get('volatility', {}).get('regime', 'N/A')}")

    # Step 3: Independent deterministic engines create the dossier. Only the
    # final CIO narrative may use an LLM; evidence and vetoes remain code-owned.
    calendar_events = intelligence.get("calendar", [])
    blockout = _check_macro_blockout(calendar_events)
    historical_stats = await _fetch_historical_stats(symbol, features, intelligence)
    portfolio_state = await load_portfolio_state(settings)
    quantitative = build_quantitative_assessment(
        intelligence.get("candles", []),
        intelligence.get("order_book", {"bids": [], "asks": []}),
        account_value=settings.default_account_size_usd,
        max_drawdown_pct=settings.max_drawdown_pct,
        max_gross_exposure_pct=settings.max_gross_exposure_pct,
        symbol=symbol,
        timeframe=timeframe,
        context_features=features,
        current_drawdown_pct=float(portfolio_state.get("current_drawdown_pct") or 0.0),
        gross_exposure_pct=float(portfolio_state.get("gross_exposure_pct") or 0.0),
    )
    dossier = build_institutional_dossier(
        symbol=symbol,
        timeframe=timeframe,
        features=features,
        intelligence=intelligence,
        quantitative=quantitative,
        portfolio_state=portfolio_state,
        macro_blockout=blockout,
        settings=settings,
    )
    dossier["historical_stats"] = historical_stats
    dossier["calendar_events"] = calendar_events

    ai_enabled = int(settings.ai_max_calls_per_analysis) > 0 and ai_is_configured(settings, ai_override)
    if ai_enabled and ai_override is None:
        try:
            await reserve_platform_ai_budget(settings)
        except AIBudgetExceededError as exc:
            logger.warning("Platform AI request blocked by budget: %s", exc)
            ai_enabled = False
            ai_budget_error = str(exc)
        else:
            ai_budget_error = ""
    else:
        ai_budget_error = ""

    raw_cio_result = (
        await _run_institutional_cio(symbol, timeframe, dossier, settings, ai_override)
        if ai_enabled
        else build_deterministic_cio_decision(
            dossier,
            narrative_error=ai_budget_error or "AI synthesis is disabled or no configured AI connection is available.",
        )
    )
    if raw_cio_result.get("error"):
        raw_cio_result = build_deterministic_cio_decision(
            dossier,
            narrative_error=str(raw_cio_result["error"]),
        )
    cio_result = apply_cio_policy(raw_cio_result, dossier)

    reports = dict(dossier["engines"])
    reports["historical_stats"] = historical_stats
    reports["calendar_events"] = calendar_events
    reports["macro_blockout"] = blockout
    voting_reports = [
        report for name, report in dossier["engines"].items()
        if name not in {"risk_committee", "adversarial_review_engine"}
    ]
    bullish = sum(report.get("bias") == "BULLISH" for report in voting_reports)
    bearish = sum(report.get("bias") == "BEARISH" for report in voting_reports)
    neutral = sum(report.get("bias") == "NEUTRAL" for report in voting_reports)
    convictions = [float(report.get("confidence_pct") or 0.0) for report in voting_reports if report.get("status") != "UNAVAILABLE"]
    avg_conviction = round(sum(convictions) / len(convictions)) if convictions else 0

    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()

    # ── Assemble final result ────────────────────────────────────────────
    result: dict[str, Any] = {
        # CIO verdict
        "decision": cio_result.get("decision", "HOLD"),
        "confidence_pct": cio_result.get("confidence_pct", 0),
        "trade_grade": cio_result.get("trade_grade", "F"),
        "explanation": cio_result.get("explanation", ""),
        "report_md": cio_result.get("report_md", ""),
        "risk_warnings": cio_result.get("risk_warnings", []),
        "investment_memo": cio_result.get("investment_memo", {}),
        "institutional_dossier": dossier,
        "committee_controls": cio_result.get("committee_controls", {}),
        "quantitative_assessment": quantitative,
        # Trade plan (if actionable)
        "suggested_entry": cio_result.get("suggested_entry"),
        "suggested_stop": cio_result.get("suggested_stop"),
        "suggested_targets": cio_result.get("suggested_targets"),
        # Agent reports
        "agent_reports": reports,
        "agent_agreement": {
            "bullish": bullish,
            "bearish": bearish,
            "neutral": neutral,
            "avg_conviction": avg_conviction,
        },
        # Context
        "symbol": symbol,
        "timeframe": timeframe,
        "current_price": features.get("current_price"),
        "historical_stats": historical_stats,
        "calendar_events": calendar_events,
        "macro_blockout": blockout,
        "data_quality": features.get("data_quality", {}),
        # Quant features summary
        "quant_features": {
            "trend": features.get("trend", {}).get("primary", {}),
            "mtf_alignment": features.get("trend", {}).get("mtf_alignment"),
            "momentum": {
                "rsi": features.get("momentum", {}).get("rsi"),
                "macd": features.get("momentum", {}).get("macd"),
                "stoch_rsi": features.get("momentum", {}).get("stoch_rsi"),
            },
            "volatility": features.get("volatility", {}),
            "volume": features.get("volume", {}),
            "regime": features.get("regime", {}),
            "derivatives": {
                "funding_rate": features.get("derivatives", {}).get("funding_rate"),
                "open_interest": features.get("derivatives", {}).get("open_interest"),
                "squeeze": features.get("derivatives", {}).get("squeeze"),
            },
            "sentiment": features.get("sentiment", {}),
            "cross_asset": features.get("cross_asset", {}),
        },
        # Internal hand-off for the signal pipeline. The public summary above
        # stays compact, while downstream lifecycle/risk code receives the
        # exact same full feature snapshot used by the council.
        "full_quant_features": features,
        # Meta
        "meta": {
            "analysis_time_seconds": round(elapsed, 2),
            "data_sources": intelligence.get("meta", {}),
            "timestamp": start_time.isoformat() + "Z",
            "model": get_model_for_task(settings, "judge", ai_override),
            "engine": "institutional_committee_v1",
        },
    }

    logger.info(
        f"🏁 AI Council verdict for {symbol}/{timeframe}: "
        f"{result['decision']} @ {result['confidence_pct']}% "
        f"(Grade: {result['trade_grade']}) "
        f"[Votes: {bullish}B/{bearish}S/{neutral}N, Avg conviction: {avg_conviction}%] "
        f"in {elapsed:.1f}s"
    )

    return result


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _fetch_historical_stats(
    symbol: str,
    features: dict[str, Any],
    intelligence: dict[str, Any],
) -> dict[str, Any]:
    """Query the DB for similar historical setups (RAG)."""
    try:
        from app.db.database import AsyncSessionLocal, init_db
        from app.db.models import AnalysisSession
        from app.brains.ai_analysis import compute_session_similarity
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            stmt = select(AnalysisSession).where(
                AnalysisSession.outcome.in_(["SUCCESS", "FAILURE"])
            )
            res = await db.execute(stmt)
            sessions = res.scalars().all()

            if not sessions:
                return {"similar_setups_count": 0, "historical_win_rate": 50.0}

            regime = features.get("volatility", {}).get("regime", "RANGING")
            trend = features.get("trend", {}).get("primary", {}).get("status", "sideways_or_mixed")
            funding = features.get("derivatives", {}).get("funding_rate", 0.0)
            oi = features.get("derivatives", {}).get("open_interest", 0.0)

            scored = []
            for s in sessions:
                sim = compute_session_similarity(
                    s, symbol, trend, funding, oi, "HOLD", regime
                )
                scored.append((sim, s))

            scored.sort(key=lambda x: x[0], reverse=True)
            top = [s for _, s in scored[:20]]
            count = len(top)
            if count > 0:
                wins = sum(1 for s in top if s.outcome == "SUCCESS")
                win_rate = (wins / count) * 100
            else:
                win_rate = 50.0

            return {
                "similar_setups_count": count,
                "historical_win_rate": round(win_rate, 2),
            }
    except Exception as exc:
        logger.warning(f"Historical stats query failed: {exc}")
        return {"similar_setups_count": 0, "historical_win_rate": 50.0}


def _check_macro_blockout(calendar_events: list[dict]) -> dict[str, Any]:
    """Check if a high-impact macro event is imminent."""
    now = datetime.now(timezone.utc)
    for event in calendar_events:
        if event.get("importance") == "HIGH":
            try:
                ev_time_str = event.get("time", "").replace("Z", "")
                ev_time = datetime.fromisoformat(ev_time_str)
                diff_mins = (ev_time - now).total_seconds() / 60.0
                if 0 <= diff_mins <= 120:
                    return {
                        "active": True,
                        "reason": f"High-impact event '{event.get('title')}' in {int(diff_mins)} mins.",
                    }
            except Exception:
                pass
    return {"active": False, "reason": ""}
