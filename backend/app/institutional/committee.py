"""Institutional committee controls built from measured evidence.

Specialist engines in this module are deterministic transformations of the
shared market snapshot.  They do not call an LLM and never fill missing data
with invented opinions.  An optional language model may later help the CIO
write a memo, but this module owns eligibility, vetoes, and confidence caps.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Iterable


ACTIONABLE = {"BUY_WATCH", "SELL_WATCH"}
ACTIVE_STATUSES = {"PENDING_ENTRY", "ACTIVE", "TP1_SECURED", "TP2_SECURED", "TP3_SECURED"}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _available_sources(intelligence: dict[str, Any]) -> set[str]:
    return set((intelligence.get("meta") or {}).get("sources_available") or [])


def _engine(
    name: str,
    *,
    status: str,
    bias: str = "NEUTRAL",
    confidence_pct: float = 0.0,
    evidence: Iterable[dict[str, Any]] = (),
    contradictory_evidence: Iterable[str] = (),
    unknowns: Iterable[str] = (),
    limitations: Iterable[str] = (),
) -> dict[str, Any]:
    return {
        "engine": name,
        "status": status,
        "bias": bias,
        "confidence_pct": round(max(0.0, min(_number(confidence_pct), 100.0)), 2),
        "evidence": list(evidence),
        "contradictory_evidence": list(contradictory_evidence),
        "unknowns": list(unknowns),
        "limitations": list(limitations),
    }


async def load_portfolio_state(settings: Any) -> dict[str, Any]:
    """Load portfolio exposure from persisted signals without pretending it is broker P&L."""
    signals: list[Any] = []
    try:
        from sqlalchemy import select
        from app.db.database import AsyncSessionLocal
        from app.db.models import TradeSignal

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(TradeSignal))
            signals = list(result.scalars().all())
    except Exception as exc:
        return {
            "available": False,
            "source": "unavailable",
            "reason": f"Portfolio state could not be loaded: {exc}",
            "current_drawdown_pct": None,
            "gross_exposure_pct": None,
        }

    active = [signal for signal in signals if signal.status in ACTIVE_STATUSES]
    account_value = max(_number(settings.default_account_size_usd), 0.0)
    gross_notional = sum(max(_number(signal.notional_usd), 0.0) for signal in active)
    open_risk = sum(max(_number(signal.risk_amount_usd), 0.0) for signal in active)
    gross_pct = gross_notional / account_value * 100.0 if account_value else 0.0
    risk_pct = open_risk / account_value * 100.0 if account_value else 0.0

    configured_drawdown = getattr(settings, "portfolio_current_drawdown_pct", None)
    drawdown_available = configured_drawdown is not None
    return {
        "available": drawdown_available,
        "source": "persisted_signals_plus_configured_drawdown" if drawdown_available else "persisted_signals_only",
        "reason": "" if drawdown_available else "Current account drawdown requires broker/account-equity input.",
        "account_value_usd": round(account_value, 2),
        "active_positions": len(active),
        "gross_notional_usd": round(gross_notional, 2),
        "open_risk_usd": round(open_risk, 2),
        "gross_exposure_pct": round(gross_pct, 4),
        "open_risk_pct": round(risk_pct, 4),
        "current_drawdown_pct": None if not drawdown_available else round(_number(configured_drawdown), 4),
        "limitations": [
            "Exposure covers signals persisted by this application, not positions opened elsewhere.",
            "Current drawdown is accepted only from configured account-equity input.",
        ],
    }


def _quant_engine(quantitative: dict[str, Any]) -> dict[str, Any]:
    stats = quantitative.get("statistical_features", {}) or {}
    forecast = quantitative.get("probability_engine", {}) or {}
    regime = quantitative.get("market_state", {}) or {}
    available = bool(stats.get("available")) and forecast.get("model") != "unavailable"
    probability_up = _number(forecast.get("probability_up"), 0.5)
    probability_down = _number(forecast.get("probability_down"), 0.5)
    bias = "BULLISH" if probability_up >= 0.55 else "BEARISH" if probability_down >= 0.55 else "NEUTRAL"
    return _engine(
        "quant_research_engine",
        status="COMPLETE" if available else "UNAVAILABLE",
        bias=bias if available else "NEUTRAL",
        confidence_pct=_number(forecast.get("confidence")) * 100.0,
        evidence=[
            {"metric": "model", "value": forecast.get("model"), "source": "quant.probability"},
            {"metric": "probability_up", "value": probability_up, "source": "quant.probability"},
            {"metric": "probability_down", "value": probability_down, "source": "quant.probability"},
            {"metric": "expected_return", "value": forecast.get("expected_return"), "source": "quant.probability"},
            {"metric": "expected_value", "value": forecast.get("expected_value"), "source": "quant.probability"},
            {"metric": "confidence_interval", "value": forecast.get("confidence_interval"), "source": "quant.probability"},
            {"metric": "market_state", "value": regime.get("state"), "source": "quant.regimes"},
        ],
        unknowns=[] if available else [stats.get("reason", "Quantitative forecast unavailable.")],
        limitations=[forecast.get("model_status", "Model validation status unavailable.")],
    )


def _microstructure_engine(features: dict[str, Any], quantitative: dict[str, Any]) -> dict[str, Any]:
    micro = quantitative.get("microstructure", {}) or features.get("microstructure", {}) or {}
    flow = features.get("trade_flow", {}) or {}
    available = bool(micro.get("available"))
    score = 0.65 * _number(micro.get("signed_trade_flow")) + 0.35 * _number(micro.get("depth_imbalance"))
    bias = "BULLISH" if score >= 0.08 else "BEARISH" if score <= -0.08 else "NEUTRAL"
    confidence = min(70.0, 35.0 + abs(score) * 100.0) if available else 0.0
    contradictions = []
    if _number(micro.get("signed_trade_flow")) * _number(micro.get("depth_imbalance")) < 0:
        contradictions.append("Displayed depth and aggressive trade flow point in opposite directions.")
    evidence = []
    if available:
        evidence = [
            {"metric": "spread_bps", "value": micro.get("spread_bps"), "source": "Binance order-book snapshot"},
            {"metric": "depth_imbalance", "value": micro.get("depth_imbalance"), "source": "Binance order-book snapshot"},
            {"metric": "signed_trade_flow", "value": micro.get("signed_trade_flow"), "source": "completed Binance klines"},
            {"metric": "recent_trade_buy_ratio", "value": flow.get("buy_ratio"), "source": "Binance recent trades"},
        ]
    return _engine(
        "market_microstructure_engine",
        status="PARTIAL" if available else "UNAVAILABLE",
        bias=bias if available else "NEUTRAL",
        confidence_pct=confidence,
        evidence=evidence,
        contradictory_evidence=contradictions,
        unknowns=[] if available else [micro.get("reason", "Order-book evidence unavailable.")],
        limitations=micro.get("limitations", ["No queue-position, cancellation, latency, or market-impact model."]),
    )


def _market_structure_engine(features: dict[str, Any]) -> dict[str, Any]:
    """Evaluate completed-candle SMC context as bounded directional evidence."""
    structure = features.get("market_structure", {}) or {}
    if not structure:
        return _engine(
            "market_structure_engine",
            status="UNAVAILABLE",
            unknowns=["Completed-candle structure data unavailable."],
        )

    phase = str(structure.get("phase", "RANGING"))
    bos = structure.get("bos", {}) or {}
    choch = structure.get("choch", {}) or {}
    sweep = structure.get("liquidity_sweep", features.get("sweep", {})) or {}
    score = 0.0
    if bos.get("detected"):
        score += 1.0 if bos.get("direction") == "bullish" else -1.0 if bos.get("direction") == "bearish" else 0.0
    if choch.get("detected"):
        score += 0.75 if choch.get("direction") == "bullish" else -0.75 if choch.get("direction") == "bearish" else 0.0
    if phase in {"MARKUP", "ACCUMULATION"}:
        score += 0.5
    elif phase in {"MARKDOWN", "DISTRIBUTION"}:
        score -= 0.5
    sweep_direction = str(sweep.get("direction", ""))
    if sweep_direction.startswith("bullish"):
        score += 0.5
    elif sweep_direction.startswith("bearish"):
        score -= 0.5

    bias = "BULLISH" if score >= 0.75 else "BEARISH" if score <= -0.75 else "NEUTRAL"
    contradictions = []
    if bos.get("detected") and choch.get("detected") and bos.get("direction") != choch.get("direction"):
        contradictions.append("Break-of-structure and change-of-character point in opposite directions.")
    return _engine(
        "market_structure_engine",
        status="COMPLETE",
        bias=bias,
        confidence_pct=min(60.0, 30.0 + abs(score) * 12.0),
        evidence=[
            {"metric": "market_phase", "value": phase, "source": "completed Binance candles"},
            {"metric": "break_of_structure", "value": bos, "source": "completed Binance candles"},
            {"metric": "change_of_character", "value": choch, "source": "completed Binance candles"},
            {"metric": "liquidity_sweep", "value": sweep, "source": "completed Binance candles"},
            {"metric": "active_order_blocks", "value": len(structure.get("order_blocks") or []), "source": "completed Binance candles"},
            {"metric": "active_fair_value_gaps", "value": len(structure.get("fair_value_gaps") or []), "source": "completed Binance candles"},
        ],
        contradictory_evidence=contradictions,
        limitations=[structure.get("limitations", "Structure labels are confluence evidence, not proof of institutional intent.")],
    )


def _market_context_engine(features: dict[str, Any]) -> dict[str, Any]:
    """Expose the causal scoring contract as the committee's primary thesis."""
    context = features.get("market_context", {}) or {}
    coverage = context.get("coverage", {}) or {}
    direction = context.get("direction", "WAIT")
    available = int(coverage.get("available_domains", 0))
    complete = bool(coverage.get("complete"))
    bias = "BULLISH" if direction == "LONG" else "BEARISH" if direction == "SHORT" else "NEUTRAL"
    return _engine(
        "causal_market_context_engine",
        status="COMPLETE" if complete else "PARTIAL" if available else "UNAVAILABLE",
        bias=bias,
        confidence_pct=_number(context.get("score")) if direction != "WAIT" else 0.0,
        evidence=[
            {"metric": "direction", "value": direction, "source": context.get("method", "causal_market_context")},
            {"metric": "context_score", "value": context.get("score"), "source": context.get("method", "causal_market_context")},
            {"metric": "coverage", "value": coverage, "source": "feature_engine"},
            {"metric": "components", "value": context.get("components", {}), "source": "feature_engine"},
        ],
        contradictory_evidence=[f"Causal context disagreement: {name}." for name in context.get("contradictions", [])],
        unknowns=[] if complete else ["Causal setup coverage is incomplete; missing domains are not neutral evidence."],
        limitations=context.get("limitations", []),
    )


