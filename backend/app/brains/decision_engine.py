"""Decision engine — trade setup construction, confidence scoring, and report generation.

Formerly ``tree_analysis.py``.  All pure-indicator functions (EMA, RSI, ATR,
sweep detection, etc.) have been moved to the ``indicators`` package.  This
module now contains only decision-making and trade-setup logic.
"""
from __future__ import annotations

from typing import Any

from app.data_sources.binance_public import Candle, completed_candles
from app.indicators.volatility import atr as _atr
from app.indicators._math import pct as _pct


def check_data_freshness(candles: list[Candle], interval: str) -> dict[str, Any]:
    from time import time
    from app.data_sources.binance_public import interval_seconds
    closed = completed_candles(candles)
    if not closed:
        return {"passed": False, "reason": "no_closed_candles"}

    last = closed[-1]
    age_seconds = max((time() * 1000 - last.close_time) / 1000, 0)
    max_age = interval_seconds(interval) * 2.5
    return {
        "passed": age_seconds <= max_age,
        "age_seconds": round(age_seconds, 2),
        "max_age_seconds": max_age,
        "last_close_time": last.close_time,
        "reason": "fresh" if age_seconds <= max_age else "stale_candles",
    }


def build_risk_idea(
    candles: list[Candle],
    sweep: dict[str, Any],
    min_rr: float,
    order_book: dict[str, Any] | None = None,
    trend: dict[str, Any] | None = None,
    order_book_pressure: dict[str, Any] | None = None,
    momentum: dict[str, Any] | None = None,
    atr: float | None = None,
) -> dict[str, Any] | None:
    closed = completed_candles(candles)
    if not closed:
        return None

    last = closed[-1]
    entry = last.close
    side = ""
    setup_type = "none"
    setup_quality = "normal"
    trigger = ""
    retail_stop = 0.0

    if sweep.get("detected"):
        direction = sweep.get("direction", "")
        setup_type = "liquidity_sweep"
        setup_quality = sweep.get("quality", "normal")
        trigger = f"{direction} after sweep of {sweep.get('swept_level')}"
        if direction.startswith("bullish"):
            retail_stop = float(sweep["sweep_extreme"]) * 0.999
            side = "long_watch"
        elif direction.startswith("bearish"):
            retail_stop = float(sweep["sweep_extreme"]) * 1.001
            side = "short_watch"
        else:
            return None
    else:
        trend_status = (trend or {}).get("status", "sideways_or_mixed")
        pressure = (order_book_pressure or {}).get("pressure", "balanced")
        momentum_bias = (momentum or {}).get("bias", "neutral")
        momentum_score = float((momentum or {}).get("score", 50.0))
        bullish_votes = int(trend_status == "bullish") + int(pressure == "buyers") + int(momentum_bias == "bullish")
        bearish_votes = int(trend_status == "bearish") + int(pressure == "sellers") + int(momentum_bias == "bearish")

        atr_val = atr if atr and atr > 0 else _atr(candles)
        if atr_val <= 0:
            atr_val = max(entry * 0.003, abs(last.high - last.low))

        recent = closed[-12:] if len(closed) >= 12 else closed
        swing_low = min(c.low for c in recent)
        swing_high = max(c.high for c in recent)

        if trend_status == "bullish" and bullish_votes >= 1 and momentum_score >= 48.0:
            side = "long_watch"
            setup_type = "trend_continuation"
            setup_quality = "strong" if bullish_votes >= 3 else ("normal" if bullish_votes >= 2 else "speculative")
            trigger = "bullish trend continuation" + (" with order-flow/momentum confirmation" if bullish_votes >= 2 else " (trend-only)")
            retail_stop = min(swing_low, entry - (atr_val * 1.15))
        elif trend_status == "bearish" and bearish_votes >= 1 and momentum_score <= 52.0:
            side = "short_watch"
            setup_type = "trend_continuation"
            setup_quality = "strong" if bearish_votes >= 3 else ("normal" if bearish_votes >= 2 else "speculative")
            trigger = "bearish trend continuation" + (" with order-flow/momentum confirmation" if bearish_votes >= 2 else " (trend-only)")
            retail_stop = max(swing_high, entry + (atr_val * 1.15))
        else:
            return None

    wall_price = None
    wall_size = None
    smart_stop = retail_stop

    if order_book:
        if side == "long_watch":
            bids = order_book.get("bids", [])
            valid_bids = [bid for bid in bids if bid and float(bid[0]) < entry]
            if valid_bids:
                max_bid = max(valid_bids, key=lambda x: x[1])
                wall_price = float(max_bid[0])
                wall_size = float(max_bid[1])
                smart_stop = wall_price * 0.999
                if smart_stop >= entry:
                    smart_stop = retail_stop
        else:
            asks = order_book.get("asks", [])
            valid_asks = [ask for ask in asks if ask and float(ask[0]) > entry]
            if valid_asks:
                max_ask = max(valid_asks, key=lambda x: x[1])
                wall_price = float(max_ask[0])
                wall_size = float(max_ask[1])
                smart_stop = wall_price * 1.001
                if smart_stop <= entry:
                    smart_stop = retail_stop

    retail_risk = entry - retail_stop if side == "long_watch" else retail_stop - entry
    smart_risk = entry - smart_stop if side == "long_watch" else smart_stop - entry

    if retail_risk <= 0:
        atr_val = atr if atr and atr > 0 else max(_atr(candles), entry * 0.003)
        retail_stop = entry - atr_val if side == "long_watch" else entry + atr_val
        retail_risk = abs(entry - retail_stop)
    if smart_risk <= 0:
        smart_stop = retail_stop
        smart_risk = retail_risk
    if retail_risk <= 0 or smart_risk <= 0:
        return None

    retail_target = entry + retail_risk * min_rr if side == "long_watch" else entry - retail_risk * min_rr
    smart_target = entry + smart_risk * min_rr if side == "long_watch" else entry - smart_risk * min_rr

    return {
        "side": side,
        "setup_type": setup_type,
        "setup_quality": setup_quality,
        "trigger": trigger,
        "entry_reference": round(entry, 4),
        "entry_zone_low": round(entry * 0.999, 4),
        "entry_zone_high": round(entry * 1.001, 4),
        "invalidation": round(retail_stop, 4),
        "retail_stop": round(retail_stop, 4),
        "smart_stop": round(smart_stop, 4),
        "target_reference": round(retail_target, 4),
        "smart_target": round(smart_target, 4),
        "risk_per_unit": round(retail_risk, 4),
        "retail_risk_per_unit": round(retail_risk, 4),
        "smart_risk_per_unit": round(smart_risk, 4),
        "risk_reward": round(min_rr, 2),
        "wall_price": round(wall_price, 4) if wall_price is not None else None,
        "wall_size": round(wall_size, 2) if wall_size is not None else None,
        "note": "Manual signal only. Use the entry zone, invalidation, and target as a planning framework, not an auto-order.",
    }


