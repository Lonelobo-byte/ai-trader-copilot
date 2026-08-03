"""Shared deterministic structure and live-execution confirmation gates.

Radar and the main signal path use this module so a trade cannot be approved
by one screen while being rejected as ``LIVE CHECK FAILED`` by the other.
"""
from __future__ import annotations

from time import time
from typing import Any

from app.data_sources.binance_public import Candle, completed_candles
from app.data_sources.execution_tape_ws import (
    MIN_PUBLICATION_SOURCES,
    MIN_PUBLICATION_VENUES,
    publication_flow_is_qualified,
)
from app.indicators.market_story import (
    build_market_story,
    evaluate_story_direction,
    evaluate_story_playbook,
    observable_liquidity_sweep,
    observable_structure_events,
)
from app.indicators.structure import classify_market_phase
from app.quant.market_context import build_volume_profile, build_vwap_context

MIN_EXECUTION_TAPE_SOURCES = MIN_PUBLICATION_SOURCES


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _observable_structure_events(candles: list[Candle]) -> dict[str, dict[str, Any]]:
    """Compatibility projection from the canonical completed-candle story."""
    return observable_structure_events(build_market_story(candles))


def apply_live_confirmation(candidate: dict[str, Any], live: dict[str, Any]) -> None:
    """Apply the exact direction-aware Radar execution checks to a candidate."""
    direction = candidate["direction"]
    imbalance = live.get("depth_imbalance")
    taker_ratio = live.get("taker_buy_sell_ratio")
    funding = _number(live.get("funding_rate"))
    oi_change = live.get("oi_change_pct")
    spread_bps = live.get("spread_bps")
    execution_tape = (
        live.get("execution_tape")
        if isinstance(live.get("execution_tape"), dict)
        else live.get("multi_venue")
        if isinstance(live.get("multi_venue"), dict)
        else {}
    )
    actual_flow = execution_tape.get("actual_flow", {}) or {}
    tape_available = bool(actual_flow.get("available"))
    tape_source_count = int(actual_flow.get("qualified_source_count") or 0)
    tape_venue_count = int(actual_flow.get("qualified_venue_count") or 0)
    tape_production_qualified = publication_flow_is_qualified(execution_tape)
    tape_bias = str(actual_flow.get("bias", "UNAVAILABLE")).upper()
    tape_verdict = str(actual_flow.get("status", "UNAVAILABLE")).upper()
    opposite_direction = "BEARISH" if direction == "BULLISH" else "BULLISH"
    tape_opposed = tape_available and tape_bias == opposite_direction
    tape_direction_confirmed = (
        tape_production_qualified
        and tape_bias == direction
        and (
            (direction == "BULLISH" and tape_verdict == "BUYING_CONFIRMED")
            or (direction == "BEARISH" and tape_verdict == "SELLING_CONFIRMED")
        )
    )
    production_tape_required = bool(
        candidate.get("causal_radar") or candidate.get("require_production_tape")
    )
    quote_stability = execution_tape.get("displayed_liquidity_stability", {}) or {}
    quote_stability_status = str(quote_stability.get("status", "UNAVAILABLE")).upper()
    context_actionability = ((candidate.get("market_context") or {}).get("actionability") or {})
    market_story_evaluated = context_actionability.get("actionable") is not None
    market_story_actionable = bool(context_actionability.get("actionable")) if market_story_evaluated else True
    story_event = (
        context_actionability.get("aligned_event")
        or context_actionability.get("selected_event")
        or {}
    )
    story_event_level = _number(story_event.get("break_level"))
    story_event_atr = _number(story_event.get("atr_at_event"))
    story_invalidation_level = _number(story_event.get("invalidation_level"))
    live_price = _number(live.get("current_price"))
    story_event_direction = str(story_event.get("direction", "")).upper()
    story_live_location_evaluated = bool(
        market_story_evaluated
        and live_price > 0
        and story_event_level > 0
        and story_event_atr > 0
        and story_event_direction == direction
    )
    story_live_distance_atr = (
        (live_price - story_event_level) / story_event_atr
        if story_live_location_evaluated and direction == "BULLISH"
        else (story_event_level - live_price) / story_event_atr
        if story_live_location_evaluated
        else None
    )
    story_live_location_not_chased = (
        story_live_distance_atr <= 2.5
        if story_live_distance_atr is not None
        else not candidate.get("causal_radar") or not market_story_evaluated
    )
    story_live_invalidation_evaluated = bool(
        market_story_evaluated
        and live_price > 0
        and story_invalidation_level > 0
        and story_event_direction == direction
    )
    story_live_invalidation_held = (
        live_price >= story_invalidation_level
        if story_live_invalidation_evaluated and direction == "BULLISH"
        else live_price <= story_invalidation_level
        if story_live_invalidation_evaluated
        else not candidate.get("causal_radar") and not candidate.get("require_production_tape")
    )
    structure_confirmation = candidate.get("structure_confirmation") or {}
    structure_gate_evaluated = "passed" in structure_confirmation
    structure_gate_passed = bool(structure_confirmation.get("passed")) if structure_gate_evaluated else True
    depth_aligned = (
        imbalance is not None
        and ((direction == "BULLISH" and imbalance >= 0.02) or (direction == "BEARISH" and imbalance <= -0.02))
    )
    legacy_flow_aligned = (
        taker_ratio is not None
        and (
            (direction == "BULLISH" and taker_ratio >= 1.02)
            or (direction == "BEARISH" and taker_ratio <= 0.98)
        )
    )
    # Production Radar/signal authorization requires normalized proof from at
    # least two independent public feeds. Legacy analytical callers may still
    # inspect completed Binance taker ratios, but that evidence cannot publish.
    flow_aligned = (
        tape_direction_confirmed
        if production_tape_required or tape_available
        else legacy_flow_aligned
    )
    price_change = live.get("price_change_pct")
    positioning_aligned = (
        oi_change is not None and abs(_number(oi_change)) >= 0.10
        and (
            # The callable also supports legacy callers that did not supply a
            # price window. The main signal path always supplies one.
            price_change is None
            or (direction == "BULLISH" and _number(price_change) > 0.0)
            or (direction == "BEARISH" and _number(price_change) < 0.0)
        )
    )
    liquid = spread_bps is not None and spread_bps <= 12.0
    crowded = (direction == "BULLISH" and funding > 0.0005) or (direction == "BEARISH" and funding < -0.0005)
    planned_notional = _number(live.get("planned_notional_usd"))
    opposing_depth = _number(live.get("opposing_depth_notional"))
    execution_capacity_evaluated = planned_notional > 0 and opposing_depth > 0
    # Reserve most displayed liquidity for other participants and for quote
    # cancellation between observation and manual execution.
    execution_capacity_sufficient = (
        planned_notional <= opposing_depth * 0.10
        if execution_capacity_evaluated
        else True
    )

    live_points = sum((8 if liquid else 0, 10 if depth_aligned else 0, 10 if flow_aligned else 0, 7 if positioning_aligned else 0, 5 if not crowded else 0))
    # Causal Radar rows already contain the complete market-context score.
    # Live checks qualify that score; they never inflate it with a second,
    # incompatible point system. Legacy callers retain their prior behavior.
    if not candidate.get("causal_radar"):
        candidate["score"] = min(100, int(candidate["score"]) + live_points)
    # Displayed depth is useful context but it is not durable proof: a single
    # snapshot can be cancelled or spoofed.  Publication therefore needs the
    # two harder-to-fake observations together (aggressive taker flow and a
    # matching price/OI regime).  Depth remains visible as supporting or
    # contradictory evidence instead of becoming a one-snapshot veto.
    execution_evidence_confirmed = flow_aligned and positioning_aligned
    checks = {
        "data_complete": bool(live.get("data_complete")),
        "spread_within_limit": liquid,
        "depth_aligned": depth_aligned,
        "taker_flow_aligned": flow_aligned,
        "price_oi_aligned": positioning_aligned,
        "funding_not_crowded": not crowded,
        "execution_evidence_confirmed": execution_evidence_confirmed,
        "actual_flow_aligned": flow_aligned,
        "actual_flow_not_opposed": not tape_opposed,
        "execution_capacity_sufficient": execution_capacity_sufficient,
        "market_story_actionable": market_story_actionable,
        "market_story_live_location_not_chased": story_live_location_not_chased,
        "market_story_live_invalidation_held": story_live_invalidation_held,
        "shared_structure_playbook_passed": structure_gate_passed,
    }
    risk_flags = candidate.setdefault("risk_flags", [])
    messages = {
        "data_complete": "Live depth, funding, open-interest, or taker-flow data is incomplete.",
        "spread_within_limit": "Live spread exceeds the execution-quality limit.",
        "depth_aligned": "Displayed 20-level order-book depth does not support the proposed direction (a snapshot only; not used as a standalone veto).",
        "taker_flow_aligned": "Measured market-order aggression does not support the proposed direction.",
        "price_oi_aligned": "Price and open interest do not form an aligned positioning regime.",
        "funding_not_crowded": "Funding is crowded in the proposed direction; squeeze/flush risk is elevated.",
        "execution_evidence_confirmed": "Actual aggressor flow and price/OI positioning are not jointly aligned.",
        "actual_flow_aligned": (
            f"Binance/Bybit execution tape is {tape_verdict.lower().replace('_', ' ')} "
            f"with {tape_bias.lower()} pressure; confirmed {direction.lower()} aggression is required."
        ),
        "actual_flow_not_opposed": "The normalized execution tape is actively opposed to the proposed direction.",
        "execution_capacity_sufficient": "Planned notional exceeds 10% of the displayed 20-level opposing-side depth.",
        "market_story_actionable": (
            f"Completed-candle market story is {context_actionability.get('state', 'not actionable')}: "
            f"{context_actionability.get('reason') or 'the original entry is no longer available.'}"
        ),
        "market_story_live_location_not_chased": (
            f"Live price is {story_live_distance_atr:.2f} event ATR beyond the completed-candle "
            "level; the Radar review location has moved away."
            if story_live_distance_atr is not None
            else "Live price could not be reconciled to the originating completed-candle event."
        ),
        "market_story_live_invalidation_held": (
            f"Live price {live_price:.6g} breached the {direction.lower()} event invalidation "
            f"at {story_invalidation_level:.6g}; the completed-candle retest is no longer valid."
            if story_live_invalidation_evaluated
            else "Live price could not be reconciled to the selected event invalidation level."
        ),
        "shared_structure_playbook_passed": (
            f"Shared structure playbook is not ready: "
            f"{structure_confirmation.get('reason') or 'event quality or higher-timeframe alignment is incomplete.'}"
        ),
    }
    required_names = (
        "data_complete", "spread_within_limit", "funding_not_crowded",
        "execution_evidence_confirmed", "execution_capacity_sufficient",
    )
    if candidate.get("causal_radar") and market_story_evaluated:
        required_names = (
            *required_names,
            "market_story_actionable",
            "market_story_live_location_not_chased",
            "market_story_live_invalidation_held",
        )
    elif candidate.get("require_production_tape") and market_story_evaluated:
        required_names = (*required_names, "market_story_live_invalidation_held")
    if candidate.get("causal_radar") and structure_gate_evaluated:
        required_names = (*required_names, "shared_structure_playbook_passed")
    required_checks = {key: checks[key] for key in required_names}
    risk_flags.extend(message for key, message in messages.items() if key in required_checks and not checks[key])
    supporting_warnings = [messages["depth_aligned"]] if not depth_aligned else []
    if not tape_available and production_tape_required:
        supporting_warnings.append(
            "The Binance/Bybit execution tape is unavailable; two-source actual-flow proof is required for publication."
        )
    elif not tape_available:
        supporting_warnings.append(
            "The Binance/Bybit execution tape is warming or unavailable; completed taker-volume evidence is being used as a fallback."
        )
    elif not tape_production_qualified:
        supporting_warnings.append(
            f"Actual flow is observed on {tape_source_count} source(s) across "
            f"{tape_venue_count} venue(s); publication requires at least "
            f"{MIN_EXECUTION_TAPE_SOURCES} qualified sources across "
            f"{MIN_PUBLICATION_VENUES} independent exchanges without venue disagreement."
        )
    elif not tape_direction_confirmed:
        supporting_warnings.append(
            f"Actual-flow verdict is {tape_verdict.lower().replace('_', ' ')}; "
            "aggression without matching price progress is not directional confirmation."
        )
    if quote_stability_status == "UNAVAILABLE":
        supporting_warnings.append(
            "Incremental quote stability is not yet qualified; spoofing cannot be inferred from the available public data."
        )
    elif quote_stability_status == "WATCH":
        supporting_warnings.append(
            "One venue shows elevated displayed-liquidity instability; this is a cancellation-risk warning, not proof of spoofing."
        )
    if not execution_capacity_evaluated:
        supporting_warnings.append("Displayed-depth capacity was not evaluated because no positive planned notional was supplied.")
    candidate["advanced_confirmation"] = {
        **live,
        "checks": checks,
        "required_checks": required_checks,
        "depth_evidence": "SUPPORTIVE" if depth_aligned else "CONTRADICTORY_SNAPSHOT",
        "supporting_warnings": supporting_warnings,
        "live_points": live_points,
        "market_story_live_location": {
            "evaluated": story_live_location_evaluated,
            "current_price": live_price if live_price > 0 else None,
            "event_level": story_event_level if story_event_level > 0 else None,
            "event_atr": story_event_atr if story_event_atr > 0 else None,
            "distance_atr": (
                round(story_live_distance_atr, 3)
                if story_live_distance_atr is not None
                else None
            ),
            "maximum_distance_atr": 2.5,
            "passed": story_live_location_not_chased,
            "invalidation_level": (
                story_invalidation_level if story_invalidation_level > 0 else None
            ),
            "invalidation_evaluated": story_live_invalidation_evaluated,
            "invalidation_held": story_live_invalidation_held,
        },
        "execution_capacity": {
            "evaluated": execution_capacity_evaluated,
            "planned_notional_usd": round(planned_notional, 2),
            "opposing_depth_notional": round(opposing_depth, 2),
            "maximum_notional_at_10pct_depth": round(opposing_depth * 0.10, 2),
        },
        "displayed_liquidity_stability": quote_stability,
        "actual_flow_evidence": {
            "available": tape_available,
            "status": tape_verdict,
            "bias": tape_bias,
            "aligned": tape_direction_confirmed,
            "active_aggressor": actual_flow.get("active_aggressor", "UNAVAILABLE"),
            "buy_notional": actual_flow.get("buy_notional"),
            "sell_notional": actual_flow.get("sell_notional"),
            "net_delta_usd": actual_flow.get("net_delta_usd"),
            "cvd_trend": actual_flow.get("cvd_trend", "UNAVAILABLE"),
            "price_response": actual_flow.get("price_response", "UNAVAILABLE"),
            "absorption": actual_flow.get("absorption", "NOT_DETECTED"),
            "exhaustion": actual_flow.get("exhaustion", "NONE"),
            "confidence": actual_flow.get("confidence", "UNAVAILABLE"),
            "cross_market_alignment": actual_flow.get(
                "cross_market_alignment", "UNAVAILABLE"
            ),
            "qualified_source_count": actual_flow.get("qualified_source_count", 0),
            "qualified_venue_count": actual_flow.get("qualified_venue_count", 0),
            "minimum_publication_sources": MIN_EXECUTION_TAPE_SOURCES,
            "minimum_publication_venues": MIN_PUBLICATION_VENUES,
            "production_qualified": tape_production_qualified,
        },
    }
    accepted = all(required_checks.values()) and candidate["score"] >= (65 if candidate.get("causal_radar") else 75)
    candidate["review_status"] = "REVIEW_CANDIDATE" if accepted else "WATCH_ONLY"
    candidate["status"] = "LIVE_CONFIRMED_REVIEW" if accepted else "LIVE_CONFIRMATION_REJECTED"
    candidate["quality_badge"] = "LIVE CHECK PASSED" if accepted else "LIVE CHECK FAILED"