def _derivatives_engine(features: dict[str, Any], intelligence: dict[str, Any]) -> dict[str, Any]:
    derivatives = features.get("derivatives", {}) or {}
    sources = _available_sources(intelligence)
    perp_available = "funding" in sources and "open_interest" in sources
    taker = derivatives.get("taker_volume", {}) or {}
    top = derivatives.get("top_traders", {}) or {}
    funding = _number(derivatives.get("funding_rate"))
    options = intelligence.get("options", {}) or {}
    options_available = bool(options.get("available"))
    score = 0.0
    if taker.get("cvd_trend") == "CVD_BULLISH_ACCUMULATION":
        score += 1.0
    elif taker.get("cvd_trend") == "CVD_BEARISH_DISTRIBUTION":
        score -= 1.0
    if top.get("bias") == "SMART_MONEY_LONG":
        score += 0.5
    elif top.get("bias") == "SMART_MONEY_SHORT":
        score -= 0.5
    if funding > 0.0003:
        score -= 0.35
    elif funding < -0.0001:
        score += 0.35
    bias = "BULLISH" if score >= 0.5 else "BEARISH" if score <= -0.5 else "NEUTRAL"
    evidence = []
    if perp_available:
        evidence.extend([
            {"metric": "funding_rate", "value": funding, "source": "Binance Futures"},
            {"metric": "open_interest", "value": derivatives.get("open_interest"), "source": "Binance Futures"},
            {"metric": "oi_change_pct", "value": (derivatives.get("oi_delta") or {}).get("oi_change_pct"), "source": "Binance Futures history"},
            {"metric": "taker_cvd", "value": taker.get("cvd_trend"), "source": "Binance Futures"},
            {"metric": "top_trader_bias", "value": top.get("bias"), "source": "Binance Futures"},
        ])
    if options_available:
        evidence.extend([
            {"metric": "options_atm_iv", "value": options.get("atm_iv"), "source": options.get("source", "options provider")},
            {"metric": "options_put_call_skew", "value": options.get("put_call_skew"), "source": options.get("source", "options provider")},
            {"metric": "options_term_structure", "value": options.get("term_structure"), "source": options.get("source", "options provider")},
            {"metric": "gamma_exposure", "value": options.get("gamma_exposure"), "source": options.get("source", "options provider")},
            {"metric": "dealer_positioning", "value": options.get("dealer_positioning"), "source": options.get("source", "options provider")},
        ])
    return _engine(
        "derivatives_engine",
        status="COMPLETE" if perp_available and options_available else "PARTIAL" if perp_available or options_available else "UNAVAILABLE",
        bias=bias if perp_available else "NEUTRAL",
        confidence_pct=min(65.0, 35.0 + abs(score) * 15.0) if perp_available else 0.0,
        evidence=evidence,
        unknowns=[] if options_available else ["Options implied-volatility surface unavailable.", "Gamma and dealer positioning unavailable."],
        limitations=["Perpetual-futures positioning is not a substitute for options-market positioning."],
    )