def calculate_confidence_engine(
    mtf_score: float,
    funding_oi_score: float,
    liq_score: float,
    order_flow_score: float,
    rag_score: float,
    premortem_score: float,
    reliability_weights: dict[str, float] | None = None,
    quant_score: float = 50.0,
) -> dict[str, Any]:
    base_weights = {
        "mtf": 0.25,
        "funding_oi": 0.20,
        "liquidation": 0.15,
        "orderflow": 0.10,
        "rag": 0.10,
        "premortem": 0.05,
        "quant": 0.15,
    }

    weights = base_weights.copy()
    if reliability_weights:
        total_adjusted = 0.0
        adjusted_weights = {}
        for key in weights:
            acc = reliability_weights.get(key, 0.5)
            adj = base_weights[key] * acc
            adjusted_weights[key] = adj
            total_adjusted += adj

        if total_adjusted > 0:
            for key in weights:
                weights[key] = adjusted_weights[key] / total_adjusted

    confidence = (
        weights["mtf"] * mtf_score +
        weights["funding_oi"] * funding_oi_score +
        weights["liquidation"] * liq_score +
        weights["orderflow"] * order_flow_score +
        weights["rag"] * rag_score +
        weights["premortem"] * premortem_score +
        weights["quant"] * quant_score
    )

    conf_pct = round(max(0.0, min(100.0, confidence)))

    if conf_pct >= 90:
        grade = "A+"
    elif conf_pct >= 80:
        grade = "A"
    elif conf_pct >= 70:
        grade = "B"
    elif conf_pct >= 60:
        grade = "C"
    elif conf_pct >= 50:
        grade = "D"
    else:
        grade = "F"

    return {
        "confidence": conf_pct,
        "trade_grade": grade,
        "weights_used": {k: round(v, 4) for k, v in weights.items()}
    }


