"""Code-owned trade-plan construction and institutional release controls.

The CIO supplies an eligible direction, never execution prices or sizing.
Entry, invalidation, targets, allocation caps, and leverage are derived from
the reviewed evidence snapshot and Risk Committee limits.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Mapping

from app.indicators.market_story import evaluate_story_direction

logger = logging.getLogger(__name__)
MAX_LIVE_EVENT_CHASE_ATR = 2.5


def _finite_number(value: Any, default: float = 0.0) -> float:
    """Return a finite float without allowing malformed model output through."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _price(value: float) -> float:
    """Keep enough precision for low-priced crypto pairs as well as majors."""
    return round(value, 8)


def _value_retest_reference(side: str, current_price: float, features: Mapping[str, Any]) -> tuple[float, str]:
    """Pick a measured value reference for a non-executable research watch.

    A watch plan should describe where participation would make sense, not
    pretend that the current quote is an entry.  VWAP and profile POC are
    measured references from the same snapshot.  A long only considers values
    at/below price and a short only values at/above price, preserving the
    intended pullback/retest geometry.
    """
    vwap = features.get("vwap_context") or {}
    profile = features.get("volume_profile") or {}
    candidates = [
        (
            "completed_structure_event_retest",
            _finite_number(
                (
                    (
                        evaluate_story_direction(
                            features.get("market_story") or {},
                            "BULLISH" if side == "LONG" else "BEARISH",
                        ).get("aligned_event")
                        or {}
                    ).get("break_level")
                )
            ),
        ),
        ("anchored_vwap_retest", _finite_number(vwap.get("anchored"))),
        ("daily_vwap_retest", _finite_number(vwap.get("daily"))),
        ("weekly_vwap_retest", _finite_number(vwap.get("weekly"))),
        ("volume_profile_poc_retest", _finite_number(profile.get("poc"))),
    ]
    if side == "LONG":
        valid = [(name, value) for name, value in candidates if 0 < value <= current_price]
    else:
        valid = [(name, value) for name, value in candidates if value >= current_price > 0]
    if valid:
        return min(valid, key=lambda item: abs(item[1] - current_price))[1], min(valid, key=lambda item: abs(item[1] - current_price))[0]
    return current_price, "measured_market_reference_no_value_retest_available"


def _live_event_distance_atr(
    *,
    side: str,
    current_price: float,
    story_view: Mapping[str, Any],
    fallback_atr: float,
) -> float | None:
    event = story_view.get("aligned_event") or story_view.get("selected_event") or {}
    expected = "BULLISH" if side == "LONG" else "BEARISH"
    if str(event.get("direction") or "").upper() != expected:
        return None
    level = _finite_number(event.get("break_level"))
    event_atr = _finite_number(event.get("atr_at_event"))
    atr = event_atr if event_atr > 0 else fallback_atr
    if current_price <= 0 or level <= 0 or atr <= 0:
        return None
    signed_distance = current_price - level if side == "LONG" else level - current_price
    return signed_distance / atr


def _plan_geometry(
    *,
    side: str,
    entry: float,
    atr: float,
    min_stop_bps: float,
    liquidity_map: Mapping[str, Any],
) -> dict[str, Any]:
    risk_per_unit = max(1.5 * atr, entry * min_stop_bps / 10_000.0)
    stop = entry - risk_per_unit if side == "LONG" else entry + risk_per_unit
    protective_pool = liquidity_map.get("nearest_below" if side == "LONG" else "nearest_above") or {}
    protective_price = _finite_number(protective_pool.get("price"))
    if (side == "LONG" and 0 < protective_price < entry) or (
        side == "SHORT" and protective_price > entry
    ):
        liquidity_buffer = max(atr * 0.25, entry * 0.0005)
        stop = (
            min(stop, protective_price - liquidity_buffer)
            if side == "LONG"
            else max(stop, protective_price + liquidity_buffer)
        )
        risk_per_unit = abs(entry - stop)

    objective_pool = liquidity_map.get("nearest_above" if side == "LONG" else "nearest_below") or {}
    objective_price = _finite_number(objective_pool.get("price"))
    objective_is_directional = (
        objective_price > entry if side == "LONG" else 0 < objective_price < entry
    )
    remaining_reward = abs(objective_price - entry) if objective_is_directional else 0.0
    remaining_reward_r = (
        remaining_reward / risk_per_unit
        if risk_per_unit > 0 and remaining_reward > 0
        else 0.0
    )
    return {
        "stop": stop,
        "risk_per_unit": risk_per_unit,
        "protective_pool": protective_pool,
        "protective_price": protective_price,
        "objective_pool": objective_pool,
        "objective_price": objective_price,
        "objective_is_directional": objective_is_directional,
        "remaining_reward": remaining_reward,
        "remaining_reward_r": remaining_reward_r,
    }