def _macro_engine(features: dict[str, Any], intelligence: dict[str, Any]) -> dict[str, Any]:
    cross = features.get("cross_asset", {}) or {}
    risk_appetite = intelligence.get("global_liquidity", {}) or {}
    sources = _available_sources(intelligence)
    available = "macro" in sources
    risk_environment = cross.get("risk_environment", "NEUTRAL")
    risk_appetite_status = risk_appetite.get("risk_appetite_status", "UNKNOWN")
    score = (-1 if risk_environment == "RISK_OFF" else 1 if risk_environment == "RISK_ON" else 0)
    score += -0.25 if risk_appetite_status == "RISK_APPETITE_NEGATIVE" else 0.25 if risk_appetite_status == "RISK_APPETITE_POSITIVE" else 0
    bias = "BULLISH" if score >= 1 else "BEARISH" if score <= -1 else "NEUTRAL"
    evidence = []
    if available:
        evidence.extend([
            {"metric": "risk_environment", "value": risk_environment, "source": "cross-asset snapshot"},
            {"metric": "dxy", "value": cross.get("dxy"), "source": "macro feed"},
            {"metric": "nasdaq", "value": cross.get("nasdaq"), "source": "macro feed"},
            {"metric": "10y_yield", "value": cross.get("yields_10y"), "source": "macro feed"},
        ])
    if "global_liquidity" in sources:
        evidence.append({"metric": "risk_appetite_proxy", "value": risk_appetite_status, "source": risk_appetite.get("source", "Fear & Greed feed")})
    return _engine(
        "macro_intelligence_engine",
        status="PARTIAL" if available else "UNAVAILABLE",
        bias=bias if available else "NEUTRAL",
        confidence_pct=55.0 if available else 0.0,
        evidence=evidence,
        unknowns=[] if available else ["Macro market snapshot unavailable."],
        limitations=[
            "Snapshot moves are context, not a causal or stable correlation model.",
            str(risk_appetite.get("limitations", "Risk-appetite proxy is not global liquidity.")),
        ],
    )


