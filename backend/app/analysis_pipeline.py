"""Unified analysis pipeline orchestrated by the AI Council.

This module replaces the old deterministic rules and decision trees with the
new AI-first architecture. It acts as the adapter layer for REST/WebSocket
routes and backtests.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from app.brains.council import run_ai_council
from app.ai_client import AIRequestConfig
from app.brains.signal_builder import build_ai_driven_trade_setup, evaluate_ai_driven_approval
from app.quant.feature_engine import compute_quant_features
from app.quant.live_confirmation import verify_main_signal_snapshot
from app.signal_service import reconcile_signal
from app.data_sources.data_aggregator import fetch_market_intelligence

logger = logging.getLogger(__name__)

# Module-level cache persists a deliberation only for intra-candle dashboard
# updates. A completed candle always receives a fresh committee review.
_ai_council_cache: dict[str, dict[str, Any]] = {}


def _build_analysis_snapshot(
    *, symbol: str, timeframe: str, candles: list[Any], ticker: dict[str, Any],
    intelligence: dict[str, Any], features: dict[str, Any], quantitative: dict[str, Any],
    cio_result: dict[str, Any], trade_setup: dict[str, Any], signal_monitor: dict[str, Any],
    approval: dict[str, Any], data_freshness: dict[str, Any], liquidity: dict[str, Any],
) -> dict[str, Any]:
    """Create the one authoritative data contract for an analysis render.

    All dashboard cards are projections of this object.  It deliberately
    contains the exact objects used by the council, risk plan, live gate and
    market-context score, rather than independently re-fetching or
    recomputing a value for a specific widget.
    """
    captured_at = datetime.now(timezone.utc).isoformat()
    latest_candle = candles[-1] if candles else None
    primary_candle = {
        "open_time": getattr(latest_candle, "open_time", None),
        "close_time": getattr(latest_candle, "close_time", None),
        "close": getattr(latest_candle, "close", None),
    }
    fingerprint = "|".join(str(value) for value in (
        symbol, timeframe, primary_candle["open_time"], primary_candle["close_time"],
        primary_candle["close"], ticker.get("lastPrice"), captured_at,
    ))
    snapshot_id = sha256(fingerprint.encode("utf-8")).hexdigest()[:16]
    derivatives = features.get("derivatives", {}) or {}
    return {
        "id": snapshot_id,
        "schema_version": "analysis_snapshot.v1",
        "captured_at": captured_at,
        "symbol": symbol,
        "timeframe": timeframe,
        "primary_candle": primary_candle,
        "source_coverage": features.get("data_quality", {}),
        "source_meta": intelligence.get("meta", {}),
        "market": {
            "last_price": ticker.get("lastPrice"),
            "price_change_pct_24h": ticker.get("priceChangePercent"),
            "quote_volume_24h": ticker.get("quoteVolume"),
            "bid": ticker.get("bidPrice"),
            "ask": ticker.get("askPrice"),
        },
        "causal": {
            "market_context": features.get("market_context", {}),
            "market_structure": features.get("market_structure", {}),
            "liquidity_map": features.get("liquidity_map", {}),
            "liquidity_sweep": features.get("sweep", {}),
            "positioning": features.get("positioning", {}),
            "volatility_context": features.get("volatility_context", {}),
            "volume_profile": features.get("volume_profile", {}),
            "vwap_context": features.get("vwap_context", {}),
        },
        "execution": {
            "order_book_pressure": features.get("order_book", {}),
            "derivatives": {
                "taker_buy_sell_volume": derivatives.get("taker_volume", {}),
                "oi_history": derivatives.get("oi_history", {}),
                "oi_delta": derivatives.get("oi_delta", {}),
                "funding_rate": derivatives.get("funding_rate"),
                "open_interest": derivatives.get("open_interest"),
                "liquidations": derivatives.get("liquidations", {}),
            },
            "trade_setup": trade_setup,
            "signal_monitor": signal_monitor,
            "approval": approval,
            "live_confirmation": cio_result.get("live_confirmation"),
            "data_freshness": data_freshness,
            "liquidity": liquidity,
        },
        "telemetry": {
            "regime": (features.get("volatility", {}) or {}).get("regime", "UNKNOWN"),
            "risk_appetite_proxy": intelligence.get("global_liquidity", {}),
            "sentiment": features.get("sentiment", {}),
        },
        "research": {
            "news_sentiment": {"token": intelligence.get("news", []), "global": intelligence.get("global_news", [])},
            "calendar_events": (cio_result.get("agent_reports", {}) or {}).get("calendar_events", []),
            "macro_blockout": cio_result.get("macro_blockout", {"active": False, "reason": ""}),
        },
        "quantitative": quantitative,
    }

async def run_full_analysis(
    *,
    symbol: str,
    timeframe: str,
    candles: list[Any],
    ticker: dict[str, Any],
    order_book_raw: dict[str, Any],
    settings: Any,
    use_ai: bool = True,
    # WebSocket-specific parameters (maintained for backwards compatibility)
    is_new_candle: bool = False,
    is_init_event: bool = False,
    last_ai_open_time: int = 0,
    market_intelligence: dict[str, Any] | None = None,
    reconcile_signals: bool = True,
    ai_override: AIRequestConfig | None = None,
    ai_cache_key: str = "platform",
) -> tuple[dict[str, Any], int]:
    """Run the analysis pipeline and return (payload, updated_last_ai_open_time).

    If use_ai is True, runs the full AI council.
    If use_ai is False, runs a high-fidelity statistical/quant heuristic.
    """
    logger.info(f"Convening analysis pipeline for {symbol}/{timeframe} (use_ai={use_ai}, new_candle={is_new_candle}, init={is_init_event})")

    # ── Step 1: Pre-calculate local features ──────────────────────────────
    if market_intelligence is None:
        intel = await fetch_market_intelligence(symbol, timeframe, settings)
    else:
        intel = market_intelligence
    # The explicit route arguments remain authoritative for streaming callers.
    intel["candles"], intel["ticker"], intel["order_book"] = candles, ticker, order_book_raw
    features = compute_quant_features(intel)

    # ── Check if an active signal is already open in the database ────────
    from app.signal_service import get_active_signal
    # Historical research must never read or mutate the live signal ledger.
    active_signal = await get_active_signal(symbol, timeframe) if reconcile_signals else None

    if active_signal:
        logger.info(f"Locking trade plan for {symbol}/{timeframe} — Active signal {active_signal.id} is open in database.")
        macro_blockout = {"active": False, "reason": ""}
        historical_stats = {"similar_setups_count": 0, "historical_win_rate": 50.0}

        # Rebuild the council contract from the columns actually persisted on TradeSignal.
        active_review = active_signal.ai_review or {}
        cio_result = {
            "decision": "BUY_WATCH" if active_signal.side == "LONG" else "SELL_WATCH",
            "confidence_pct": active_signal.confidence,
            "trade_grade": active_review.get("trade_grade", "A"),
            "explanation": active_review.get("explanation") or "Active signal tracking.",
            "report_md": active_review.get("report_md") or "Active signal tracking.",
            "risk_warnings": active_review.get("risk_warnings") or [],
            "suggested_entry": active_signal.entry_reference,
            "suggested_stop": active_signal.stop_initial,
            "suggested_targets": [active_signal.target_1, active_signal.target_2, active_signal.target_3, active_signal.target_runner],
            "agent_reports": active_review.get("agent_reports") or {},
            "agent_agreement": active_review.get("agent_agreement") or {"bullish": 0, "bearish": 0, "neutral": 9},
            "macro_blockout": macro_blockout,
            "historical_stats": historical_stats,
            "quant_features": features,
            "data_quality": features.get("data_quality", {}),
        }
    else:
        # Check Volatility/Volume Ratios for logging
        vol_ratio = features.get("volume", {}).get("volume_ratio", 1.0)
        current_range = 0.0
        if candles:
            try:
                current_range = float(candles[-1].high) - float(candles[-1].low)
            except (AttributeError, ValueError, TypeError, IndexError):
                pass
        atr = features.get("volatility", {}).get("atr", 0.0)
        range_ratio = current_range / atr if atr > 0 else 1.0

        # Never share a cached paid deliberation across user connections.
        cache_key = f"{symbol}_{timeframe}_{ai_cache_key}"
        should_run_ai = use_ai and (
            is_init_event
            or is_new_candle
            or cache_key not in _ai_council_cache
        )

        if should_run_ai:
            logger.info(f"Convening live AI Council for {symbol}/{timeframe} (vol_ratio={vol_ratio:.2f}, range_ratio={range_ratio:.2f})")
            # ``intel`` has already been normalised to the explicit REST or
            # WebSocket candle/ticker/order-book snapshot. Passing the original
            # optional argument here caused the WebSocket path to fetch a second
            # snapshot and let the committee decide on different data.
            cio_result = await run_ai_council(symbol, timeframe, settings, intelligence=intel, ai_override=ai_override)
            features = cio_result.get("full_quant_features") or features
            # Cache the deliberation report
            _ai_council_cache[cache_key] = cio_result
            macro_blockout = cio_result["macro_blockout"]
            historical_stats = cio_result["historical_stats"]
        elif use_ai and cache_key in _ai_council_cache:
            logger.info(f"Reusing cached AI Council deliberation for {symbol}/{timeframe} (cooldown tick)")
            cio_result = _ai_council_cache[cache_key].copy()
            # The narrative may be cached intra-candle, but every numerical
            # field exposed to the dashboard must use this exact snapshot.
            cio_result["quant_features"] = features
            cio_result["full_quant_features"] = features
            cio_result["data_quality"] = features.get("data_quality", {})
            macro_blockout = cio_result.get("macro_blockout", {"active": False, "reason": ""})
            historical_stats = cio_result.get("historical_stats", {"similar_setups_count": 0, "historical_win_rate": 50.0})
        else:
            macro_blockout = {"active": False, "reason": ""}
            historical_stats = {"similar_setups_count": 0, "historical_win_rate": 50.0}

            # Quant heuristic fallback (mainly for fast backtests)
            current_price = features["current_price"]
            atr_val = features.get("volatility", {}).get("atr", current_price * 0.01)
            if not atr_val or atr_val <= 0:
                atr_val = current_price * 0.01
            atr_val = max(atr_val, current_price * 0.001, 1e-6)

            decision = "HOLD"
            suggested_entry = None
            suggested_stop = None
            suggested_targets = None
            confidence = 50.0
            grade = "C"

            context = features.get("market_context", {}) or {}
            context_direction = context.get("direction", "WAIT")
            context_score = float(context.get("score") or 0.0)
            if context.get("status") == "SETUP_CANDIDATE" and context_direction == "LONG":
                decision = "BUY_WATCH"
                suggested_entry = current_price
                suggested_stop = current_price - 1.5 * atr_val
                suggested_targets = [
                    current_price + 1.5 * atr_val,
                    current_price + 3.0 * atr_val,
                    current_price + 4.5 * atr_val,
                    current_price + 7.5 * atr_val,
                ]
                confidence = max(60.0, min(context_score, 85.0))
                grade = "A"
            elif context.get("status") == "SETUP_CANDIDATE" and context_direction == "SHORT":
                decision = "SELL_WATCH"
                suggested_entry = current_price
                suggested_stop = current_price + 1.5 * atr_val
                suggested_targets = [
                    current_price - 1.5 * atr_val,
                    current_price - 3.0 * atr_val,
                    current_price - 4.5 * atr_val,
                    current_price - 7.5 * atr_val,
                ]
                confidence = max(60.0, min(context_score, 85.0))
                grade = "A"

            cio_result = {
                "decision": decision,
                "confidence_pct": confidence,
                "trade_grade": grade,
                "explanation": "Causal market-context fallback decision (use_ai=False). Derived indicators do not vote.",
                "report_md": "Heuristic backtest report.",
                "risk_warnings": [],
                "suggested_entry": suggested_entry,
                "suggested_stop": suggested_stop,
                "suggested_targets": suggested_targets,
                "agent_reports": {},
                "agent_agreement": {"bullish": 3 if decision == "BUY_WATCH" else 0, "bearish": 3 if decision == "SELL_WATCH" else 0, "neutral": 4},
                "macro_blockout": macro_blockout,
                "historical_stats": historical_stats,
                "quant_features": features,
                "data_quality": features.get("data_quality", {}),
            }

    # ── Step 2: Build trade setup and evaluate approval ──────────────────
    trade_setup = build_ai_driven_trade_setup(cio_result, features, settings)
    # A new live signal must pass the same completed-structure and live
    # execution checks that drive Radar's LIVE CHECK PASSED/FAILED badge.
    # Historical backtests do not publish and therefore do not apply this
    # live-only gate.
    if reconcile_signals:
        confirmation_timeframes = {
            "1m": "5m", "5m": "1h", "15m": "4h", "1h": "1d", "4h": "1d", "1d": "1w",
        }
        confirmation_htf = confirmation_timeframes.get(timeframe)
        higher_candles = (intel.get("multi_tf_candles", {}) or {}).get(confirmation_htf, []) if confirmation_htf else []
        direction_side = "LONG" if cio_result.get("decision") == "BUY_WATCH" else "SHORT" if cio_result.get("decision") == "SELL_WATCH" else None
        cio_result["live_confirmation"] = verify_main_signal_snapshot(
            symbol=symbol, timeframe=timeframe, side=direction_side, candles=candles,
            higher_candles=higher_candles, order_book=order_book_raw,
            funding=intel.get("funding", {}) or {}, derivatives=intel.get("derivatives", {}) or {},
        )
    if cio_result.get("institutional_dossier"):
        from app.institutional.committee import build_investment_memo, render_investment_memo
        cio_result["deterministic_trade_plan"] = trade_setup
        memo = build_investment_memo(cio_result, cio_result["institutional_dossier"])
        cio_result["investment_memo"] = memo
        cio_result["report_md"] = render_investment_memo(memo)
    approval = evaluate_ai_driven_approval(cio_result, trade_setup)

    # ── Step 3: Reconcile active signal or publish new one ───────────────
    # If this is WebSocket streaming (is_new_candle tracking), or REST.
    # Reconcile signal logic checks if an active signal is open and advances it,
    # otherwise publishes a new approved one.
    from app.brains.decision_engine import check_data_freshness
    from app.indicators.liquidity import analyze_liquidity
    data_freshness = check_data_freshness(candles, timeframe)
    liquidity = analyze_liquidity(ticker, order_book_raw)

    entry = trade_setup.get("entry") or {}
    stop = trade_setup.get("stop") or {}
    targets = trade_setup.get("targets") or {}
    entry_reference = float(entry.get("reference") or 0.0)
    risk_per_unit = abs(entry_reference - float(stop.get("selected") or 0.0))
    risk_reward = abs(float(targets.get("tp2_2r") or 0.0) - entry_reference) / risk_per_unit if risk_per_unit > 0 else 0.0
    lifecycle_decision = {**cio_result, "confidence": cio_result.get("confidence_pct", 0.0)}
    risk_idea = {
        "risk_reward": risk_reward,
        "entry_zone_low": entry.get("zone_low"),
        "entry_zone_high": entry.get("zone_high"),
    }

    if reconcile_signals:
        signal_monitor = await reconcile_signal(
            symbol=symbol,
            timeframe=timeframe,
            current_price=float(ticker.get("lastPrice", cio_result.get("suggested_entry") or 0.0)),
            decision=lifecycle_decision,
            trade_setup=trade_setup,
            risk_idea=risk_idea,
            trend=features.get("trend", {}).get("primary", {}),
            momentum=features.get("momentum", {}).get("summary", {}),
            order_book=features.get("order_book", {}),
            data_freshness=data_freshness,
            liquidity=liquidity,
            ai_result=cio_result,
            council_approval=approval,
        )
    else:
        signal_monitor = {
            "status": "SIMULATED_APPROVED" if approval.get("approved") else "SIMULATED_REJECTED",
            "action": "SIMULATED",
            "side": trade_setup.get("side", "NEUTRAL"),
            "reason": "Historical replay; no live signal was read or written.",
        }

    # The institutional scenario remains the only publication authority. A
    # primary-timeframe-only confirmation is attached for the monitor as a
    # lower-confidence research watch; it cannot create or alter a signal.
    signal_monitor["confirmation_scenarios"] = (cio_result.get("live_confirmation") or {}).get("scenarios", {})
    signal_monitor["candidate_setup"] = {
        "side": trade_setup.get("side", "NEUTRAL"),
        "entry": trade_setup.get("entry", {}),
        "stop": trade_setup.get("stop", {}),
        "targets": trade_setup.get("targets", {}),
    }

    # ── Step 4: Reuse the exact quantitative snapshot reviewed by the
    # committee. Non-AI/backtest paths still construct it locally.
    # Rebuild this deterministic assessment each render.  A cached council
    # memo must never leave an earlier order-book/candle state in one card.
    from app.quant.engine import build_quantitative_assessment
    quantitative = build_quantitative_assessment(
        candles,
        order_book_raw,
        account_value=settings.default_account_size_usd,
        max_drawdown_pct=settings.max_drawdown_pct,
        max_gross_exposure_pct=settings.max_gross_exposure_pct,
        symbol=symbol,
        timeframe=timeframe,
        context_features=features,
    )
    analysis_snapshot = _build_analysis_snapshot(
        symbol=symbol, timeframe=timeframe, candles=candles, ticker=ticker,
        intelligence=intel, features=features, quantitative=quantitative,
        cio_result=cio_result, trade_setup=trade_setup, signal_monitor=signal_monitor,
        approval=approval, data_freshness=data_freshness, liquidity=liquidity,
    )
    cio_result["analysis_snapshot_id"] = analysis_snapshot["id"]

    # ── Step 5: Map back to the expected REST/WebSocket response payload ──
    # Adapter mapping
    payload = {
        "analysis_snapshot": analysis_snapshot,
        "symbol": symbol,
        "timeframe": timeframe,
        "decision": signal_monitor["action"],
        "market_decision": cio_result["decision"],
        "confidence": cio_result["confidence_pct"],
        "trade_grade": cio_result["trade_grade"],
        "failed_gate": approval["blockers"][0] if approval["blockers"] else None,
        "ai_calls": 1 if use_ai else 0,
        "ai_allowed": use_ai,
        "ai_provider": settings.ai_provider,
        "market": analysis_snapshot["market"],
        "gates": {
            "data_freshness": analysis_snapshot["execution"]["data_freshness"],
            "liquidity": analysis_snapshot["execution"]["liquidity"],
            "live_confirmation": analysis_snapshot["execution"]["live_confirmation"],
        },
        "data_quality": analysis_snapshot["source_coverage"],
        "quantitative": analysis_snapshot["quantitative"],
        "institutional_committee": cio_result.get("institutional_dossier"),
        "investment_memo": cio_result.get("investment_memo"),
        "committee_controls": cio_result.get("committee_controls"),
        "trend": features.get("trend", {}).get("primary", {}),
        "momentum": features.get("momentum", {}).get("summary", {}),
        "signal": {
            "state": signal_monitor["status"],
            "bias": signal_monitor.get("side", "NEUTRAL"),
            "action": signal_monitor["action"],
            "confidence": cio_result["confidence_pct"],
            "grade": cio_result["trade_grade"],
            "regime": features.get("volatility", {}).get("regime", "UNKNOWN"),
            "reasons": [cio_result["explanation"]],
            "warnings": cio_result.get("risk_warnings", []),
            "entry_zone": {
                "low": trade_setup.get("entry", {}).get("zone_low"),
                "high": trade_setup.get("entry", {}).get("zone_high"),
                "reference": trade_setup.get("entry", {}).get("reference"),
            },
            "stops": {
                "retail": trade_setup.get("stop", {}).get("selected"),
                "smart": trade_setup.get("stop", {}).get("selected"),
            },
            "targets": {
                "retail": trade_setup.get("targets", {}).get("tp1_1r"),
                "smart": trade_setup.get("targets", {}).get("tp3_3r"),
            },
        },
        "signal_monitor": analysis_snapshot["execution"]["signal_monitor"],
        "trade_setup": analysis_snapshot["execution"]["trade_setup"],
        "order_book_pressure": analysis_snapshot["execution"]["order_book_pressure"],
        "liquidity_sweep": analysis_snapshot["causal"]["liquidity_sweep"],
        "risk_idea": cio_result,
        "position_sizing_example": trade_setup.get("position"),
        "kelly_sizing": {
            "kelly_pct": 0.0,
            "risk_amount_usd": 0.0,
            "units": 0.0,
            "status": "no_history",
        },
        "ai_analysis": cio_result,
        "news_sentiment": analysis_snapshot["research"]["news_sentiment"],
        "historical_stats": historical_stats,
        "calendar_events": analysis_snapshot["research"]["calendar_events"],
        "macro_blockout": analysis_snapshot["research"]["macro_blockout"],
        "regime": analysis_snapshot["telemetry"]["regime"],
        "funding_rate": analysis_snapshot["execution"]["derivatives"]["funding_rate"],
        "open_interest": analysis_snapshot["execution"]["derivatives"]["open_interest"],
        "liquidations": analysis_snapshot["execution"]["derivatives"]["liquidations"],
        # Dashboard telemetry contract. Keep the raw source labels explicit so
        # widgets never silently fall back to invented neutral values.
        "derivatives": analysis_snapshot["execution"]["derivatives"],
        "risk_appetite_proxy": analysis_snapshot["telemetry"]["risk_appetite_proxy"],
        "sentiment": analysis_snapshot["telemetry"]["sentiment"],
        "market_structure": analysis_snapshot["causal"]["market_structure"],
        # Primary causal-decision contract for the dashboard. These fields are
        # intentionally separate from legacy indicator telemetry.
        "market_context": analysis_snapshot["causal"]["market_context"],
        "liquidity_map": analysis_snapshot["causal"]["liquidity_map"],
        "positioning": analysis_snapshot["causal"]["positioning"],
        "volatility_context": analysis_snapshot["causal"]["volatility_context"],
        "volume_profile": analysis_snapshot["causal"]["volume_profile"],
        "vwap_context": analysis_snapshot["causal"]["vwap_context"],
        "live_confirmation": analysis_snapshot["execution"]["live_confirmation"],
        "notes": [
            "Signal monitoring only. This app does not place trades.",
            "AI is run when deterministic gates pass and use_ai is enabled.",
        ],
    }

    updated_ai_time = last_ai_open_time
    # If new candle closed, update last_ai_open_time
    if is_new_candle and len(candles) > 1:
        try:
            updated_ai_time = candles[-2].open_time
        except (AttributeError, IndexError):
            pass

    return payload, updated_ai_time