def decide_report(
    *,
    data_freshness: dict[str, Any],
    liquidity: dict[str, Any],
    trend: dict[str, Any],
    order_book: dict[str, Any],
    sweep: dict[str, Any],
    risk_idea: dict[str, Any] | None,
    confidence_engine: dict[str, Any] | None = None,
    probability_engine: dict[str, Any] | None = None,
) -> dict[str, Any]:
    gates = [
        ("data_freshness", data_freshness.get("passed", False)),
        ("liquidity", liquidity.get("passed", False)),
    ]
    for name, passed in gates:
        if not passed:
            return {"decision": "AVOID", "failed_gate": name, "confidence": 0.0, "trade_grade": "F"}

    conf = float(confidence_engine["confidence"]) if confidence_engine else 35.0
    grade = confidence_engine["trade_grade"] if confidence_engine else "C"

    if not confidence_engine:
        if trend.get("passed"):
            conf += 20.0
        if sweep.get("detected"):
            conf += 25.0
        if order_book.get("pressure") in {"buyers", "sellers"}:
            conf += 10.0
        if risk_idea:
            conf += 10.0
        conf = round(min(conf, 95.0), 2)
        if conf >= 80:
            grade = "A"
        elif conf >= 70:
            grade = "B"
        elif conf < 50:
            grade = "D"

    side = ""
    if risk_idea:
        risk_side = risk_idea.get("side", "")
        if risk_side.startswith("long"):
            side = "BUY"
        elif risk_side.startswith("short"):
            side = "SELL"
    elif sweep.get("detected"):
        direction = sweep.get("direction", "")
        if direction.startswith("bullish"):
            side = "BUY"
        elif direction.startswith("bearish"):
            side = "SELL"
    elif trend.get("status") == "bullish":
        side = "BUY"
    elif trend.get("status") == "bearish":
        side = "SELL"

    if risk_idea and side:
        if conf >= 52.0:
            decision = f"{side}_WATCH"
        else:
            decision = "HOLD"
    elif side and trend.get("passed") and conf >= 58.0:
        decision = "WATCH"
    else:
        decision = "HOLD"

    # Incorporate Quantitative EV & Probability filters as soft penalties
    ev_warning = None
    if probability_engine:
        expected_value = float(probability_engine.get("expected_value", 0.0))
        prob_up = float(probability_engine.get("probability_up", 0.5))
        prob_down = float(probability_engine.get("probability_down", 0.5))

        # Hard kill ONLY for deeply negative EV (genuine danger)
        if expected_value < -0.02:
            decision = "HOLD"
            ev_warning = f"Deeply negative EV ({expected_value:.4f}) — hard veto."
        # Soft penalty for mildly negative EV (low-vol noise)
        elif expected_value <= 0.0:
            conf = max(0.0, conf - 12.0)
            ev_warning = f"Negative EV ({expected_value:.4f}) — confidence reduced by 12%."
        # Soft penalty for weak directional probability
        if decision == "BUY_WATCH" and prob_up < 0.48:
            conf = max(0.0, conf - 8.0)
            ev_warning = (ev_warning or "") + f" Weak prob_up ({prob_up:.2f}) — confidence reduced by 8%."
        elif decision == "SELL_WATCH" and prob_down < 0.48:
            conf = max(0.0, conf - 8.0)
            ev_warning = (ev_warning or "") + f" Weak prob_down ({prob_down:.2f}) — confidence reduced by 8%."

    # Recalculate grade after any penalty adjustments
    if conf >= 90:
        grade = "A+"
    elif conf >= 80:
        grade = "A"
    elif conf >= 70:
        grade = "B"
    elif conf >= 60:
        grade = "C"
    elif conf >= 50:
        grade = "D"
    else:
        grade = "F"

    result = {
        "decision": decision,
        "failed_gate": None,
        "confidence": conf,
        "trade_grade": grade,
    }
    if ev_warning:
        result["ev_warning"] = ev_warning.strip()
    return result