def _provisional_thesis(engines: dict[str, dict[str, Any]], features: dict[str, Any] | None = None) -> dict[str, Any]:
    context = ((features or {}).get("market_context") or {})
    coverage = context.get("coverage", {}) or {}
    if coverage.get("complete"):
        direction = context.get("direction", "WAIT")
        return {
            "direction": direction if direction in {"LONG", "SHORT"} else "NEUTRAL",
            "weighted_score": _number(context.get("normalized_directional_score")),
            "engines_with_evidence": int(coverage.get("available_domains", 0)),
            "method": context.get("method", "causal_market_context_v1"),
            "setup_score": _number(context.get("score")),
            "context_status": context.get("status", "WAIT"),
        }
    weighted_score = 0.0
    total_weight = 0.0
    for report in engines.values():
        if report.get("status") == "UNAVAILABLE":
            continue
        direction = 1.0 if report.get("bias") == "BULLISH" else -1.0 if report.get("bias") == "BEARISH" else 0.0
        weight = _number(report.get("confidence_pct")) / 100.0
        weighted_score += direction * weight
        total_weight += weight
    normalized = weighted_score / total_weight if total_weight else 0.0
    direction = "LONG" if normalized >= 0.18 else "SHORT" if normalized <= -0.18 else "NEUTRAL"
    return {"direction": direction, "weighted_score": round(normalized, 4), "engines_with_evidence": sum(r.get("status") != "UNAVAILABLE" for r in engines.values()), "method": "legacy_evidence_fallback"}


def _adversarial_review(
    engines: dict[str, dict[str, Any]],
    thesis: dict[str, Any],
    features: dict[str, Any],
    macro_blockout: dict[str, Any],
) -> dict[str, Any]:
    direction = thesis["direction"]
    opposition = "BEARISH" if direction == "LONG" else "BULLISH" if direction == "SHORT" else None
    contradictions: list[str] = []
    severity = 1
    for name, report in engines.items():
        if opposition and report.get("bias") == opposition and _number(report.get("confidence_pct")) >= 40:
            contradictions.append(f"{name} opposes the provisional {direction.lower()} thesis.")
            severity += 2
        contradictions.extend(report.get("contradictory_evidence") or [])
    unavailable = [name for name, report in engines.items() if report.get("status") == "UNAVAILABLE"]
    if unavailable:
        severity += min(2, len(unavailable))
        contradictions.append("Unavailable specialist evidence: " + ", ".join(unavailable) + ".")
    quant = engines["quant_research_engine"]
    if any("baseline" in str(item).lower() for item in quant.get("limitations", [])):
        severity += 2
        contradictions.append("The directional probability is an unvalidated research baseline.")
    if macro_blockout.get("active"):
        severity += 3
        contradictions.append("A high-impact macro event can invalidate short-horizon evidence.")
    if (features.get("microstructure") or {}).get("liquidity_quality") == "thin":
        severity += 2
        contradictions.append("Thin liquidity raises slippage and manipulation risk.")
    severity = min(severity, 10)
    return {
        "engine": "adversarial_review_engine",
        "status": "COMPLETE",
        "severity_score": severity,
        "veto": severity >= 8,
        "thesis_tested": direction,
        "contradictory_evidence": contradictions,
        "weakest_assumptions": [
            "Snapshot order-book liquidity will remain available at execution.",
            "Recent statistical relationships will persist into the next horizon.",
            "Unobserved options positioning will not dominate price discovery.",
        ],
        "falsification_tests": [
            "Reject if price invalidates the pre-committed structural level.",
            "Reject if signed trade flow reverses while displayed depth remains adverse.",
            "Reject if expected value becomes non-positive after cost and risk updates.",
        ],
    }