def verify_main_signal_snapshot(
    *, symbol: str, timeframe: str, side: str | None, candles: list[Candle],
    higher_candles: list[Candle], order_book: dict[str, Any], funding: dict[str, Any],
    derivatives: dict[str, Any], multi_venue: dict[str, Any] | None = None,
    planned_notional_usd: float | None = None,
) -> dict[str, Any]:
    """Fail closed for new main-system signals unless Radar-equivalent checks pass.

    This performs no second network request: it uses the exact snapshot already
    reviewed by the committee, preventing a venue/timing mismatch.
    """
    direction = "BULLISH" if side == "LONG" else "BEARISH" if side == "SHORT" else "NEUTRAL"
    now_ms = int(time() * 1000)
    primary = [c for c in completed_candles(candles) if c.close_time <= now_ms]
    higher = [c for c in completed_candles(higher_candles) if c.close_time <= now_ms]
    structure_checks: dict[str, bool] = {
        "directional_plan": direction != "NEUTRAL",
        "completed_primary_candles": len(primary) >= 55,
        "completed_higher_candles": len(higher) >= 55,
    }
    risk_flags: list[str] = []
    # Build observational evidence before the directional fail-closed branch.
    # A HOLD/NEUTRAL result still needs to explain the completed-candle phase,
    # structure, liquidity event and participation visible to the researcher.
    primary_phase = classify_market_phase(primary) if structure_checks["completed_primary_candles"] else "UNAVAILABLE"
    higher_phase = classify_market_phase(higher) if structure_checks["completed_higher_candles"] else "UNAVAILABLE"
    latest = primary[-1] if primary else None
    candle_range = (
        max(_number(latest.high) - _number(latest.low), 1e-12)
        if latest is not None else 0.0
    )
    body_ratio = (
        abs(_number(latest.close) - _number(latest.open)) / candle_range
        if latest is not None and candle_range > 0 else 0.0
    )
    average_volume = (
        sum(_number(c.quote_volume) for c in primary[-21:-1]) / 20.0
        if len(primary) >= 21 else 0.0
    )
    rvol = (
        _number(latest.quote_volume) / average_volume
        if latest is not None and average_volume > 0 else 0.0
    )
    primary_story = build_market_story(primary) if primary else {
        "available": False,
        "current_state": "UNAVAILABLE",
        "structure_events": [],
        "liquidity_events": [],
    }
    higher_story = build_market_story(higher) if structure_checks["completed_higher_candles"] else {
        "available": False,
        "current_state": "UNAVAILABLE",
        "structure_events": [],
        "liquidity_events": [],
    }
    events = observable_structure_events(primary_story)
    bos, choch = events["bos"], events["choch"]
    observed_sweep = primary_story.get("latest_liquidity_event") or {}
    observed_event = primary_story.get("latest_event") or {}
    # Tactical observation is allowed to evaluate a valid primary-timeframe
    # setup without demanding a higher-timeframe confirmation. It still fails
    # closed if there is no direction or no completed primary structure.
    if not structure_checks["directional_plan"] or not structure_checks["completed_primary_candles"]:
        if not structure_checks["directional_plan"]:
            risk_flags.append("No directional trade plan is available for live confirmation.")
        if not structure_checks["completed_primary_candles"]:
            risk_flags.append("Insufficient completed primary-timeframe candles for structure confirmation.")
        return {
            "symbol": symbol, "timeframe": timeframe, "direction": direction,
            "passed": False, "status": "STRUCTURE_REJECTED", "quality_badge": "STRUCTURE CHECK FAILED",
            "structure_checks": structure_checks, "live_checks": {}, "risk_flags": risk_flags,
            "reason": risk_flags[0], "evaluation_mode": "causal_regime_aware_live_confirmation",
            "metrics": {
                "playbook": "NONE",
                "primary_phase": primary_phase,
                "higher_phase": higher_phase,
                "rvol": round(rvol, 2),
                "body_ratio": round(body_ratio, 3),
                "event_rvol": round(_number(observed_event.get("relative_volume", rvol)), 2),
                "event_body_ratio": round(_number(observed_event.get("body_ratio", body_ratio)), 3),
                "sweep": observed_sweep,
                "bos": bos,
                "choch": choch,
                "selected_structure_event": observed_event,
                "structure_story": {
                    "schema_version": "structure_story.v1",
                    "primary_latest_event": primary_story.get("latest_event"),
                    "higher_latest_event": higher_story.get("latest_event"),
                    "setup_state": primary_story.get("current_state", "NO_ACTIVE_EVENT"),
                },
            },
            "structure_story": {
                "schema_version": "structure_story.v1",
                "primary": primary_story,
                "higher": higher_story,
                "setup_state": primary_story.get("current_state", "NO_ACTIVE_EVENT"),
            },
            "scenarios": {
                "institutional": {"passed": False, "status": "UNAVAILABLE", "reason": risk_flags[0]},
                "tactical": {"passed": False, "candidate": False, "status": "UNAVAILABLE", "reason": risk_flags[0]},
            },
        }

    phase_direction = {"MARKUP": "BULLISH", "ACCUMULATION": "BULLISH", "MARKDOWN": "BEARISH", "DISTRIBUTION": "BEARISH"}
    primary_direction = phase_direction.get(primary_phase, "NEUTRAL")
    higher_direction = phase_direction.get(higher_phase, "NEUTRAL")
    close_location = ((_number(latest.close) - _number(latest.low)) / candle_range if direction == "BULLISH"
                      else (_number(latest.high) - _number(latest.close)) / candle_range)
    story_view = evaluate_story_direction(primary_story, direction)
    higher_story_view = evaluate_story_direction(higher_story, direction)
    aligned_structure_event = story_view.get("aligned_structure_event") or {}
    aligned_liquidity_event = story_view.get("aligned_liquidity_event") or {}
    opposing_event = story_view.get("opposing_event") or {}
    structure_opposed = (
        bool(opposing_event)
        and int(opposing_event.get("event_index", -1))
        > int(aligned_structure_event.get("event_index", -1))
    )
    sweep_opposed = (
        bool(opposing_event)
        and int(opposing_event.get("event_index", -1))
        > int(aligned_liquidity_event.get("event_index", -1))
    )
    structure_event_actionable = (
        bool(aligned_structure_event.get("detected"))
        and bool(aligned_structure_event.get("actionable"))
        and not structure_opposed
    )
    breakout = structure_event_actionable
    event_rvol = _number(aligned_structure_event.get("relative_volume"))
    event_body_ratio = _number(aligned_structure_event.get("body_ratio"))
    event_close_location = _number(aligned_structure_event.get("close_location"))
    higher_opposing_event = higher_story_view.get("opposing_event") or {}
    higher_structure_opposed = (
        bool(higher_opposing_event)
        and int(higher_opposing_event.get("event_index", -1))
        > int((higher_story_view.get("aligned_event") or {}).get("event_index", -1))
    )
    trend_playbook = primary_direction == direction and higher_direction == direction
    # A lower-timeframe accumulation/distribution phase can be the internal
    # auction of a higher-timeframe range.  It is not trend alignment, but it
    # is a valid reversal context when it agrees with the proposed side and a
    # completed liquidity sweep proves acceptance.  Treating it as a generic
    # regime mismatch discarded the exact causal setup we want to observe.
    range_playbook = (
        higher_phase == "RANGING"
        and primary_phase in {"RANGING", "ACCUMULATION", "DISTRIBUTION"}
        and primary_direction in {"NEUTRAL", direction}
    )
    sweep = observable_liquidity_sweep(primary_story)
    vwap = build_vwap_context(primary)
    profile = build_volume_profile(primary)
    sweep_aligned = (
        bool(aligned_liquidity_event.get("detected"))
        and bool(aligned_liquidity_event.get("actionable"))
        and not sweep_opposed
    )
    expected_vwap = "ABOVE_ALL" if direction == "BULLISH" else "BELOW_ALL"
    expected_profile = "ABOVE_POC_ACCEPTANCE" if direction == "BULLISH" else "BELOW_POC_ACCEPTANCE"
    vwap_aligned = bool(vwap.get("available")) and vwap.get("price_relation") == expected_vwap
    profile_aligned = bool(profile.get("available")) and profile.get("location") == expected_profile
    institutional_story_playbook = evaluate_story_playbook(
        primary_story=primary_story,
        higher_story=higher_story,
        direction=direction,
        primary_phase=primary_phase,
        higher_phase=higher_phase,
        vwap_context=vwap,
        volume_profile=profile,
    )
    tactical_story_playbook = evaluate_story_playbook(
        primary_story=primary_story,
        higher_story=None,
        direction=direction,
        primary_phase=primary_phase,
        vwap_context=vwap,
        volume_profile=profile,
        require_higher_timeframe=False,
    )

    # This is the primary-timeframe scenario. It is intentionally strict on
    # measured structure and live execution, but it does not turn an HTF
    # mismatch into an invisible setup. It never authorizes a trade signal.
    tactical_structure_checks: dict[str, bool] = {
        "directional_plan": structure_checks["directional_plan"],
        "completed_primary_candles": structure_checks["completed_primary_candles"],
        "structure_not_opposed": True,
    }
    tactical_risk_flags: list[str] = []
    if primary_direction == direction:
        tactical_playbook = "PRIMARY_TREND_CONTINUATION"
        tactical_structure_checks.update({
            "primary_trend_aligned": True,
            "confirmed_completed_breakout": breakout,
            "recent_structure_event_actionable": structure_event_actionable,
            "relative_volume_confirmed": event_rvol >= 1.5,
            "decisive_candle": event_body_ratio >= 0.55 and event_close_location >= 0.60,
            "structure_not_opposed": not structure_opposed,
            "shared_story_playbook_passed": tactical_story_playbook["passed"],
        })
        if not breakout:
            tactical_risk_flags.append(
                f"Primary market story is {story_view.get('state', 'unavailable')}: "
                f"{story_view.get('reason') or 'no fresh actionable structure event is available.'}"
            )
        if event_rvol < 1.5:
            tactical_risk_flags.append(f"Structure-event relative volume {event_rvol:.2f}x is below the 1.50x tactical threshold.")
        if event_body_ratio < 0.55 or event_close_location < 0.60:
            tactical_risk_flags.append("The originating structure-event candle lacks decisive body or close location.")
    elif primary_phase in {"RANGING", "ACCUMULATION", "DISTRIBUTION"} and primary_direction in {"NEUTRAL", direction}:
        tactical_playbook = "PRIMARY_RANGE_SWEEP_REVERSAL"
        tactical_structure_checks.update({
            "primary_range_auction_aligned": True,
            "liquidity_sweep_aligned": sweep_aligned,
            "vwap_acceptance_aligned": vwap_aligned,
            "profile_acceptance_aligned": profile_aligned,
            "structure_not_opposed": not sweep_opposed,
            "shared_story_playbook_passed": tactical_story_playbook["passed"],
        })
        if not sweep_aligned:
            tactical_risk_flags.append(f"No completed {direction.lower()} primary-timeframe liquidity-sweep reversal is present.")
        if not vwap_aligned:
            tactical_risk_flags.append(f"Primary reversal has not accepted {expected_vwap.replace('_', ' ').lower()}.")
        if not profile_aligned:
            tactical_risk_flags.append(f"Primary reversal has not accepted {expected_profile.replace('_', ' ').lower()}.")
    else:
        tactical_playbook = "NONE"
        tactical_structure_checks["approved_primary_playbook"] = False
        tactical_risk_flags.append(f"No approved primary-timeframe playbook: primary={primary_phase}; expected trend continuation or a confirmed range sweep.")
    if not tactical_structure_checks["structure_not_opposed"]:
        tactical_risk_flags.append("A completed-candle structural break opposes the proposed direction.")

    if trend_playbook:
        playbook = "TREND_CONTINUATION"
        structure_checks.update({
            "trend_regime_aligned": True,
            "confirmed_completed_breakout": breakout,
            "recent_structure_event_actionable": structure_event_actionable,
            "relative_volume_confirmed": event_rvol >= 1.5,
            "decisive_candle": event_body_ratio >= 0.55 and event_close_location >= 0.60,
            "structure_not_opposed": not structure_opposed,
            "higher_structure_not_opposed": not higher_structure_opposed,
            "shared_story_playbook_passed": institutional_story_playbook["passed"],
        })
        if not structure_checks["confirmed_completed_breakout"]:
            risk_flags.append(
                f"Completed-candle market story is {story_view.get('state', 'unavailable')}: "
                f"{story_view.get('reason') or 'no fresh actionable structure event is available.'}"
            )
        if not structure_checks["relative_volume_confirmed"]:
            risk_flags.append(f"Structure-event relative volume {event_rvol:.2f}x is below the 1.50x continuation threshold.")
        if not structure_checks["decisive_candle"]:
            risk_flags.append("The originating structure-event candle lacks decisive body/close location.")
        if not structure_checks["higher_structure_not_opposed"]:
            risk_flags.append("A newer higher-timeframe structure event opposes the proposed direction.")
    elif range_playbook:
        # A range is not a failed trend.  It gets its own strict, causal
        # playbook: a completed sweep back inside the range, then acceptance
        # above/below VWAP and the profile POC in the proposed direction.
        # The primary timeframe may be neutral, accumulating, or distributing
        # within that higher-timeframe auction; it must never oppose the side.
        playbook = (
            "RANGE_SWEEP_REVERSAL"
            if primary_phase == "RANGING"
            else "RANGE_AUCTION_SWEEP_REVERSAL"
        )
        structure_checks.update({
            "range_regime_confirmed": True,
            "primary_range_auction_aligned": primary_direction in {"NEUTRAL", direction},
            "liquidity_sweep_aligned": sweep_aligned,
            "vwap_acceptance_aligned": vwap_aligned,
            "profile_acceptance_aligned": profile_aligned,
            "structure_not_opposed": not sweep_opposed,
            "higher_structure_not_opposed": not higher_structure_opposed,
            "shared_story_playbook_passed": institutional_story_playbook["passed"],
        })
        if not sweep_aligned:
            risk_flags.append(
                f"Higher-timeframe range detected but no completed {direction.lower()} liquidity-sweep reversal is present."
            )
        if not vwap_aligned:
            risk_flags.append(f"Range reversal has not accepted {expected_vwap.replace('_', ' ').lower()}.")
        if not profile_aligned:
            risk_flags.append(f"Range reversal has not accepted {expected_profile.replace('_', ' ').lower()}.")
        if not structure_checks["higher_structure_not_opposed"]:
            risk_flags.append("A newer higher-timeframe structure event opposes the proposed direction.")
    else:
        playbook = "NONE"
        structure_checks.update({"approved_regime_playbook": False})
        risk_flags.append(
            f"No approved regime playbook: primary={primary_phase}, higher={higher_phase}; expected aligned trend or a confirmed range sweep."
        )

    if not structure_checks.get("structure_not_opposed", True):
        risk_flags.append("A completed-candle structural break opposes the proposed direction.")

    bids, asks = order_book.get("bids", []) or [], order_book.get("asks", []) or []
    bid_notional = sum(_number(row[0]) * _number(row[1]) for row in bids[:20])
    ask_notional = sum(_number(row[0]) * _number(row[1]) for row in asks[:20])
    best_bid = _number(bids[0][0]) if bids else 0.0
    best_ask = _number(asks[0][0]) if asks else 0.0
    midpoint = (best_bid + best_ask) / 2.0 if best_bid and best_ask else 0.0
    oi_history = derivatives.get("oi_history", {}) or {}
    taker = derivatives.get("taker_buy_sell_volume", {}) or {}
    funding_available = (
        isinstance(funding, dict)
        and "funding_rate" in funding
        and not funding.get("error")
    )
    execution_tape = multi_venue or {}
    tape_actual_flow = execution_tape.get("actual_flow") or {}
    tape_flow_ready = publication_flow_is_qualified(execution_tape)
    taker_flow_ready = tape_flow_ready
    live = {
        "current_price": midpoint if midpoint > 0 else _number(primary[-1].close),
        "data_complete": bool(
            bids and asks and funding_available
            and oi_history.get("available") and taker_flow_ready
        ),
        "depth_imbalance": (bid_notional - ask_notional) / (bid_notional + ask_notional) if bid_notional + ask_notional else None,
        "spread_bps": (best_ask - best_bid) / midpoint * 10_000 if midpoint else None,
        "funding_rate": _number(funding.get("funding_rate")),
        "oi_change_pct": _number(oi_history.get("oi_change_pct")) if oi_history.get("available") else None,
        "price_change_pct": ((_number(primary[-1].close) - _number(primary[-6].close)) / _number(primary[-6].close) * 100.0) if _number(primary[-6].close) else 0.0,
        "taker_buy_sell_ratio": _number(taker.get("ratio", taker.get("buy_sell_ratio"))) if taker.get("available") else None,
        "execution_tape": execution_tape,
        "planned_notional_usd": _number(planned_notional_usd),
        "opposing_depth_notional": ask_notional if direction == "BULLISH" else bid_notional,
    }
    coverage_requirements = {
        "order_book": bool(bids and asks),
        "funding": funding_available,
        "oi_history": bool(oi_history.get("available")),
        "taker_flow": taker_flow_ready,
    }
    publication_coverage = {
        "ready": all(coverage_requirements.values()),
        "inputs_complete": all(coverage_requirements.values()),
        "confirmation_ready": False,
        "requirements": coverage_requirements,
        "missing": [name for name, available in coverage_requirements.items() if not available],
        "label": "PUBLICATION INPUTS COMPLETE" if all(coverage_requirements.values()) else "PUBLICATION INPUTS PARTIAL",
        "taker_flow_source": "live_execution_tape" if tape_flow_ready else "unavailable",
        "supplemental": {
            "execution_tape": {
                "ready": tape_flow_ready,
                "status": (execution_tape.get("actual_flow") or {}).get(
                    "status", "UNAVAILABLE"
                ),
                "bias": (execution_tape.get("actual_flow") or {}).get(
                    "bias", "UNAVAILABLE"
                ),
            },
            "displayed_liquidity_stability": execution_tape.get(
                "displayed_liquidity_stability",
                {"status": "UNAVAILABLE", "publication_veto": False},
            ),
        },
    }
    candidate = {
        "symbol": symbol,
        "direction": direction,
        "score": 75 if all(structure_checks.values()) else 0,
        "risk_flags": risk_flags,
        "require_production_tape": True,
        "market_context": {"actionability": story_view},
    }
    apply_live_confirmation(candidate, live)
    live_checks = candidate["advanced_confirmation"]["checks"]
    passed = all(structure_checks.values()) and candidate["status"] == "LIVE_CONFIRMED_REVIEW"
    publication_coverage.update({
        "confirmation_ready": passed,
        "signal_publication_ready": passed,
        "confirmation_status": (
            "CONFIRMED"
            if passed
            else "AWAITED"
            if publication_coverage["inputs_complete"]
            else "INPUTS_PARTIAL"
        ),
    })
    status = "LIVE_CONFIRMED_REVIEW" if passed else ("STRUCTURE_REJECTED" if not all(structure_checks.values()) else "LIVE_CONFIRMATION_REJECTED")
    tactical_candidate = {
        "symbol": symbol, "direction": direction,
        "score": 75 if all(tactical_structure_checks.values()) else 0,
        "risk_flags": tactical_risk_flags,
        "require_production_tape": True,
        "market_context": {"actionability": story_view},
    }
    apply_live_confirmation(tactical_candidate, live)
    tactical_live_checks = tactical_candidate["advanced_confirmation"]["checks"]
    tactical_passed = all(tactical_structure_checks.values()) and tactical_candidate["status"] == "LIVE_CONFIRMED_REVIEW"
    # Keep a viable primary-timeframe setup visible while it is waiting for
    # its own measured proof. This never authorizes a trade: only the
    # institutional scenario can publish a signal.
    tactical_watch_candidate = (
        tactical_playbook != "NONE"
        and tactical_structure_checks.get("directional_plan", False)
        and tactical_structure_checks.get("completed_primary_candles", False)
        and tactical_structure_checks.get("structure_not_opposed", False)
    )
    tactical_reason = tactical_candidate["risk_flags"][0] if tactical_candidate["risk_flags"] else "Primary-timeframe structure and execution evidence are aligned."
    result = {
        "symbol": symbol, "timeframe": timeframe, "direction": direction, "passed": passed,
        "status": status, "quality_badge": "LIVE CHECK PASSED" if passed else "LIVE CHECK FAILED",
        "structure_checks": structure_checks, "live_checks": live_checks,
        "risk_flags": candidate["risk_flags"], "reason": candidate["risk_flags"][0] if candidate["risk_flags"] else "All shared Radar checks passed.",
        "metrics": {
            "playbook": playbook,
            "primary_phase": primary_phase,
            "higher_phase": higher_phase,
            "primary_direction": primary_direction,
            "higher_direction": higher_direction,
            "rvol": round(rvol, 2),
            "body_ratio": round(body_ratio, 3),
            "event_rvol": round(event_rvol, 2),
            "event_body_ratio": round(event_body_ratio, 3),
            "event_close_location": round(event_close_location, 3),
            "sweep": sweep,
            "vwap": vwap,
            "volume_profile": profile,
            "bos": bos,
            "choch": choch,
            "selected_structure_event": aligned_structure_event,
            "structure_confirmation": institutional_story_playbook,
            "tactical_structure_confirmation": tactical_story_playbook,
            "structure_story": {
                "schema_version": "structure_story.v1",
                "directional_view": story_view,
                "higher_directional_view": higher_story_view,
                "primary_latest_event": primary_story.get("latest_event"),
                "higher_latest_event": higher_story.get("latest_event"),
                "setup_state": story_view.get("state", "NO_ACTIVE_EVENT"),
            },
        },
        "structure_story": {
            "schema_version": "structure_story.v1",
            "primary": primary_story,
            "higher": higher_story,
            "directional_view": story_view,
            "higher_directional_view": higher_story_view,
            "alignment": {
                "phase_aligned": higher_direction == direction,
                "higher_structure_opposed": higher_structure_opposed,
                "direction": direction,
            },
            "setup_state": story_view.get("state", "NO_ACTIVE_EVENT"),
        },
        "live_evidence": candidate["advanced_confirmation"],
        "publication_coverage": publication_coverage,
        "evaluation_mode": "causal_regime_aware_live_confirmation",
    }
    result["scenarios"] = {
        "institutional": {
            "passed": passed,
            "status": status,
            "label": "INSTITUTIONAL CONFIRMATION",
            "reason": result["reason"],
            "structure_checks": structure_checks,
            "live_checks": live_checks,
            "playbook": playbook,
            "higher_timeframe_aligned": higher_direction == direction,
            "higher_timeframe_state": higher_phase,
            "market_story_state": story_view.get("state", "NO_ACTIVE_EVENT"),
            "market_story_actionable": bool(story_view.get("actionable")),
            "market_story_reason": story_view.get("reason"),
            "selected_event": institutional_story_playbook.get("selected_event"),
            "structure_confirmation": institutional_story_playbook,
        },
        "tactical": {
            "passed": tactical_passed,
            "candidate": tactical_watch_candidate,
            "status": (
                "TACTICAL_CONFIRMED_WATCH"
                if tactical_passed
                else "TACTICAL_EVIDENCE_WATCH"
                if tactical_watch_candidate
                else "TACTICAL_REJECTED"
            ),
            "label": "TACTICAL CONFIRMATION",
            "reason": tactical_reason,
            "structure_checks": tactical_structure_checks,
            "live_checks": tactical_live_checks,
            "playbook": tactical_playbook,
            "higher_timeframe_aligned": higher_direction == direction,
            "higher_timeframe_state": higher_phase,
            "market_story_state": story_view.get("state", "NO_ACTIVE_EVENT"),
            "market_story_actionable": bool(story_view.get("actionable")),
            "market_story_reason": story_view.get("reason"),
            "selected_event": tactical_story_playbook.get("selected_event"),
            "structure_confirmation": tactical_story_playbook,
        },
    }
    return result