def build_ai_driven_trade_setup(
    cio_result: dict[str, Any],
    features: dict[str, Any],
    settings: Any,
) -> dict[str, Any]:
    """Build a deterministic trade plan after an eligible CIO direction."""
    decision = cio_result.get("decision", "HOLD")
    confidence = _finite_number(cio_result.get("confidence_pct"))
    grade = cio_result.get("trade_grade", "F")
    symbol = cio_result.get("symbol", "")
    timeframe = cio_result.get("timeframe", "")

    dossier = cio_result.get("institutional_dossier") or {}
    thesis = dossier.get("provisional_thesis") or {}
    thesis_direction = str(thesis.get("direction", "")).upper()
    requested_side = "LONG" if decision == "BUY_WATCH" else "SHORT" if decision == "SELL_WATCH" else thesis_direction
    story = features.get("market_story") or {}
    story_view = (
        evaluate_story_direction(
            story,
            "BULLISH" if requested_side == "LONG" else "BEARISH" if requested_side == "SHORT" else "NEUTRAL",
        )
        if story.get("available")
        else {}
    )
    story_allows_entry = not story.get("available") or bool(story_view.get("actionable"))
    requested_actionable = decision in {"BUY_WATCH", "SELL_WATCH"}
    actionable = requested_actionable and story_allows_entry
    research_watch = not actionable and requested_side in {"LONG", "SHORT"}

    if not actionable and not research_watch:
        return {
            "status": "NO_TRADE",
            "reason": f"CIO decision is {decision}. No directional causal scenario is currently established.",
            "symbol": symbol,
            "timeframe": timeframe,
        }

    side = "LONG" if requested_side == "LONG" else "SHORT"
    direction = 1 if side == "LONG" else -1

    # Extract the market price and volatility from measured features.
    last_price = _finite_number(features.get("current_price"))
    if last_price <= 0:
        last_price = _finite_number((features.get("market") or {}).get("last_price"))
    if last_price <= 0:
        last_price = _finite_number((features.get("ticker") or {}).get("lastPrice"))
    if last_price <= 0 and features.get("candles"):
        latest_candle = features["candles"][-1]
        close = latest_candle.get("close") if isinstance(latest_candle, Mapping) else getattr(latest_candle, "close", 0.0)
        last_price = _finite_number(close)

    atr_val = 0.0
    try:
        # Features may arrive as full quant features or as a nested summary.
        # Try the direct path first, then fall back to the nested path.
        volatility_info = features.get("volatility") or {}
        if not volatility_info.get("atr"):
            volatility_info = (features.get("quant_features") or {}).get("volatility") or {}
        atr_val = _finite_number(volatility_info.get("atr"))
    except AttributeError:
        atr_val = 0.0

    if atr_val <= 0 or not math.isfinite(atr_val):
        atr_val = last_price * 0.015

    live_event_distance_atr = (
        _live_event_distance_atr(
            side=side,
            current_price=last_price,
            story_view=story_view,
            fallback_atr=atr_val,
        )
        if actionable
        else None
    )
    live_chase_blocker = ""
    if live_event_distance_atr is not None and live_event_distance_atr > MAX_LIVE_EVENT_CHASE_ATR:
        actionable = False
        research_watch = True
        live_chase_blocker = (
            f"Live quote is {live_event_distance_atr:.2f} ATR beyond the completed "
            "event level; the entry has moved away and chasing is prohibited."
        )

    # Models are not permitted to invent execution levels. Approved setups use
    # the current measured quote; a non-executable context is displayed at a
    # measured event/VWAP/profile retest instead of masquerading as a live entry.
    entry, entry_mode = (
        (last_price, "measured_market_reference")
        if actionable
        else _value_retest_reference(side, last_price, features)
    )
    if entry <= 0:
        return {
            "status": "NO_TRADE",
            "reason": "A valid measured market price is required.",
            "symbol": symbol,
            "timeframe": timeframe,
        }

    min_stop_bps = max(_finite_number(getattr(settings, "institutional_min_stop_distance_bps", 25.0), 25.0), 1.0)
    liquidity_map = features.get("liquidity_map") or {}
    geometry = _plan_geometry(
        side=side,
        entry=entry,
        atr=atr_val,
        min_stop_bps=min_stop_bps,
        liquidity_map=liquidity_map,
    )
    stop = geometry["stop"]
    risk_per_unit = geometry["risk_per_unit"]
    protective_pool = geometry["protective_pool"]
    protective_price = geometry["protective_price"]
    objective_pool = geometry["objective_pool"]
    objective_price = geometry["objective_price"]
    objective_is_directional = geometry["objective_is_directional"]
    remaining_reward = geometry["remaining_reward"]
    remaining_reward_r = geometry["remaining_reward_r"]
    minimum_remaining_reward_r = 1.5
    reward_space_adequate = (
        objective_is_directional and remaining_reward_r >= minimum_remaining_reward_r
        if story.get("available")
        else True
    )
    reward_space_blocker = ""
    release_reward_r = remaining_reward_r
    release_reward_distance = remaining_reward
    release_objective_price = objective_price
    release_objective_kind = objective_pool.get("kind") if objective_is_directional else None
    if actionable and not reward_space_adequate:
        actionable = False
        research_watch = True
        reward_space_blocker = (
            f"Only {remaining_reward_r:.2f}R remains to the next measured liquidity objective."
            if objective_is_directional
            else "No unconsumed directional liquidity objective is available."
        )
        # The release gate was evaluated at the live quote. Once it downgrades
        # the setup, rebuild the displayed research plan from a measured retest
        # rather than leaving the rejected live quote labelled as its entry.
        entry, entry_mode = _value_retest_reference(side, last_price, features)
        geometry = _plan_geometry(
            side=side,
            entry=entry,
            atr=atr_val,
            min_stop_bps=min_stop_bps,
            liquidity_map=liquidity_map,
        )
        stop = geometry["stop"]
        risk_per_unit = geometry["risk_per_unit"]
        protective_pool = geometry["protective_pool"]
        protective_price = geometry["protective_price"]
        objective_pool = geometry["objective_pool"]
        objective_price = geometry["objective_price"]
        objective_is_directional = geometry["objective_is_directional"]
        remaining_reward = geometry["remaining_reward"]
        remaining_reward_r = geometry["remaining_reward_r"]

    # Deterministic payoff ladder (1.5R, 2.5R, 3.5R, 5.0R).
    tp1 = entry + direction * risk_per_unit * 1.5
    tp2 = entry + direction * risk_per_unit * 2.5
    tp3 = entry + direction * risk_per_unit * 3.5
    runner = entry + direction * risk_per_unit * 5.0

    targets = {
        "tp1_1r": _price(tp1),
        "tp2_2r": _price(tp2),
        "tp3_3r": _price(tp3),
        "runner_5r": _price(runner),
    }

    # Position sizing
    account_size = float(settings.default_account_size_usd)
    configured_risk_pct = float(settings.default_risk_per_idea_pct)
    risk_committee = dossier.get("risk_committee") or {}
    allocation = risk_committee.get("allocation_ceiling") or {}
    committee_risk_pct = max(_finite_number(allocation.get("risk_fraction")) * 100.0, 0.0)
    risk_pct = min(configured_risk_pct, committee_risk_pct) if actionable and committee_risk_pct > 0 else 0.0
    risk_amount = account_size * (risk_pct / 100.0)
    units = risk_amount / risk_per_unit
    notional = units * entry
    notional_ceiling = max(_finite_number(allocation.get("max_notional_usd")), 0.0)
    if notional_ceiling > 0 and notional > notional_ceiling:
        notional = notional_ceiling
        units = notional / entry
        risk_amount = units * risk_per_unit
        risk_pct = risk_amount / account_size * 100.0 if account_size > 0 else 0.0
    stop_distance_pct = (risk_per_unit / entry) * 100.0 if entry > 0 else 999.0

    # Max leverage caps based on confidence and distance
    leverage_cap = 10
    if confidence >= 90:
        leverage_cap = 10
    elif confidence >= 80:
        leverage_cap = 20
    elif confidence >= 70:
        leverage_cap = 10
    else:
        leverage_cap = 5

    leverage_cap = min(leverage_cap, max(1, int(getattr(settings, "institutional_max_leverage", 5))))

    # Leverage options
    leverage_rows = []
    for lev in [2, 3, 5, 10, 20, 50]:
        margin_required = notional / lev
        liq_distance_pct = 100.0 / lev
        buffer = liq_distance_pct - stop_distance_pct
        approx_liq = entry * (1.0 - (1.0 / lev)) if side == "LONG" else entry * (1.0 + (1.0 / lev))
        allowed = lev <= leverage_cap and buffer > (stop_distance_pct * 0.3)
        leverage_rows.append({
            "leverage": lev,
            "allowed": allowed,
            "margin_required_usd": round(margin_required, 2),
            "approx_liquidation": round(approx_liq, 4),
            "liquidation_distance_pct": round(liq_distance_pct, 3),
            "buffer_after_stop_pct": round(buffer, 3),
            "verdict": "usable" if allowed else "too_aggressive_for_this_stop",
        })

    usable = [row["leverage"] for row in leverage_rows if row["allowed"]]
    recommended_leverage = max(usable) if usable else 1

    # Status determination
    allocation_tier = risk_committee.get("allocation_tier", "RESEARCH_ONLY")
    macro_blocked = bool((cio_result.get("macro_blockout") or {}).get("active"))
    if macro_blocked:
        status = "BLOCKED_BY_MACRO"
        reason = "Macro blockout is active; signal publication suppressed."
    elif research_watch:
        blocker = (
            reward_space_blocker
            if reward_space_blocker
            else live_chase_blocker
            if live_chase_blocker
            else
            (
                f"market story is {story_view.get('state', 'not actionable')}: "
                f"{story_view.get('reason') or 'the original structural opportunity is no longer available'}"
            )
            if requested_actionable and not story_allows_entry
            else next(iter(risk_committee.get("hard_blockers") or []), "allocation approval and live confirmation are still required")
        )
        status = "WATCH_ONLY"
        reason = f"Directional causal context is mapped as a value-retest watch; no allocation is authorized yet: {blocker}"
    elif allocation_tier == "CONDITIONAL_MANUAL_REVIEW" and confidence >= 60:
        status = "CONDITIONAL_MANUAL_REVIEW"
        reason = "Positive edge passed hard controls with reduced sizing because validation or portfolio coverage is incomplete."
    elif confidence >= 65:
        status = "READY_FOR_MANUAL_REVIEW"
        reason = "Institutional controls passed. The deterministic plan is ready for manual review."
    else:
        status = "WATCH_ONLY"
        reason = f"CIO confidence is below the manual-review threshold ({confidence}%)."

    return {
        "status": status,
        "reason": reason,
        "symbol": symbol,
        "timeframe": timeframe,
        "side": side,
        "confidence": confidence,
        "grade": grade,
        "setup_type": "completed_candle_event_allocation" if actionable else "causal_value_retest_watch",
        "execution_permitted": actionable and status in {"READY_FOR_MANUAL_REVIEW", "CONDITIONAL_MANUAL_REVIEW"},
        "market_story": {
            "state": "EXTENDED_DO_NOT_CHASE" if live_chase_blocker else story_view.get("state", "UNAVAILABLE"),
            "actionable": (
                False
                if live_chase_blocker
                else bool(story_view.get("actionable"))
                if story_view
                else None
            ),
            "reason": live_chase_blocker or story_view.get("reason"),
            "selected_event": story_view.get("aligned_event"),
            "chase_prohibited": bool(live_chase_blocker or story_view.get("chase_prohibited")),
            "entry_timing": (story_view.get("aligned_event") or {}).get("entry_timing"),
            "campaign_maturity": (story_view.get("aligned_event") or {}).get("campaign_maturity"),
            "campaign_origin_price": (story_view.get("aligned_event") or {}).get("campaign_origin_price"),
            "campaign_distance_atr": (story_view.get("aligned_event") or {}).get("campaign_distance_atr_current"),
            "campaign_max_entry_atr": (story_view.get("aligned_event") or {}).get("campaign_max_entry_atr"),
            "live_quote_distance_atr": (
                round(live_event_distance_atr, 3)
                if live_event_distance_atr is not None
                else None
            ),
            "live_quote": _price(last_price),
        },
        "remaining_reward": {
            "adequate": reward_space_adequate,
            "minimum_required_r": minimum_remaining_reward_r,
            "to_liquidity_objective_r": round(
                release_reward_r if reward_space_blocker else remaining_reward_r,
                3,
            ),
            "distance": _price(
                release_reward_distance if reward_space_blocker else remaining_reward
            ),
            "objective_price": (
                _price(release_objective_price)
                if reward_space_blocker and release_objective_price > 0
                else _price(objective_price)
                if objective_is_directional
                else None
            ),
            "objective_kind": (
                release_objective_kind
                if reward_space_blocker
                else objective_pool.get("kind")
                if objective_is_directional
                else None
            ),
            "displayed_retest_reward_r": round(remaining_reward_r, 3),
            "reason": reward_space_blocker or "Sufficient measured reward remains before the next liquidity objective.",
        },
        "allocation_tier": allocation_tier,
        "committee_restrictions": (((cio_result.get("institutional_dossier") or {}).get("risk_committee") or {}).get("restrictions", [])),
        "entry": {
            "mode": entry_mode,
            "reference": _price(entry),
            # Keep the entire entry zone well clear of the invalidation stop.
            # A fixed +/-0.2% zone previously allowed a narrow stop to land
            # inside the zone and produced instant stop-outs.
            "zone_low": _price(entry - min(entry * 0.002, risk_per_unit * 0.25)),
            "zone_high": _price(entry + min(entry * 0.002, risk_per_unit * 0.25)),
            "zone_half_width": _price(min(entry * 0.002, risk_per_unit * 0.25)),
            "zone_method": "capped_at_25pct_of_stop_distance",
        },
        "stop": {
            "selected": _price(stop),
            "method": "max_atr_invalidation_1_5x_or_minimum_stop_distance",
            "atr": _price(atr_val),
            "distance_pct": round(stop_distance_pct, 3),
            "risk_per_unit": _price(risk_per_unit),
            "minimum_distance_bps": round(min_stop_bps, 2),
            "liquidity_reference": protective_price if protective_price > 0 else None,
            "liquidity_reference_kind": protective_pool.get("kind") if protective_price > 0 else None,
        },
        "targets": targets,
        "liquidity_objective": objective_pool or None,
        "position": {
            "account_size_usd": round(account_size, 2),
            "risk_pct": round(risk_pct, 3),
            "risk_amount_usd": round(risk_amount, 2),
            "units": round(units, 8),
            "notional_usd": round(notional, 2),
        },
        "leverage": {
            "recommended": recommended_leverage,
            "max_sensible": leverage_cap,
            "options": leverage_rows,
        },
        "rules": [
            "Confirm trade details manually. Do not auto-execute.",
            "Move stop to entry after TP1 hits.",
            "Never widen the stop loss.",
        ],
    }