def _risk_committee(
    engines: dict[str, dict[str, Any]],
    quantitative: dict[str, Any],
    features: dict[str, Any],
    thesis: dict[str, Any],
    portfolio_state: dict[str, Any],
    data_quality: dict[str, Any],
    macro_blockout: dict[str, Any],
    adversarial: dict[str, Any],
    settings: Any,
) -> dict[str, Any]:
    forecast = quantitative.get("probability_engine", {}) or {}
    quant_risk = quantitative.get("risk_engine", {}) or {}
    hard_blockers: list[str] = []
    warnings: list[str] = []
    quant_one_bar_ev = _number(forecast.get("expected_value"))
    forecast_confidence = _number(forecast.get("confidence"))
    model_status = str(forecast.get("model_status", "")).lower()
    model_validated = "baseline" not in model_status and forecast.get("model") not in {None, "unavailable"}
    portfolio_available = bool(portfolio_state.get("available"))
    restrictions: list[str] = []

    if not data_quality.get("passed", False):
        hard_blockers.append("Required core market data is incomplete.")
    context = features.get("market_context", {}) or {}
    if context and context.get("status") != "SETUP_CANDIDATE":
        hard_blockers.append("Causal market-context score is not an aligned setup; wait for regime, liquidity, positioning, and flow alignment.")
    # Estimate EV on the same horizon as the proposed trade plan. The quant
    # forecast is next-observation directional evidence; it cannot be compared
    # directly with a multi-bar ATR stop and target ladder. Engine agreement
    # adjusts the probability of reaching the payoff barrier, then explicit
    # fees, slippage, and spread are charged in R units.
    expected_payoff_r = max(_number(getattr(settings, "institutional_expected_payoff_r", 2.5), 2.5), 0.25)
    thesis_score = abs(_number(thesis.get("weighted_score")))
    directional_probability = min(0.70, 0.50 + thesis_score * 0.20) if thesis.get("direction") in {"LONG", "SHORT"} else 0.50
    break_even_hit_probability = 1.0 / (1.0 + expected_payoff_r)
    target_hit_probability = min(
        0.85,
        max(0.05, break_even_hit_probability + (directional_probability - 0.50) * 0.85),
    )
    gross_expected_value_r = target_hit_probability * expected_payoff_r - (1.0 - target_hit_probability)
    fee_bps = max(_number(getattr(settings, "institutional_fee_bps_per_side", 3.0), 3.0), 0.0)
    slippage_bps = max(_number(getattr(settings, "institutional_slippage_bps_per_side", 0.5), 0.5), 0.0)
    spread_bps = max(_number((quantitative.get("microstructure") or {}).get("spread_bps")), 0.0)
    round_trip_cost_pct = (2.0 * (fee_bps + slippage_bps) + spread_bps) / 100.0
    stop_distance_pct = max(_number((features.get("volatility") or {}).get("atr_pct")) * 1.5, 0.1)
    transaction_cost_r = round_trip_cost_pct / stop_distance_pct
    expected_value_r = gross_expected_value_r - transaction_cost_r
    if thesis.get("direction") == "NEUTRAL":
        hard_blockers.append("No measurable directional edge exists across the evidence engines.")
    elif expected_value_r <= 0:
        hard_blockers.append(f"Cost-adjusted trade expected value is non-positive ({expected_value_r:.3f}R).")
    if not model_validated:
        if getattr(settings, "institutional_require_validated_model", False):
            hard_blockers.append("No validated out-of-sample forecasting model is active.")
        else:
            warnings.append("No validated out-of-sample forecasting model is active; conditional manual review only.")
            restrictions.append("Cap risk because the probability model is an unvalidated research baseline.")
    minimum_confidence = _number(getattr(settings, "institutional_min_forecast_confidence", 0.50), 0.50)
    if forecast_confidence < minimum_confidence:
        hard_blockers.append(f"Forecast confidence {forecast_confidence:.2f} is below the institutional minimum {minimum_confidence:.2f}.")
    if macro_blockout.get("active"):
        hard_blockers.append("High-impact macro event blockout is active.")
    if adversarial.get("veto"):
        hard_blockers.append(f"Adversarial severity {adversarial.get('severity_score')}/10 triggered a veto.")

    require_portfolio = getattr(settings, "institutional_require_portfolio_state", False)
    if not portfolio_available:
        if require_portfolio:
            hard_blockers.append("Current portfolio drawdown is unavailable.")
        else:
            warnings.append("Current portfolio drawdown is unavailable; conditional manual review only.")
            restrictions.append("Cap risk because total account drawdown cannot be verified.")
    drawdown = portfolio_state.get("current_drawdown_pct")
    if drawdown is not None and _number(drawdown) >= _number(settings.max_drawdown_pct):
        hard_blockers.append("Maximum portfolio drawdown limit reached.")
    gross = portfolio_state.get("gross_exposure_pct")
    if gross is not None and _number(gross) >= _number(settings.max_gross_exposure_pct):
        hard_blockers.append("Maximum gross-exposure limit reached.")

    for name in ("derivatives_engine",):
        if engines[name]["status"] != "COMPLETE":
            warnings.append(f"{name} has incomplete coverage.")
    # The quant risk module's EV blocker refers to its next-observation horizon.
    # Portfolio drawdown/exposure blockers remain authoritative, while trade EV
    # is owned by the horizon-matched calculation above.
    quant_hard_blockers = [
        item for item in (quant_risk.get("blockers") or [])
        if "expected value" not in str(item).lower()
    ]
    blockers = list(dict.fromkeys(hard_blockers + quant_hard_blockers))
    risk_fraction = max(_number(quant_risk.get("volatility_adjusted_risk_fraction")), 0.0)
    if expected_value_r > 0 and risk_fraction <= 0:
        risk_fraction = min(
            max(_number(getattr(settings, "default_risk_per_idea_pct", 0.5), 0.5) / 100.0, 0.0),
            0.005,
        )
    if not model_validated:
        risk_fraction = min(
            risk_fraction,
            _number(getattr(settings, "institutional_unvalidated_risk_cap_pct", 0.25), 0.25) / 100.0,
        )
    if not portfolio_available:
        risk_fraction = min(
            risk_fraction,
            _number(getattr(settings, "institutional_missing_portfolio_risk_cap_pct", 0.25), 0.25) / 100.0,
        )
    allocation_tier = "CAPITAL_ELIGIBLE" if model_validated and portfolio_available else "CONDITIONAL_MANUAL_REVIEW"
    minimum_directional_engines = max(1, int(getattr(settings, "institutional_min_directional_engines", 2)))
    return {
        "engine": "risk_committee",
        "status": "APPROVED" if not blockers else "VETOED",
        "approved_for_allocation": not blockers,
        "hard_blockers": blockers,
        "warnings": warnings,
        "restrictions": restrictions,
        "allocation_tier": allocation_tier,
        "model_validated": model_validated,
        "portfolio_state_available": portfolio_available,
        "minimum_directional_engines": minimum_directional_engines,
        "expected_value": round(expected_value_r, 4),
        "expected_value_unit": "R_multiple_after_costs",
        "quant_next_observation_expected_value": quant_one_bar_ev,
        "ev_method": {
            "direction": thesis.get("direction"),
            "directional_probability": round(directional_probability, 4),
            "target_hit_probability": round(target_hit_probability, 4),
            "expected_payoff_r": round(expected_payoff_r, 3),
            "gross_expected_value_r": round(gross_expected_value_r, 4),
            "transaction_cost_r": round(transaction_cost_r, 4),
            "round_trip_cost_pct": round(round_trip_cost_pct, 4),
            "stop_distance_pct": round(stop_distance_pct, 4),
        },
        "expected_drawdown_proxy": forecast.get("expected_risk"),
        "forecast_confidence": forecast_confidence,
        "portfolio_state": portfolio_state,
        "allocation_ceiling": {
            "risk_fraction": round(risk_fraction, 6),
            "max_notional_usd": quant_risk.get("illustrative_max_notional_usd", 0.0),
        },
        "decision": "ELIGIBLE_FOR_CIO_REVIEW" if not blockers else "WAIT",
    }