def build_trade_setup(
    *,
    symbol: str,
    timeframe: str,
    decision: dict[str, Any],
    risk_idea: dict[str, Any] | None,
    atr: float,
    account_size_usd: float,
    risk_pct: float,
    ai_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    decision_name = decision.get("decision", "HOLD")
    confidence = float(decision.get("confidence", 0.0))
    grade = decision.get("trade_grade", "F")

    if not risk_idea or decision_name not in {"BUY_WATCH", "SELL_WATCH"}:
        return {
            "status": "NO_TRADE",
            "reason": "No directional setup with valid entry, stop, and target.",
            "symbol": symbol,
            "timeframe": timeframe,
        }

    side = "LONG" if decision_name.startswith("BUY") else "SHORT"
    direction_multiplier = 1 if side == "LONG" else -1

    # Check for AI-determined setup overrides
    ai_entry = (ai_result or {}).get("suggested_entry")
    ai_stop = (ai_result or {}).get("suggested_stop")
    ai_targets = (ai_result or {}).get("suggested_targets")

    if ai_entry is not None and ai_stop is not None and ai_targets:
        entry = float(ai_entry)
        stop = float(ai_stop)
        use_smart_stop = False
        retail_stop = stop
        smart_stop = stop
        risk_per_unit = abs(entry - stop)
        
        # Populate targets from AI output list
        targets = {
            "tp1_1r": round(float(ai_targets[0]), 4) if len(ai_targets) > 0 and ai_targets[0] is not None else round(entry + direction_multiplier * risk_per_unit * 1.0, 4),
            "tp2_2r": round(float(ai_targets[1]), 4) if len(ai_targets) > 1 and ai_targets[1] is not None else round(entry + direction_multiplier * risk_per_unit * 2.0, 4),
            "tp3_3r": round(float(ai_targets[2]), 4) if len(ai_targets) > 2 and ai_targets[2] is not None else round(entry + direction_multiplier * risk_per_unit * 3.0, 4),
            "runner_5r": round(float(ai_targets[3]), 4) if len(ai_targets) > 3 and ai_targets[3] is not None else round(entry + direction_multiplier * risk_per_unit * 5.0, 4),
        }
    else:
        entry = float(risk_idea["entry_reference"])
        retail_stop = float(risk_idea["retail_stop"])
        smart_stop = float(risk_idea.get("smart_stop") or retail_stop)
        retail_risk = abs(entry - retail_stop)
        smart_risk = abs(entry - smart_stop)
        min_stop_distance = max(float(atr or 0.0) * 0.25, entry * 0.001)

        use_smart_stop = (
            risk_idea.get("wall_price") is not None
            and smart_risk >= min_stop_distance
            and smart_risk <= retail_risk
        )
        stop = smart_stop if use_smart_stop else retail_stop
        risk_per_unit = abs(entry - stop)
        targets = {
            "tp1_1r": round(entry + direction_multiplier * risk_per_unit * 1.0, 4),
            "tp2_2r": round(entry + direction_multiplier * risk_per_unit * 2.0, 4),
            "tp3_3r": round(entry + direction_multiplier * risk_per_unit * 3.0, 4),
            "runner_5r": round(entry + direction_multiplier * risk_per_unit * 5.0, 4),
        }

    if risk_per_unit <= 0:
        return {
            "status": "NO_TRADE",
            "reason": "Invalid stop distance; setup rejected.",
            "symbol": symbol,
            "timeframe": timeframe,
        }

    risk_amount = account_size_usd * (risk_pct / 100.0)
    units = risk_amount / risk_per_unit if risk_per_unit > 0 else 0.0
    notional = units * entry
    stop_distance_pct = abs(entry - stop) / entry * 100.0 if entry > 0 else 999.0

    if confidence >= 95 and stop_distance_pct <= 0.40:
        confidence_cap = 50
    elif confidence >= 90 and stop_distance_pct <= 0.65:
        confidence_cap = 20
    elif confidence >= 82 and stop_distance_pct <= 1.25:
        confidence_cap = 10
    elif confidence >= 75:
        confidence_cap = 5
    elif confidence >= 68:
        confidence_cap = 3
    else:
        confidence_cap = 2

    liquidation_cap = max(1, int(60.0 / stop_distance_pct)) if stop_distance_pct > 0 else 1
    max_sensible_leverage = max(1, min(confidence_cap, liquidation_cap, 50))

    leverage_rows = []
    for leverage in [2, 3, 5, 10, 20, 50]:
        margin_required = notional / leverage if leverage > 0 else 0.0
        liquidation_distance_pct = 100.0 / leverage
        buffer_after_stop_pct = liquidation_distance_pct - stop_distance_pct
        if side == "LONG":
            approx_liquidation = entry * (1.0 - (1.0 / leverage))
        else:
            approx_liquidation = entry * (1.0 + (1.0 / leverage))
        allowed = leverage <= max_sensible_leverage and buffer_after_stop_pct > stop_distance_pct * 0.5
        leverage_rows.append(
            {
                "leverage": leverage,
                "allowed": allowed,
                "margin_required_usd": round(margin_required, 2),
                "approx_liquidation": round(approx_liquidation, 4),
                "liquidation_distance_pct": round(liquidation_distance_pct, 3),
                "buffer_after_stop_pct": round(buffer_after_stop_pct, 3),
                "verdict": "usable" if allowed else "too_aggressive_for_this_stop",
            }
        )

    usable = [row["leverage"] for row in leverage_rows if row["allowed"]]
    recommended_leverage = max(usable) if usable else 1

    ai_error = bool(ai_result and ai_result.get("error"))
    ai_complete = bool(ai_result and not ai_error)
    macro_blocked = bool((ai_result or {}).get("macro_blockout", {}).get("active"))
    if confidence < 68.0:
        status = "WATCH_ONLY"
        reason = "Directional setup exists, but confidence is below the AI-review threshold."
    elif macro_blocked:
        status = "BLOCKED_BY_MACRO"
        reason = "High-impact macro blockout is active; do not treat this as an actionable setup."
    elif ai_complete:
        status = "READY_FOR_MANUAL_REVIEW"
        reason = "Deterministic setup passed and AI review completed. Manual execution only."
    else:
        status = "AI_REVIEW_REQUIRED"
        reason = "Deterministic setup is strong enough, but AI review has not completed yet."

    return {
        "status": status,
        "reason": reason,
        "symbol": symbol,
        "timeframe": timeframe,
        "side": side,
        "confidence": round(confidence, 2),
        "grade": grade,
        "setup_type": risk_idea.get("setup_type"),
        "entry": {
            "mode": "limit_zone_then_confirmation",
            "reference": round(entry, 4),
            "zone_low": risk_idea.get("entry_zone_low"),
            "zone_high": risk_idea.get("entry_zone_high"),
        },
        "stop": {
            "selected": round(stop, 4),
            "method": "smart_wall_stop" if use_smart_stop else "structure_stop",
            "distance_pct": round(stop_distance_pct, 3),
            "risk_per_unit": round(risk_per_unit, 4),
        },
        "targets": targets,
        "position": {
            "account_size_usd": round(account_size_usd, 2),
            "risk_pct": round(risk_pct, 3),
            "risk_amount_usd": round(risk_amount, 2),
            "units": round(units, 8),
            "notional_usd": round(notional, 2),
        },
        "leverage": {
            "recommended": recommended_leverage,
            "max_sensible": max_sensible_leverage,
            "options": leverage_rows,
        },
        "rules": [
            "No manual trade unless status is READY_FOR_MANUAL_REVIEW.",
            "Cancel the setup if price hits stop before entry confirmation.",
            "Move risk only after TP1; do not widen the stop.",
            "Higher leverage is rejected when liquidation sits too close to the stop.",
        ],
    }


def build_signal_profile(
    *,
    decision: dict[str, Any],
    trend: dict[str, Any],
    order_book: dict[str, Any],
    sweep: dict[str, Any],
    risk_idea: dict[str, Any] | None,
    momentum: dict[str, Any],
    regime: str,
    squeeze: dict[str, Any],
    liquidations: dict[str, Any],
) -> dict[str, Any]:
    decision_name = decision.get("decision", "HOLD")
    confidence = float(decision.get("confidence", 0.0))
    grade = decision.get("trade_grade", "F")

    if risk_idea and risk_idea.get("side", "").startswith("long"):
        bias = "LONG"
    elif risk_idea and risk_idea.get("side", "").startswith("short"):
        bias = "SHORT"
    elif decision_name.startswith("BUY"):
        bias = "LONG"
    elif decision_name.startswith("SELL"):
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    state = "STANDBY"
    if decision_name == "AVOID":
        state = "BLOCKED"
    elif decision_name in {"BUY_WATCH", "SELL_WATCH"}:
        state = "ACTIVE_WATCH" if confidence >= 68.0 else "EARLY_WATCH"
    elif decision_name == "WATCH":
        state = "EARLY_WATCH"

    reasons: list[str] = []
    warnings: list[str] = []

    if trend.get("status"):
        reasons.append(f"Trend: {trend.get('status')} with EMA21 slope {trend.get('ema21_slope_pct', 0)}%.")
    if momentum:
        reasons.append(
            f"Momentum: {momentum.get('bias', 'neutral')} score {momentum.get('score', 50)} "
            f"(RSI {momentum.get('rsi', '-')}, volume x{momentum.get('volume_ratio', '-')})."
        )
    if order_book.get("pressure"):
        reasons.append(f"Order book pressure: {order_book.get('pressure')} imbalance {order_book.get('imbalance', 0)}.")
    if sweep.get("detected"):
        reasons.append(f"Liquidity sweep: {sweep.get('direction')} at {sweep.get('swept_level')}.")
    if risk_idea:
        reasons.append(f"Setup: {risk_idea.get('setup_type')} ({risk_idea.get('setup_quality')}) - {risk_idea.get('trigger')}.")

    squeeze_signal = squeeze.get("signal", "NEUTRAL")
    if bias == "LONG" and squeeze_signal == "POTENTIAL_LONG_SQUEEZE":
        warnings.append("Crowded longs: funding suggests downside flush risk.")
    elif bias == "SHORT" and squeeze_signal == "POTENTIAL_SHORT_SQUEEZE":
        warnings.append("Crowded shorts: funding suggests upside squeeze risk.")
    if regime in {"HIGH_VOLATILITY", "PANIC", "EUPHORIA", "BREAKOUT"}:
        warnings.append(f"{regime} regime: reduce size and demand cleaner invalidation.")
    if confidence < 52 and state != "BLOCKED":
        warnings.append("Confidence below signal threshold; wait for cleaner confirmation.")

    profile: dict[str, Any] = {
        "state": state,
        "bias": bias,
        "action": decision_name,
        "confidence": round(confidence, 2),
        "grade": grade,
        "regime": regime,
        "setup_type": risk_idea.get("setup_type") if risk_idea else None,
        "reasons": reasons[:6],
        "warnings": warnings,
        "liquidity_magnets": liquidations,
    }

    if risk_idea:
        profile.update(
            {
                "entry_zone": {
                    "low": risk_idea.get("entry_zone_low"),
                    "high": risk_idea.get("entry_zone_high"),
                    "reference": risk_idea.get("entry_reference"),
                },
                "stops": {
                    "retail": risk_idea.get("retail_stop"),
                    "smart": risk_idea.get("smart_stop"),
                },
                "targets": {
                    "retail": risk_idea.get("target_reference"),
                    "smart": risk_idea.get("smart_target"),
                },
                "risk_reward": risk_idea.get("risk_reward"),
            }
        )

    return profile
