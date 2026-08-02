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
    final_validation_fingerprint,
    load_portfolio_state,
)

logger = logging.getLogger(__name__)

# Avoid repeatedly calling a judge-model slug that the provider has declared
# unavailable. Transient rate limits and transport failures are not cached.
_PERMANENT_CIO_MODEL_FAILURES: dict[str, str] = {}
_MAX_PERMANENT_MODEL_FAILURES = 128


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


def _cio_prompt_dossier(dossier: dict[str, Any]) -> str:
    """Build a bounded, valid JSON dossier without dropping code-owned controls."""
    def prompt_value(value: Any, max_chars: int = 600) -> Any:
        raw = json.dumps(value, default=str)
        if len(raw) <= max_chars:
            return value
        return raw[:max_chars] + "...(value truncated)"

    engines: dict[str, Any] = {}
    for name, report in (dossier.get("engines") or {}).items():
        engines[name] = {
            "status": report.get("status"),
            "bias": report.get("bias"),
            "confidence_pct": report.get("confidence_pct"),
            "evidence": [
                {
                    "metric": item.get("metric"),
                    "value": prompt_value(item.get("value")),
                    "source": item.get("source"),
                }
                for item in (report.get("evidence") or [])[:12]
            ],
            "contradictory_evidence": list(report.get("contradictory_evidence") or [])[:8],
            "unknowns": list(report.get("unknowns") or [])[:8],
            "limitations": list(report.get("limitations") or [])[:5],
        }
    projection = {
        "architecture": dossier.get("architecture"),
        "symbol": dossier.get("symbol"),
        "timeframe": dossier.get("timeframe"),
        "as_of": dossier.get("as_of"),
        "policy": dossier.get("policy", {}),
        "data_quality": dossier.get("data_quality", {}),
        "evidence_manifest": dossier.get("evidence_manifest", {}),
        "provisional_thesis": dossier.get("provisional_thesis", {}),
        "adversarial_review": dossier.get("adversarial_review", {}),
        "risk_committee": dossier.get("risk_committee", {}),
        "historical_stats": dossier.get("historical_stats", {}),
        "calendar_events": list(dossier.get("calendar_events") or [])[:5],
        "engines": engines,
        "final_trade_setup": dossier.get("final_trade_setup"),
        "final_live_confirmation": dossier.get("final_live_confirmation"),
    }
    return json.dumps(projection, default=str)


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
        f"Institutional Dossier: {_cio_prompt_dossier(dossier)}\n"
    )
    result = await _call_agent(prompt, user, settings, task="judge", agent_name="institutional_cio", ai_override=ai_override)
    error = str(result.get("error", ""))
    if error and ("404" in error or "unavailable" in error.lower() or "not found" in error.lower()):
        if len(_PERMANENT_CIO_MODEL_FAILURES) >= _MAX_PERMANENT_MODEL_FAILURES and model not in _PERMANENT_CIO_MODEL_FAILURES:
            _PERMANENT_CIO_MODEL_FAILURES.pop(next(iter(_PERMANENT_CIO_MODEL_FAILURES)), None)
        _PERMANENT_CIO_MODEL_FAILURES[model] = error
    return result