def build_institutional_dossier(
    *,
    symbol: str,
    timeframe: str,
    features: dict[str, Any],
    intelligence: dict[str, Any],
    quantitative: dict[str, Any],
    portfolio_state: dict[str, Any],
    macro_blockout: dict[str, Any],
    settings: Any,
) -> dict[str, Any]:
    engines = {
        "causal_market_context_engine": _market_context_engine(features),
        "quant_research_engine": _quant_engine(quantitative),
        "market_structure_engine": _market_structure_engine(features),
        "market_microstructure_engine": _microstructure_engine(features, quantitative),
        "derivatives_engine": _derivatives_engine(features, intelligence),
        "macro_intelligence_engine": _macro_engine(features, intelligence),
    }
    thesis = _provisional_thesis(engines, features)
    adversarial = _adversarial_review(engines, thesis, features, macro_blockout)
    risk = _risk_committee(
        engines, quantitative, features, thesis, portfolio_state, features.get("data_quality", {}),
        macro_blockout, adversarial, settings,
    )
    engines["adversarial_review_engine"] = adversarial
    engines["risk_committee"] = risk
    available = sum(report.get("status") not in {"UNAVAILABLE"} for report in engines.values())
    return {
        "architecture": "institutional_committee_v1",
        "symbol": symbol,
        "timeframe": timeframe,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "policy": {
            "capital_preservation_first": True,
            "no_trade_is_a_valid_decision": True,
            "llm_may_override_veto": False,
            "execution": "manual_only",
        },
        "data_quality": features.get("data_quality", {}),
        "engines": engines,
        "provisional_thesis": thesis,
        "adversarial_review": adversarial,
        "risk_committee": risk,
        "engine_coverage_pct": round(available / len(engines) * 100.0, 1),
    }