def evaluate_ai_driven_approval(
    cio_result: dict[str, Any],
    trade_setup: dict[str, Any],
    *,
    require_live_confirmation: bool = True,
) -> dict[str, Any]:
    """Evaluate whether the AI Council output meets criteria for publication."""
    blockers = []
    decision = cio_result.get("decision", "HOLD")
    confidence = float(cio_result.get("confidence_pct", 0))
    grade = cio_result.get("trade_grade", "F")
    
    if decision not in {"BUY_WATCH", "SELL_WATCH"}:
        blockers.append(f"AI CIO decision is {decision} (needs BUY_WATCH or SELL_WATCH).")
    
    allocation_tier = (((cio_result.get("institutional_dossier") or {}).get("risk_committee") or {}).get("allocation_tier"))
    confidence_threshold = 60.0 if allocation_tier == "CONDITIONAL_MANUAL_REVIEW" else 65.0
    if confidence < confidence_threshold:
        blockers.append(f"CIO confidence {confidence}% is below the {allocation_tier or 'institutional'} release threshold of {confidence_threshold:.0f}%.")
        
    if grade not in {"A+", "A", "B"}:
        blockers.append(f"AI trade grade {grade} is below the release requirement (B or higher).")
        
    if (cio_result.get("macro_blockout") or {}).get("active"):
        blockers.append("High-impact economic calendar event blockout active.")

    # Institutional controls are code-owned and cannot be overridden by the
    # CIO model's decision, confidence, grade, or prose.
    dossier = cio_result.get("institutional_dossier") or {}
    risk_committee = dossier.get("risk_committee") or {}
    adversarial = dossier.get("adversarial_review") or {}
    evidence_manifest = dossier.get("evidence_manifest") or {}
    if decision in {"BUY_WATCH", "SELL_WATCH"} and not dossier:
        blockers.append("An institutional evidence dossier is required for publication.")
    if decision in {"BUY_WATCH", "SELL_WATCH"} and not evidence_manifest:
        blockers.append("A deterministic evidence manifest is required for publication.")
    elif decision in {"BUY_WATCH", "SELL_WATCH"} and not evidence_manifest.get("core_ready", False):
        missing = ", ".join(evidence_manifest.get("missing_required", [])) or "unknown controls"
        blockers.append(f"Required evidence validation is incomplete: {missing}.")
    if dossier and not risk_committee.get("approved_for_allocation", False):
        blockers.extend(str(item) for item in risk_committee.get("hard_blockers", []) if item)
        if not risk_committee.get("hard_blockers"):
            blockers.append("Risk Committee vetoed allocation.")
    if adversarial.get("veto"):
        blockers.append(f"Adversarial Review vetoed allocation at severity {adversarial.get('severity_score', 'unknown')}/10.")

    data_quality = cio_result.get("data_quality")
    if data_quality is not None and not data_quality.get("passed", False):
        blockers.append("Signal blocked because required market data is incomplete.")

    # A committee narrative cannot override the deterministic Radar-equivalent
    # structure and execution verifier at the point a signal is published.
    live_confirmation = cio_result.get("live_confirmation")
    if require_live_confirmation and not isinstance(live_confirmation, dict):
        blockers.append("Live confirmation is required before an actionable setup can be published.")
    elif live_confirmation is not None and not live_confirmation.get("passed", False):
        blockers.append(f"Live confirmation failed: {live_confirmation.get('reason') or 'required structure or execution evidence is absent.'}")
        
    if trade_setup.get("status") == "NO_TRADE":
        blockers.append("Failed to build valid trade parameters.")
    story_view = trade_setup.get("market_story") or {}
    if decision in {"BUY_WATCH", "SELL_WATCH"} and story_view.get("actionable") is False:
        blockers.append(
            f"Completed-candle market story is {story_view.get('state', 'not actionable')}: "
            f"{story_view.get('reason') or 'the structural entry is unavailable.'}"
        )
    remaining_reward = trade_setup.get("remaining_reward") or {}
    if decision in {"BUY_WATCH", "SELL_WATCH"} and remaining_reward.get("adequate") is False:
        blockers.append(
            remaining_reward.get("reason")
            or "Insufficient reward remains before the next measured liquidity objective."
        )
    if decision in {"BUY_WATCH", "SELL_WATCH"} and trade_setup.get("execution_permitted") is False:
        blockers.append("The deterministic trade plan is research-only and cannot authorize signal publication.")
    position = trade_setup.get("position") or {}
    if decision in {"BUY_WATCH", "SELL_WATCH"} and (
        _finite_number(position.get("risk_amount_usd")) <= 0
        or _finite_number(position.get("units")) <= 0
    ):
        blockers.append("Risk Committee allocation ceiling produced a zero-sized position.")

    side = "LONG" if decision == "BUY_WATCH" else "SHORT" if decision == "SELL_WATCH" else None

    # Tally how many agents support the setup
    agreement = cio_result.get("agent_agreement", {})
    support_votes = agreement.get("bullish", 0) if side == "LONG" else agreement.get("bearish", 0)
    
    # Four evidence engines can express direction; Risk and Adversarial Review
    # are controls rather than votes. The committee config sets the minimum.
    minimum_support = max(1, int(risk_committee.get("minimum_directional_engines", 2)))
    if side and support_votes < minimum_support:
        blockers.append(f"Insufficient engine alignment ({support_votes} engines in favor; needs >={minimum_support}).")

    return {
        "approved": not blockers,
        "side": side,
        "confidence": confidence,
        "confirmations": support_votes,
        "blockers": list(dict.fromkeys(blockers)),
        "validation_coverage": evidence_manifest,
        "summary": "AI Council approved signal release." if not blockers else blockers[0],
    }