async def _resolve_cio_synthesis(
    symbol: str,
    timeframe: str,
    dossier: dict[str, Any],
    settings: Settings,
    ai_override: AIRequestConfig | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the one permitted synthesis call and report truthful provenance."""
    ai_enabled = int(settings.ai_max_calls_per_analysis) > 0 and ai_is_configured(settings, ai_override)
    ai_budget_error = ""
    if ai_enabled and ai_override is None:
        try:
            await reserve_platform_ai_budget(settings)
        except AIBudgetExceededError as exc:
            logger.warning("Platform AI request blocked by budget: %s", exc)
            ai_enabled = False
            ai_budget_error = str(exc)

    ai_attempted = ai_enabled
    raw_cio_result = (
        await _run_institutional_cio(symbol, timeframe, dossier, settings, ai_override)
        if ai_enabled
        else build_deterministic_cio_decision(
            dossier,
            narrative_error=ai_budget_error or "AI synthesis is disabled or no configured AI connection is available.",
        )
    )
    ai_failure_reason = ""
    if raw_cio_result.get("error"):
        ai_failure_reason = str(raw_cio_result["error"])
        raw_cio_result = build_deterministic_cio_decision(
            dossier,
            narrative_error=ai_failure_reason,
        )
    ai_synthesis_used = bool(ai_attempted and not ai_failure_reason)
    if not ai_synthesis_used and not ai_failure_reason:
        ai_failure_reason = ai_budget_error or "No configured AI connection was available."
    cio_result = apply_cio_policy(raw_cio_result, dossier)
    provenance = {
        "attempted": ai_attempted,
        "synthesis_used": ai_synthesis_used,
        "deterministic_fallback_used": not ai_synthesis_used,
        "failure_reason": ai_failure_reason or None,
        "provider": settings.ai_provider if ai_synthesis_used else None,
        "model": get_model_for_task(settings, "judge", ai_override) if ai_synthesis_used else None,
        "validation_scope": (
            "dossier_trade_plan_live_confirmation"
            if dossier.get("final_trade_setup") is not None
            and dossier.get("final_live_confirmation") is not None
            else "dossier_only"
        ),
    }
    return cio_result, provenance


# ── Committee orchestrator ──────────────────────────────────────────────────


async def run_ai_council(
    symbol: str,
    timeframe: str = "15m",
    settings: Settings | None = None,
    intelligence: dict[str, Any] | None = None,
    ai_override: AIRequestConfig | None = None,
    defer_ai_validation: bool = False,
    precomputed_features: dict[str, Any] | None = None,
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
    features = precomputed_features or await asyncio.to_thread(
        compute_quant_features,
        intelligence,
    )
    logger.info(f"🔬 Feature engine computed: regime={features.get('volatility', {}).get('regime', 'N/A')}")

    # Step 3: Independent deterministic engines create the dossier. Only the
    # final CIO narrative may use an LLM; evidence and vetoes remain code-owned.
    calendar_events = intelligence.get("calendar", [])
    blockout = _check_macro_blockout(calendar_events)
    historical_stats = await _fetch_historical_stats(symbol, features, intelligence)
    portfolio_state = await load_portfolio_state(settings)
    quantitative = await asyncio.to_thread(
        build_quantitative_assessment,
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
    dossier = await asyncio.to_thread(
        build_institutional_dossier,
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

    if defer_ai_validation:
        raw_cio_result = build_deterministic_cio_decision(
            dossier,
            narrative_error="Final AI validation is deferred until the trade plan and live confirmation are complete.",
        )
        cio_result = apply_cio_policy(raw_cio_result, dossier)
        ai_provenance = {
            "attempted": False,
            "synthesis_used": False,
            "deterministic_fallback_used": True,
            "failure_reason": None,
            "provider": None,
            "model": None,
            "pending_final_validation": True,
            "validation_scope": "pending_trade_plan_live_confirmation",
        }
    else:
        cio_result, ai_provenance = await _resolve_cio_synthesis(
            symbol, timeframe, dossier, settings, ai_override
        )

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
        "evidence_manifest": dossier.get("evidence_manifest", {}),
        "committee_controls": cio_result.get("committee_controls", {}),
        "ai_provenance": ai_provenance,
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
            "model": ai_provenance.get("model"),
            "engine": "institutional_committee_v1",
            "synthesis_mode": (
                "pending_final_validation"
                if ai_provenance.get("pending_final_validation")
                else "ai"
                if ai_provenance.get("synthesis_used")
                else "deterministic_fallback"
            ),
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


async def finalize_ai_council_validation(
    council_result: dict[str, Any],
    trade_setup: dict[str, Any],
    live_confirmation: dict[str, Any],
    settings: Settings,
    ai_override: AIRequestConfig | None = None,
) -> dict[str, Any]:
    """Run final synthesis over the exact plan and live gate being published."""
    original_decision = str(council_result.get("decision") or "WAIT").upper()
    original_confidence = council_result.get("confidence_pct")
    original_grade = council_result.get("trade_grade")
    dossier = dict(council_result.get("institutional_dossier") or {})
    live_evidence = live_confirmation.get("live_evidence") or {}
    scenarios = live_confirmation.get("scenarios") or {}

    def compact_scenario(value: Any) -> dict[str, Any]:
        scenario = value if isinstance(value, dict) else {}
        return {
            key: scenario.get(key)
            for key in (
                "passed", "candidate", "status", "reason", "playbook",
                "higher_timeframe_aligned", "higher_timeframe_state",
                "market_story_state", "market_story_actionable",
                "market_story_reason", "selected_event",
            )
        }

    def compact_story(value: Any) -> dict[str, Any]:
        story = value if isinstance(value, dict) else {}
        return {
            key: story.get(key)
            for key in (
                "available", "as_of_close_time", "current_state", "setup_state",
                "what_happened", "what_is_happening", "what_may_happen_next",
                "latest_event", "latest_liquidity_event", "actionability",
                "directional_view", "higher_directional_view", "alignment",
            )
        }

    metrics = live_confirmation.get("metrics") or {}

    dossier["final_trade_setup"] = {
        "status": trade_setup.get("status"),
        "side": trade_setup.get("side"),
        "setup_type": trade_setup.get("setup_type"),
        "execution_permitted": trade_setup.get("execution_permitted"),
        "entry": trade_setup.get("entry"),
        "stop": trade_setup.get("stop"),
        "targets": trade_setup.get("targets"),
        "position": trade_setup.get("position"),
        "allocation_tier": trade_setup.get("allocation_tier"),
        "committee_restrictions": trade_setup.get("committee_restrictions"),
        "leverage": trade_setup.get("leverage"),
        "liquidity_objective": trade_setup.get("liquidity_objective"),
        "remaining_reward": trade_setup.get("remaining_reward"),
        "market_story": trade_setup.get("market_story"),
        "rules": trade_setup.get("rules"),
    }
    dossier["final_live_confirmation"] = {
        "passed": live_confirmation.get("passed"),
        "status": live_confirmation.get("status"),
        "reason": live_confirmation.get("reason"),
        "direction": live_confirmation.get("direction"),
        "structure_checks": live_confirmation.get("structure_checks"),
        "live_checks": live_confirmation.get("live_checks"),
        "risk_flags": live_confirmation.get("risk_flags"),
        "metrics": {
            key: metrics.get(key)
            for key in (
                "playbook", "primary_phase", "higher_phase",
                "primary_direction", "higher_direction", "rvol", "body_ratio",
                "event_rvol", "event_body_ratio", "event_close_location",
                "sweep", "vwap", "volume_profile", "bos", "choch",
                "selected_structure_event", "structure_confirmation",
                "tactical_structure_confirmation", "structure_story",
            )
        },
        "structure_story": {
            "setup_state": (live_confirmation.get("structure_story") or {}).get("setup_state"),
            "primary": compact_story((live_confirmation.get("structure_story") or {}).get("primary")),
            "higher": compact_story((live_confirmation.get("structure_story") or {}).get("higher")),
            "directional_view": (live_confirmation.get("structure_story") or {}).get("directional_view"),
            "higher_directional_view": (live_confirmation.get("structure_story") or {}).get("higher_directional_view"),
            "alignment": (live_confirmation.get("structure_story") or {}).get("alignment"),
        },
        "live_evidence": {
            key: live_evidence.get(key)
            for key in (
                "checks", "required_checks", "live_points",
                "market_story_live_location", "execution_capacity",
                "displayed_liquidity_stability", "actual_flow_evidence",
                "supporting_warnings",
            )
        },
        "publication_coverage": live_confirmation.get("publication_coverage"),
        "scenarios": {
            "institutional": compact_scenario(scenarios.get("institutional")),
            "tactical": compact_scenario(scenarios.get("tactical")),
        },
    }
    cio_result, provenance = await _resolve_cio_synthesis(
        str(council_result.get("symbol") or ""),
        str(council_result.get("timeframe") or ""),
        dossier,
        settings,
        ai_override,
    )
    resolved_decision = str(cio_result.get("decision") or "WAIT").upper()
    if original_decision not in {"BUY_WATCH", "SELL_WATCH"} or resolved_decision != original_decision:
        # Final AI is a veto/synthesis layer. It cannot create a setup that the
        # deterministic committee did not propose or switch its measured side.
        cio_result["decision"] = "WAIT"
        cio_result["confidence_pct"] = min(
            float(cio_result.get("confidence_pct") or 0.0),
            55.0,
        )
    else:
        allocation_tier = str((dossier.get("risk_committee") or {}).get("allocation_tier") or "")
        release_threshold = 60.0 if allocation_tier == "CONDITIONAL_MANUAL_REVIEW" else 65.0
        if float(cio_result.get("confidence_pct") or 0.0) < release_threshold:
            cio_result["decision"] = "WAIT"
        else:
            # The model reviewed this exact code-owned plan. Preserve its
            # sizing inputs; the model may approve or veto, never reshape it.
            cio_result["confidence_pct"] = original_confidence
            cio_result["trade_grade"] = original_grade
    provenance["evidence_fingerprint"] = final_validation_fingerprint(
        trade_setup,
        live_confirmation,
    )
    for key in (
        "decision", "confidence_pct", "trade_grade", "explanation", "report_md",
        "risk_warnings", "investment_memo", "committee_controls", "suggested_entry",
        "suggested_stop", "suggested_targets", "institutional_dossier",
    ):
        council_result[key] = cio_result.get(key)
    council_result["ai_provenance"] = provenance
    council_result["meta"] = {
        **(council_result.get("meta") or {}),
        "model": provenance.get("model"),
        "synthesis_mode": "ai" if provenance.get("synthesis_used") else "deterministic_fallback",
    }
    return council_result


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _fetch_historical_stats(
    symbol: str,
    features: dict[str, Any],
    intelligence: dict[str, Any],
) -> dict[str, Any]:
    """Query the DB for similar historical setups (RAG)."""
    try:
        from app.db.database import AsyncSessionLocal
        from app.db.models import TradeSignal
        from app.brains.signal_lifecycle import TERMINAL_SIGNAL_STATUSES
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            # The live product writes TradeSignal records, not legacy
            # AnalysisSession rows. Keep this bounded so council calls do not
            # load the full lifetime ledger into memory.
            stmt = (
                select(TradeSignal)
                .where(TradeSignal.symbol == symbol, TradeSignal.status.in_(TERMINAL_SIGNAL_STATUSES))
                .order_by(TradeSignal.closed_at.desc(), TradeSignal.id.desc())
                .limit(200)
            )
            res = await db.execute(stmt)
            signals = res.scalars().all()

            if not signals:
                return {"similar_setups_count": 0, "historical_win_rate": 50.0}

            current_context = features.get("market_context", {}) or {}
            current_direction = str(current_context.get("direction", "WAIT"))
            # The causal direction is durable in the signal context. Prefer
            # comparable Bare Eye contexts, then use the recent bounded
            # sample as a fallback for pre-migration records.
            resolved = [
                signal
                for signal in signals
                if signal.status in {"COMPLETED", "STOPPED_OUT"}
                or (signal.status == "INVALIDATED" and signal.entry_price is not None)
            ]
            top = [
                signal for signal in signals
                if signal in resolved
                if str(
                    ((signal.context or {}).get("causal_market_context") or {}).get(
                        "direction",
                        "WAIT",
                    )
                ) == current_direction
            ][:20] or resolved[:20]
            count = len(top)
            if count > 0:
                wins = sum(1 for s in top if s.status == "COMPLETED")
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