def build_investment_memo(cio_result: dict[str, Any], dossier: dict[str, Any]) -> dict[str, Any]:
    forecast = dossier.get("engines", {}).get("quant_research_engine", {})
    risk = dossier.get("risk_committee", {})
    adversarial = dossier.get("adversarial_review", {})
    decision = cio_result.get("decision", "WAIT")
    actionable = decision in ACTIONABLE
    plan = cio_result.get("deterministic_trade_plan") or {}
    entry = plan.get("entry") or {}
    stop = plan.get("stop") or {}
    targets = plan.get("targets") or {}
    position = plan.get("position") or {}
    entry_reference = _number(entry.get("reference"))
    risk_per_unit = _number(stop.get("risk_per_unit"))
    tp2 = _number(targets.get("tp2_2r"))
    risk_reward = abs(tp2 - entry_reference) / risk_per_unit if risk_per_unit > 0 else None
    measured_evidence = []
    for engine_name, report in dossier.get("engines", {}).items():
        for item in report.get("evidence", []):
            measured_evidence.append(
                f"{engine_name}: {item.get('metric')}={item.get('value')} ({item.get('source')})"
            )
    quant_values = {
        item.get("metric"): item.get("value")
        for item in forecast.get("evidence", [])
    }
    events = dossier.get("calendar_events", []) or []
    events_to_monitor = [
        event.get("event") or event.get("title") or str(event)
        for event in events[:5]
    ] + list(adversarial.get("falsification_tests", []))
    memo = {
        "executive_summary": cio_result.get("explanation") or "The committee found no allocation-ready edge.",
        "market_context": (
            f"{dossier.get('symbol')} {dossier.get('timeframe')} review; "
            f"engine coverage {dossier.get('engine_coverage_pct')}%; "
            f"provisional direction {dossier.get('provisional_thesis', {}).get('direction', 'NEUTRAL')}."
        ),
        "primary_thesis": dossier.get("provisional_thesis"),
        "supporting_evidence": measured_evidence,
        "contradictory_evidence": adversarial.get("contradictory_evidence", []),
        "unknown_variables": [item for report in dossier.get("engines", {}).values() for item in report.get("unknowns", [])],
        "risk_assessment": risk,
        "scenario_analysis": {
            "bull_case": f"Quant probability_up={quant_values.get('probability_up')}; requires supporting flow and no invalidation.",
            "base_case": f"Committee action remains {decision}; reassess when evidence or controls change.",
            "bear_case": f"Quant probability_down={quant_values.get('probability_down')}; adverse flow or a falsification trigger invalidates the thesis.",
        },
        "expected_value": risk.get("expected_value"),
        "recommended_action": decision,
        "entry_zone": entry if actionable and plan else None,
        "invalidation": stop if actionable and plan else None,
        "stop_loss": stop.get("selected") if actionable and plan else None,
        "profit_targets": targets if actionable and plan else None,
        "risk_reward": round(risk_reward, 3) if actionable and risk_reward is not None else None,
        "position_size": position if actionable and plan else 0,
        "portfolio_allocation": risk.get("allocation_ceiling") if actionable else {"risk_fraction": 0.0, "max_notional_usd": 0.0},
        "time_horizon": cio_result.get("time_horizon") or dossier.get("timeframe"),
        "events_to_monitor": events_to_monitor,
        "confidence_score": cio_result.get("confidence_pct", 0),
        "reasons_confidence_was_reduced": risk.get("hard_blockers", []) + risk.get("warnings", []),
        "decision_rationale": (
            "Allocation is eligible only when measured expected value, model validation, portfolio limits, "
            "and adversarial controls all pass; doing nothing remains the benchmark."
        ),
    }
    return memo


def build_deterministic_cio_decision(
    dossier: dict[str, Any],
    *,
    narrative_error: str | None = None,
) -> dict[str, Any]:
    """Synthesize the code-owned dossier when no narrative model is available.

    This is not a second forecasting model. It maps committee eligibility and
    the provisional evidence direction into the existing CIO contract.
    """
    risk = dossier.get("risk_committee", {}) or {}
    adversarial = dossier.get("adversarial_review", {}) or {}
    thesis = dossier.get("provisional_thesis", {}) or {}
    direction = thesis.get("direction", "NEUTRAL")
    score = abs(_number(thesis.get("weighted_score")))
    eligible = bool(risk.get("approved_for_allocation")) and not adversarial.get("veto")
    actionable = eligible and direction in {"LONG", "SHORT"}
    decision = "BUY_WATCH" if actionable and direction == "LONG" else "SELL_WATCH" if actionable else "WAIT"
    if actionable:
        confidence = min(74.0, 60.0 + score * 20.0)
        grade = "A" if risk.get("allocation_tier") == "CAPITAL_ELIGIBLE" and confidence >= 72 else "B"
        explanation = (
            f"The deterministic committee approved a {direction.lower()} {risk.get('allocation_tier', '').lower()} candidate: "
            f"cost-adjusted EV {risk.get('expected_value')}R, evidence score {thesis.get('weighted_score')}, "
            f"and {thesis.get('engines_with_evidence')} engines with usable evidence."
        )
    else:
        confidence = min(55.0, 35.0 + score * 20.0)
        grade = "C" if direction != "NEUTRAL" else "F"
        blockers = risk.get("hard_blockers") or ["No allocation-ready directional edge."]
        explanation = f"WAIT: {blockers[0]}"
    warnings = list(risk.get("warnings", [])) + list(risk.get("hard_blockers", []))
    if narrative_error:
        warnings.insert(0, f"CIO narrative unavailable; deterministic synthesis used: {narrative_error}")
    return {
        "decision": decision,
        "confidence_pct": round(confidence, 2),
        "trade_grade": grade,
        "explanation": explanation,
        "risk_warnings": list(dict.fromkeys(warnings)),
        "suggested_entry": None,
        "suggested_stop": None,
        "suggested_targets": None,
        "position_size": 0,
    }


def render_investment_memo(memo: dict[str, Any]) -> str:
    def show(value: Any) -> str:
        if isinstance(value, list):
            return "\n".join(f"- {item}" for item in value) if value else "- None"
        return str(value)

    sections = [
        ("Executive Summary", memo["executive_summary"]),
        ("Market Context", memo["market_context"]),
        ("Primary Thesis", memo["primary_thesis"]),
        ("Supporting Evidence", memo["supporting_evidence"]),
        ("Contradictory Evidence", memo["contradictory_evidence"]),
        ("Unknown Variables", memo["unknown_variables"]),
        ("Risk Assessment", memo["risk_assessment"]),
        ("Scenario Analysis", memo["scenario_analysis"]),
        ("Expected Value", memo["expected_value"]),
        ("Recommended Action", memo["recommended_action"]),
        ("Entry Zone", memo["entry_zone"]),
        ("Invalidation / Stop Loss", {"invalidation": memo["invalidation"], "stop_loss": memo["stop_loss"]}),
        ("Profit Targets", memo["profit_targets"]),
        ("Risk / Reward", memo["risk_reward"]),
        ("Position Size / Portfolio Allocation", {"position_size": memo["position_size"], "allocation": memo["portfolio_allocation"]}),
        ("Time Horizon", memo["time_horizon"]),
        ("Events To Monitor", memo["events_to_monitor"]),
        ("Confidence", memo["confidence_score"]),
        ("Reasons Confidence Was Reduced", memo["reasons_confidence_was_reduced"]),
        ("Decision Rationale", memo["decision_rationale"]),
    ]
    return "\n\n".join(f"## {title}\n\n{show(value)}" for title, value in sections)


def apply_cio_policy(cio_result: dict[str, Any], dossier: dict[str, Any]) -> dict[str, Any]:
    """Constrain a CIO narrative to evidence and non-overridable committee controls."""
    result = dict(cio_result or {})
    risk = dossier["risk_committee"]
    adversarial = dossier["adversarial_review"]
    decision = str(result.get("decision", "WAIT")).upper()
    proposed_decision = decision
    if decision == "HOLD":
        decision = "WAIT"
    if decision not in ACTIONABLE | {"WAIT", "AVOID"}:
        decision = "WAIT"

    confidence = _number(result.get("confidence_pct"))
    if not risk.get("approved_for_allocation") or adversarial.get("veto"):
        decision = "WAIT"
        confidence = min(confidence, 55.0)
        result["trade_grade"] = "C" if result.get("trade_grade") in {"A+", "A", "B"} else result.get("trade_grade", "F")
        if proposed_decision in ACTIONABLE:
            first_blocker = next(iter(risk.get("hard_blockers", [])), "committee controls did not pass")
            result["explanation"] = f"Allocation withheld despite the proposed {proposed_decision}: {first_blocker}"
    if decision in ACTIONABLE and dossier.get("provisional_thesis", {}).get("direction") == "NEUTRAL":
        decision = "WAIT"
        confidence = min(confidence, 55.0)

    result["decision"] = decision
    result["confidence_pct"] = round(max(0.0, min(confidence, 100.0)), 2)
    result.setdefault("trade_grade", "F")
    result.setdefault("explanation", "No allocation-ready edge was established.")
    result.setdefault("risk_warnings", [])
    # Execution parameters are always discarded, even for an eligible thesis.
    # They are rebuilt later by the deterministic trade-plan layer.
    result["suggested_entry"] = None
    result["suggested_stop"] = None
    result["suggested_targets"] = None
    result["position_size"] = 0
    result["risk_warnings"] = list(dict.fromkeys(list(result["risk_warnings"]) + risk.get("hard_blockers", []) + risk.get("warnings", [])))
    result["institutional_dossier"] = dossier
    result["committee_controls"] = {
        "risk_veto": not risk.get("approved_for_allocation"),
        "adversarial_veto": bool(adversarial.get("veto")),
        "llm_override_permitted": False,
    }
    memo = build_investment_memo(result, dossier)
    result["investment_memo"] = memo
    # Use a deterministic complete memorandum.  LLM prose remains available in
    # the memo fields, but it cannot omit mandatory sections.
    result["report_md"] = render_investment_memo(memo)
    return result
