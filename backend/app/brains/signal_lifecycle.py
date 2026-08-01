from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from app.data_sources.binance_public import interval_seconds


OPEN_SIGNAL_STATUSES = {"PENDING_ENTRY", "ACTIVE", "TP1_SECURED", "TP2_SECURED"}
TERMINAL_SIGNAL_STATUSES = {"COMPLETED", "STOPPED_OUT", "INVALIDATED", "CANCELLED", "EXPIRED"}
MAX_LIVE_EVENT_CHASE_ATR = 2.5


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _utc(value: Any, fallback: datetime) -> datetime:
    if not isinstance(value, datetime):
        return fallback
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _side(decision: str) -> str | None:
    if decision.startswith("BUY"):
        return "LONG"
    if decision.startswith("SELL"):
        return "SHORT"
    return None


def _event(kind: str, title: str, detail: str, now: datetime) -> dict[str, str]:
    return {"at": now.astimezone(timezone.utc).isoformat(), "kind": kind, "title": title, "detail": detail}


def _aligned(value: Any, side: str, kind: str) -> bool:
    expected = {
        ("LONG", "trend"): "bullish", ("SHORT", "trend"): "bearish",
        ("LONG", "momentum"): "bullish", ("SHORT", "momentum"): "bearish",
        ("LONG", "book"): "buyers", ("SHORT", "book"): "sellers",
    }
    return str(value or "").lower() == expected[(side, kind)]


def _story_event(story: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(story, Mapping):
        return {}
    for key in ("selected_event", "aligned_event", "latest_event"):
        event = story.get(key)
        if isinstance(event, Mapping) and event.get("event_id"):
            return event
    return {}


def _event_direction_for_side(side: str) -> str:
    return "BULLISH" if side == "LONG" else "BEARISH" if side == "SHORT" else ""


def market_story_matches_signal(
    signal: Mapping[str, Any],
    market_story: Mapping[str, Any] | None,
) -> bool:
    """Return true only when a live story belongs to the signal's origin event.

    A new analysis can select a newer or opposite event while an older signal
    is still open.  Its lifecycle must never be refreshed, cancelled, or
    invalidated by that unrelated setup.
    """
    side = str(signal.get("side", "")).upper()
    expected_direction = _event_direction_for_side(side)
    context = signal.get("context") or {}
    if not isinstance(context, Mapping):
        return False
    stored_story = context.get("market_story") or {}
    stored_event = _story_event(stored_story)
    incoming_event = _story_event(market_story)
    origin_id = str(context.get("structure_event_id") or stored_event.get("event_id") or "")
    origin_direction = str(
        context.get("structure_event_direction")
        or stored_event.get("direction")
        or ""
    ).upper()
    incoming_id = str(incoming_event.get("event_id") or "")
    incoming_direction = str(incoming_event.get("direction") or "").upper()
    return bool(
        origin_id
        and incoming_id == origin_id
        and origin_direction
        and incoming_direction == origin_direction
        and incoming_direction == expected_direction
    )


def _entry_flow_confirmation(side: str, market_context: Mapping[str, Any] | None) -> dict[str, Any]:
    """Qualify a live entry from measured aggression plus matching price response."""
    context = market_context if isinstance(market_context, Mapping) else {}
    tape = context.get("execution_tape") or {}
    actual_flow = tape.get("actual_flow") or {} if isinstance(tape, Mapping) else {}
    available = bool(actual_flow.get("available"))
    bias = str(actual_flow.get("bias", "UNAVAILABLE")).upper()
    status = str(actual_flow.get("status", "UNAVAILABLE")).upper()
    expected_bias = "BULLISH" if side == "LONG" else "BEARISH"
    expected_status = "BUYING_CONFIRMED" if side == "LONG" else "SELLING_CONFIRMED"
    passed = available and bias == expected_bias and status == expected_status
    opposed = available and bias not in {expected_bias, "NEUTRAL", "UNAVAILABLE"}
    if passed:
        reason = (
            f"Live {expected_bias.lower()} aggression is producing matching price acceptance."
        )
    elif not available:
        reason = "Waiting for a qualified Binance/Bybit execution-tape observation."
    elif opposed:
        reason = (
            f"Live execution flow is {bias.lower()} ({status.lower().replace('_', ' ')}), "
            f"opposing the planned {side.lower()} entry."
        )
    else:
        reason = (
            f"Live flow is {status.lower().replace('_', ' ')}; "
            f"{expected_status.lower().replace('_', ' ')} is required."
        )
    return {
        "available": available,
        "passed": passed,
        "opposed": opposed,
        "bias": bias,
        "status": status,
        "price_response": actual_flow.get("price_response", "UNAVAILABLE"),
        "active_aggressor": actual_flow.get("active_aggressor", "UNAVAILABLE"),
        "qualified_source_count": int(_number(actual_flow.get("qualified_source_count"))),
        "captured_at": tape.get("captured_at") if isinstance(tape, Mapping) else None,
        "reason": reason,
    }


def _completed_retest_confirmed(story: Mapping[str, Any] | None) -> bool:
    if not isinstance(story, Mapping) or story.get("actionable") is not True:
        return False
    event = _story_event(story)
    return str(story.get("state") or event.get("state") or "").upper() == "RETESTING"


def _live_event_distance_atr(
    *,
    side: str,
    current_price: float,
    story: Mapping[str, Any],
    fallback_atr: float = 0.0,
) -> float | None:
    event = _story_event(story)
    if str(event.get("direction") or "").upper() != _event_direction_for_side(side):
        return None
    level = _number(event.get("break_level"))
    event_atr = _number(event.get("atr_at_event"))
    atr = event_atr if event_atr > 0 else _number(fallback_atr)
    if level <= 0 or atr <= 0 or current_price <= 0:
        return None
    distance = current_price - level if side == "LONG" else level - current_price
    return distance / atr


def evaluate_signal_approval(
    *, decision: Mapping[str, Any], trade_setup: Mapping[str, Any] | None,
    risk_idea: Mapping[str, Any] | None, trend: Mapping[str, Any],
    momentum: Mapping[str, Any], order_book: Mapping[str, Any],
    data_freshness: Mapping[str, Any], liquidity: Mapping[str, Any],
    ai_result: Mapping[str, Any] | None, current_price: float,
    council_approval: Mapping[str, Any] | None = None,
    require_council_approval: bool = False,
) -> dict[str, Any]:
    """Apply publication safeguards to a council-approved trade plan.

    ``council_approval`` is the canonical council release decision produced by
    :func:`evaluate_ai_driven_approval`.  The lifecycle layer must not apply a
    second, incompatible opinion gate: doing so used to reject every
    single-call council setup because its synthetic Risk Manager report has no
    ``approved`` field.  It still verifies live market-data, plan integrity,
    and price-chase protections immediately before persistence.
    """
    blockers: list[str] = []
    decision_name = str(decision.get("decision", "HOLD"))
    side = _side(decision_name)
    confidence = _number(decision.get("confidence"))

    if council_approval is not None:
        if not council_approval.get("approved", False):
            blockers.extend(str(item) for item in council_approval.get("blockers", []) if item)
            if not blockers:
                blockers.append(str(council_approval.get("summary") or "AI Council did not approve this setup."))
        council_side = council_approval.get("side")
        if council_side and council_side != side:
            blockers.append("AI Council approval direction does not match the trade plan.")
    elif require_council_approval:
        blockers.append("Canonical AI Council approval is required before publication.")

    if side is None:
        blockers.append("No directional AI Council decision.")
    # Legacy callers without a council release decision retain the original
    # deterministic approval contract.
    if council_approval is None and not require_council_approval:
        if confidence < 72:
            blockers.append(f"Confidence {confidence:.0f}% is below the 72% release threshold.")
        if decision.get("trade_grade") not in {"A+", "A", "B"}:
            blockers.append("Trade grade must be B or higher.")
    if not data_freshness.get("passed"):
        blockers.append("Market data is stale.")
    if not liquidity.get("passed"):
        blockers.append("Liquidity gate did not pass.")
    if not risk_idea or not trade_setup or _number(risk_idea.get("risk_reward")) < 1.5:
        blockers.append("A minimum 1.5R entry, invalidation, and target plan is required.")
    if trade_setup:
        story_view = trade_setup.get("market_story") or {}
        if story_view.get("actionable") is False:
            blockers.append(
                f"Completed-candle market story is {story_view.get('state', 'not actionable')}: "
                f"{story_view.get('reason') or 'the structural entry is unavailable.'}"
            )
        reward_space = trade_setup.get("remaining_reward") or {}
        if reward_space.get("adequate") is False:
            blockers.append(
                reward_space.get("reason")
                or "Insufficient reward remains before the next measured liquidity objective."
            )
        if trade_setup.get("execution_permitted") is False:
            blockers.append("The deterministic trade plan is research-only and cannot be published.")
        if side:
            event_distance_atr = _live_event_distance_atr(
                side=side,
                current_price=current_price,
                story=story_view,
                fallback_atr=_number((trade_setup.get("stop") or {}).get("atr")),
            )
            if event_distance_atr is not None and event_distance_atr > MAX_LIVE_EVENT_CHASE_ATR:
                blockers.append(
                    f"Live quote is {event_distance_atr:.2f} ATR beyond the originating "
                    "event level; do not chase."
                )

    confirmations = 0
    if side and council_approval is None and not require_council_approval:
        confirmations = sum((
            _aligned(trend.get("status"), side, "trend"),
            _aligned(momentum.get("bias"), side, "momentum"),
            _aligned(order_book.get("pressure"), side, "book"),
        ))
        if confirmations < 2:
            blockers.append("Trend, momentum, and order flow are not sufficiently aligned.")

    if not ai_result:
        blockers.append("AI risk review has not completed.")
    elif ai_result.get("error"):
        blockers.append("AI risk review failed, so the bot will not release a signal.")
    elif council_approval is None and not require_council_approval:
        reports = ai_result.get("agent_reports") or {}
        risk_review = reports.get("risk_manager") or {}
        pre_mortem = reports.get("pre_mortem_analyst") or {}
        if _side(str(ai_result.get("decision", "HOLD"))) != side:
            blockers.append("AI CIO decision does not confirm the deterministic direction.")
        if risk_review.get("approved") is not True:
            blockers.append("AI risk manager did not approve the setup.")
        if _number(pre_mortem.get("severity_score"), 10) > 5:
            blockers.append("AI pre-mortem severity is too high.")
        if (ai_result.get("macro_blockout") or {}).get("active"):
            blockers.append("High-impact macro blockout is active.")

    if side and risk_idea and trade_setup:
        entry = trade_setup.get("entry") or {}
        zone_low = _number(entry.get("zone_low"), _number(risk_idea.get("entry_zone_low")))
        zone_high = _number(entry.get("zone_high"), _number(risk_idea.get("entry_zone_high")))
        stop = _number((trade_setup.get("stop") or {}).get("selected"))
        risk = abs(_number(entry.get("reference")) - stop)
        if side == "LONG" and current_price > zone_high + risk * 0.5:
            blockers.append("Long entry is too far above the planned zone; do not chase.")
        if side == "SHORT" and current_price < zone_low - risk * 0.5:
            blockers.append("Short entry is too far below the planned zone; do not chase.")

    return {
        "approved": not blockers, "side": side, "confidence": round(confidence, 2),
        "confirmations": confirmations, "blockers": blockers,
        "summary": "All decision gates passed. Signal can be published." if not blockers else blockers[0],
    }


def build_signal_seed(
    *, symbol: str, timeframe: str, decision: Mapping[str, Any], trade_setup: Mapping[str, Any],
    approval: Mapping[str, Any], current_price: float, context: Mapping[str, Any],
    ai_review: Mapping[str, Any], now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    entry = trade_setup.get("entry") or {}
    stop = trade_setup.get("stop") or {}
    targets = trade_setup.get("targets") or {}
    position = trade_setup.get("position") or {}
    entry_low = _number(entry.get("zone_low"), _number(entry.get("reference")))
    entry_high = _number(entry.get("zone_high"), _number(entry.get("reference")))
    entry_low, entry_high = min(entry_low, entry_high), max(entry_low, entry_high)
    entry_reference = _number(entry.get("reference"))
    stop_price = _number(stop.get("selected"))
    price_in_zone = entry_low <= current_price <= entry_high
    interval = interval_seconds(timeframe)
    story_view = trade_setup.get("market_story") or {}
    selected_event = story_view.get("selected_event") or {}
    event_age_bars = max(0, int(_number(selected_event.get("age_bars"))))
    remaining_entry_bars = max(1, 6 - event_age_bars)
    remaining_lifecycle_bars = max(remaining_entry_bars, 32 - event_age_bars)
    publication_flow = _entry_flow_confirmation(str(approval["side"]), context)
    confirmed_retest_at_publication = (
        price_in_zone
        and _completed_retest_confirmed(story_view)
        and publication_flow["passed"]
    )
    # A completed retest plus live accepting flow is already the confirmation;
    # requiring another leave-and-retest would miss the intended entry. Other
    # in-zone publications remain pending and must produce a fresh live retest,
    # reclaim, and aligned execution tape before activation.
    status = "ACTIVE" if confirmed_retest_at_publication else "PENDING_ENTRY"
    if confirmed_retest_at_publication:
        initial_detail = (
            f"Completed retest held and live {publication_flow['status'].lower().replace('_', ' ')} "
            f"confirmed entry at {current_price:.4f}."
        )
    elif price_in_zone:
        initial_detail = (
            f"Price {current_price:.4f} is inside the approved entry zone. "
            "Waiting for a fresh retest, favourable reclaim, and aligned live flow."
        )
    else:
        initial_detail = f"Waiting for price to reach {entry_low:.4f} - {entry_high:.4f}."
    entry_confirmation = {
        **publication_flow,
        "state": (
            "CONFIRMED_AT_PUBLICATION"
            if confirmed_retest_at_publication
            else "WAITING_FOR_FRESH_RETEST"
            if price_in_zone
            else "WAITING_FOR_ZONE"
        ),
        "completed_retest_confirmed": confirmed_retest_at_publication,
        "reason": initial_detail,
    }
    return {
        "symbol": symbol, "timeframe": timeframe, "side": approval["side"], "status": status,
        "decision": decision["decision"], "confidence": _number(decision.get("confidence")),
        "entry_low": entry_low, "entry_high": entry_high, "entry_reference": entry_reference,
        "entry_price": current_price if confirmed_retest_at_publication else None,
        "stop_initial": stop_price, "stop_current": stop_price,
        "target_1": _number(targets.get("tp1_1r")), "target_2": _number(targets.get("tp2_2r")),
        "target_3": _number(targets.get("tp3_3r")), "target_runner": _number(targets.get("runner_5r")),
        "target_stage": 0, "risk_per_unit": abs(entry_reference - stop_price),
        "risk_amount_usd": _number(position.get("risk_amount_usd")),
        "notional_usd": _number(position.get("notional_usd")),
        "recommended_leverage": int(_number((trade_setup.get("leverage") or {}).get("recommended"), 1)),
        "current_price": current_price, "entry_timeout_at": now + timedelta(seconds=interval * remaining_entry_bars),
        "expires_at": now + timedelta(seconds=interval * remaining_lifecycle_bars), "published_at": now,
        "last_evaluated_at": now, "events": [
            _event(
                "entry_confirmed" if confirmed_retest_at_publication else "signal_published",
                "Entry confirmed" if confirmed_retest_at_publication else "Signal published",
                initial_detail,
                now,
            )
        ],
        "context": {
            **dict(context),
            "market_story": dict(story_view),
            "structure_event_id": selected_event.get("event_id"),
            "structure_event_direction": selected_event.get("direction"),
            "structure_event_age_bars_at_publication": event_age_bars,
            "requires_fresh_entry_retest": not confirmed_retest_at_publication,
            "left_entry_zone_after_publication": not price_in_zone,
            "entry_zone_armed": False,
            "entry_confirmation": entry_confirmation,
        },
        "ai_review": dict(ai_review),
    }


def advance_signal(
    signal: Mapping[str, Any], *, current_price: float,
    market_context: Mapping[str, Any] | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    """Advance one immutable signal plan. Stops and targets are never widened."""
    now = now or datetime.now(timezone.utc)
    state = dict(signal)
    events = list(state.get("events") or [])
    state.update({"current_price": current_price, "last_evaluated_at": now, "events": events})
    status = str(state.get("status", "PENDING_ENTRY"))
    if status in TERMINAL_SIGNAL_STATUSES:
        return state
    side = str(state["side"])
    entry = _number(state.get("entry_price"), _number(state.get("entry_reference")))
    stop = _number(state.get("stop_current"), _number(state.get("stop_initial")))

    def close(status_name: str, kind: str, title: str, detail: str) -> dict[str, Any]:
        state.update({"status": status_name, "exit_price": current_price, "exit_reason": detail, "closed_at": now})
        events.append(_event(kind, title, detail, now))
        return state

    # Compatibility for signals created before TP3 became the final realised
    # target. They already reached TP3, so preserve that fact as a win rather
    # than leaving them in a no-longer-monitored intermediate state.
    if status == "TP3_SECURED":
        state["target_stage"] = max(3, int(state.get("target_stage", 0)))
        return close(
            "COMPLETED", "tp3_finalized", "TP3 finalised — successful trade",
            "TP3 had already been secured. Trade finalised as a successful outcome.",
        )

    context = market_context or {}
    incoming_story = context.get("market_story") or {}
    story_view = incoming_story if market_story_matches_signal(state, incoming_story) else {}
    matched_event = _story_event(story_view)
    story_state = str(story_view.get("state") or matched_event.get("state") or "")
    story_reason = story_view.get("reason") or matched_event.get("reason")
    if story_view:
        live_context_state = dict(state.get("context") or {})
        live_context_state["market_story"] = dict(story_view)
        live_context_state["last_completed_story_update_at"] = now.astimezone(timezone.utc).isoformat()
        state["context"] = live_context_state
    if story_state == "INVALIDATED":
        return close(
            "INVALIDATED",
            "structure_invalidated",
            "Exit now",
            story_reason or "The completed-candle structure event was invalidated.",
        )
    if status == "PENDING_ENTRY" and story_state in {
        "EXPIRED",
        "MISSED",
        "EXTENDED_DO_NOT_CHASE",
        "LATE_STRUCTURE_DO_NOT_CHASE",
    }:
        return close(
            "EXPIRED" if story_state == "EXPIRED" else "CANCELLED",
            "structure_entry_unavailable",
            "Cancel setup",
            story_reason or "The original completed-candle entry opportunity is no longer available.",
        )

    if status == "PENDING_ENTRY":
        breached = current_price <= stop if side == "LONG" else current_price >= stop
        if breached:
            return close("INVALIDATED", "entry_invalidated", "Signal invalidated", "Price reached the invalidation stop before entry.")
        if now >= _utc(state.get("entry_timeout_at"), now):
            return close("EXPIRED", "entry_timeout", "Entry window expired", "Price did not reach the approved entry zone in time.")
        zone_low = _number(state.get("entry_low"))
        zone_high = _number(state.get("entry_high"))
        in_entry_zone = zone_low <= current_price <= zone_high
        context_state = dict(state.get("context") or {})
        armed = bool(context_state.get("entry_zone_armed"))
        completed_retest_reference = _number(matched_event.get("current_close"))
        completed_retest_can_arm = bool(
            not armed
            and context_state.get("left_entry_zone_after_publication")
            and _completed_retest_confirmed(story_view)
            and zone_low <= completed_retest_reference <= zone_high
        )
        if completed_retest_can_arm:
            armed = True
            context_state.update({
                "entry_zone_armed": True,
                "entry_zone_armed_at": now.astimezone(timezone.utc).isoformat(),
                "entry_zone_arm_price": completed_retest_reference,
                "entry_retest_extreme": completed_retest_reference,
                "entry_confirmation": {
                    "state": "COMPLETED_RETEST_ARMED",
                    "passed": False,
                    "reclaim_confirmed": False,
                    "reason": (
                        f"Completed candle held the originating event inside the entry zone at "
                        f"{completed_retest_reference:.4f}; waiting for live reclaim and aggressor flow."
                    ),
                },
            })
            state["context"] = context_state
            events.append(_event(
                "completed_retest_armed",
                "Completed retest armed",
                context_state["entry_confirmation"]["reason"],
                now,
            ))
        if not in_entry_zone and not armed:
            context_state["left_entry_zone_after_publication"] = True
            context_state["entry_confirmation"] = {
                "state": "WAITING_FOR_ZONE",
                "passed": False,
                "reason": f"Waiting for price to reach {zone_low:.4f} - {zone_high:.4f}.",
            }
            state["context"] = context_state
            return state
        if context_state.get("requires_fresh_entry_retest") and not context_state.get("left_entry_zone_after_publication"):
            context_state["entry_confirmation"] = {
                "state": "WAITING_FOR_FRESH_RETEST",
                "passed": False,
                "reason": "The setup was published inside its entry zone; price must leave before a fresh retest can arm it.",
            }
            state["context"] = context_state
            return state

        flow_confirmation = _entry_flow_confirmation(side, context)
        risk_per_unit = max(
            _number(state.get("risk_per_unit")),
            abs(_number(state.get("entry_reference")) - stop),
            max(abs(_number(state.get("entry_reference"))), 1e-9) * 0.0005,
        )
        reclaim_threshold = max(
            risk_per_unit * 0.05,
            max(abs(_number(state.get("entry_reference"))), 1e-9) * 0.0002,
        )
        confirmation_buffer = risk_per_unit * 0.25

        if in_entry_zone and not armed:
            context_state.update({
                "entry_zone_armed": True,
                "entry_zone_armed_at": now.astimezone(timezone.utc).isoformat(),
                "entry_zone_arm_price": current_price,
                "entry_retest_extreme": current_price,
                "entry_confirmation": {
                    **flow_confirmation,
                    "state": "ARMED_AWAITING_RECLAIM_AND_FLOW",
                    "passed": False,
                    "reclaim_confirmed": False,
                    "reason": (
                        f"Entry zone touched at {current_price:.4f}. Waiting for a favourable "
                        "price reclaim with aligned live aggressor flow."
                    ),
                },
            })
            state["context"] = context_state
            events.append(_event(
                "entry_zone_armed",
                "Entry zone armed",
                context_state["entry_confirmation"]["reason"],
                now,
            ))
            return state

        extreme = _number(context_state.get("entry_retest_extreme"), current_price)
        if side == "LONG":
            extreme = min(extreme, current_price)
            reclaim_confirmed = current_price >= extreme + reclaim_threshold
            location_acceptable = zone_low <= current_price <= zone_high + confirmation_buffer
            moved_away = current_price > zone_high + confirmation_buffer
        else:
            extreme = max(extreme, current_price)
            reclaim_confirmed = current_price <= extreme - reclaim_threshold
            location_acceptable = zone_low - confirmation_buffer <= current_price <= zone_high
            moved_away = current_price < zone_low - confirmation_buffer
        context_state["entry_retest_extreme"] = extreme

        if moved_away:
            context_state.update({
                "entry_zone_armed": False,
                "entry_confirmation": {
                    **flow_confirmation,
                    "state": "RECLAIM_MOVED_AWAY",
                    "passed": False,
                    "reclaim_confirmed": reclaim_confirmed,
                    "reason": "Price left the live confirmation buffer before entry proof completed; waiting for another retest.",
                },
            })
            state["context"] = context_state
            events.append(_event(
                "entry_confirmation_missed",
                "Entry confirmation moved away",
                context_state["entry_confirmation"]["reason"],
                now,
            ))
            return state

        entry_ready = reclaim_confirmed and location_acceptable and flow_confirmation["passed"]
        context_state["entry_confirmation"] = {
            **flow_confirmation,
            "state": "CONFIRMED" if entry_ready else "ARMED_AWAITING_RECLAIM_AND_FLOW",
            "passed": entry_ready,
            "reclaim_confirmed": reclaim_confirmed,
            "reclaim_threshold": reclaim_threshold,
            "retest_extreme": extreme,
            "reason": (
                f"Favourable reclaim and {flow_confirmation['status'].lower().replace('_', ' ')} confirmed entry."
                if entry_ready
                else flow_confirmation["reason"]
                if reclaim_confirmed and location_acceptable
                else "A favourable bounce began, but price has not reclaimed the approved entry zone."
                if reclaim_confirmed
                else "Entry zone is armed; waiting for a favourable live-price reclaim."
            ),
        }
        state["context"] = context_state
        if entry_ready:
            state.update({"status": "ACTIVE", "entry_price": current_price})
            entry = current_price
            events.append(_event(
                "entry_confirmed",
                "Entry confirmed",
                f"Fresh retest, favourable reclaim, and live execution flow confirmed entry at {current_price:.4f}.",
                now,
            ))
        else:
            return state

    if now >= _utc(state.get("expires_at"), now):
        return close("EXPIRED", "signal_expired", "Signal expired", "Maximum signal lifetime reached; close or ignore the remaining position.")
    stop_hit = current_price <= stop if side == "LONG" else current_price >= stop
    if stop_hit:
        if int(state.get("target_stage", 0)) > 0:
            return close("COMPLETED", "protected_exit", "Protected exit", "Trailing protection closed the remaining position after profit was secured.")
        return close("STOPPED_OUT", "stop_hit", "Exit now", "Initial invalidation stop was reached.")

    targets = (
        (1, "target_1", "TP1_SECURED", "TP1 reached", "Move stop to entry and protect the remaining position."),
        (2, "target_2", "TP2_SECURED", "TP2 reached", "Move stop to TP1 and let the remaining position work."),
        # TP3 is the system's realised-profit finish line.  A displayed
        # runner is informational only; leaving a successful trade open for
        # it made both the monitor and outcome history misclassify it as live.
        (3, "target_3", "COMPLETED", "TP3 reached — successful trade", "TP3 profit target reached. Trade is recorded as successful and closed."),
    )
    stage = int(state.get("target_stage", 0))
    for next_stage, field, next_status, title, detail in targets:
        target = _number(state.get(field))
        hit = current_price >= target if side == "LONG" else current_price <= target
        if next_stage > stage and target > 0 and hit:
            state.update({"target_stage": next_stage, "status": next_status})
            if next_stage == 1:
                state["stop_current"] = entry
            elif next_stage == 2:
                state["stop_current"] = _number(state.get("target_1"))
            elif next_stage == 3:
                state.update({"exit_price": current_price, "exit_reason": detail, "closed_at": now})
            events.append(_event(f"tp{next_stage}_hit", title, detail, now))
            return state

    # Do not invalidate an approved plan because RSI-derived momentum or a
    # transient displayed-book snapshot flipped. Structural invalidation is
    # handled above from the originating completed-candle event. Stops and
    # targets remain authoritative between completed causal refreshes.
    return state


def signal_action(status: str) -> str:
    return {
        "PENDING_ENTRY": "WAIT_FOR_ENTRY", "ACTIVE": "HOLD_POSITION", "TP1_SECURED": "PROTECT_PROFIT",
        "TP2_SECURED": "PROTECT_PROFIT", "TP3_SECURED": "TAKE_PROFIT_COMPLETE", "COMPLETED": "TAKE_PROFIT_COMPLETE",
        "STOPPED_OUT": "EXIT_TRADE", "INVALIDATED": "EXIT_TRADE", "CANCELLED": "SIGNAL_CANCELLED", "EXPIRED": "SIGNAL_EXPIRED",
    }.get(status, "SCANNING")


def build_signal_view(signal: Mapping[str, Any] | None, approval: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if not signal:
        blockers = list((approval or {}).get("blockers") or [])
        return {
            "id": None, "status": "SCANNING", "action": "SCANNING", "headline": "No tradable signal",
            "reason": blockers[0] if blockers else "Waiting for a fully qualified setup.",
            "approval": dict(approval or {"approved": False, "blockers": blockers}), "events": [],
        }
    side = str(signal.get("side", "LONG"))
    current = _number(signal.get("current_price"))
    entry = _number(signal.get("entry_price"), _number(signal.get("entry_reference")))
    tp1 = _number(signal.get("target_1"))
    status = str(signal["status"])
    stage = int(signal.get("target_stage", 0))
    tp2 = _number(signal.get("target_2"))
    tp3 = _number(signal.get("target_3"))
    runner = _number(signal.get("target_runner"))
    progress_label = "Waiting for confirmed entry"
    progress_start, progress_target = entry, tp1
    if status in {"ACTIVE", "TP1_SECURED", "TP2_SECURED", "TP3_SECURED", "COMPLETED"}:
        if status == "COMPLETED":
            progress_label, progress_start, progress_target = "TP3 complete — successful trade", tp3, tp3
        elif stage <= 0:
            progress_label, progress_start, progress_target = "Progress to TP1", entry, tp1
        elif stage == 1:
            progress_label, progress_start, progress_target = "Progress to TP2", tp1, tp2
        elif stage == 2:
            progress_label, progress_start, progress_target = "Progress to TP3", tp2, tp3
        elif stage == 3:
            progress_label, progress_start, progress_target = "Progress to Runner", tp3, runner
        else:
            progress_label, progress_start, progress_target = "All targets complete", runner, runner
    elif status in TERMINAL_SIGNAL_STATUSES:
        progress_label = "Signal closed"

    direction = 1.0 if side == "LONG" else -1.0
    total_distance = (progress_target - progress_start) * direction
    travelled_distance = (current - progress_start) * direction
    progress = travelled_distance / total_distance * 100 if total_distance > 0 else (100.0 if status == "COMPLETED" or stage >= 4 else 0.0)
    if status == "PENDING_ENTRY" or travelled_distance < 0:
        progress = 0.0
    journey_progress = 0.0
    if status in {"ACTIVE", "TP1_SECURED", "TP2_SECURED", "TP3_SECURED", "COMPLETED"}:
        journey_progress = 100.0 if status == "COMPLETED" or stage >= 4 else min(100.0, (stage + progress / 100.0) * 25.0)
    entry_confirmation = ((signal.get("context") or {}).get("entry_confirmation") or {})
    return {
        "id": signal.get("id"), "symbol": signal.get("symbol"), "timeframe": signal.get("timeframe"), "side": side,
        "status": status, "action": signal_action(status), "headline": signal_action(status).replace("_", " "),
        "reason": (
            signal.get("exit_reason")
            or (((signal.get("context") or {}).get("entry_confirmation") or {}).get("reason") if status == "PENDING_ENTRY" else None)
            or "Monitoring the published signal against its fixed plan."
        ),
        "exit_now": status in {"STOPPED_OUT", "INVALIDATED"},
        "confidence": round(_number(signal.get("confidence")), 2), "current_price": current,
        "entry": {
            "low": _number(signal.get("entry_low")),
            "high": _number(signal.get("entry_high")),
            "reference": _number(signal.get("entry_reference")),
            "price": signal.get("entry_price"),
            "confirmation": entry_confirmation,
        },
        "entry_confirmation": entry_confirmation,
        "stop": {"initial": _number(signal.get("stop_initial")), "current": _number(signal.get("stop_current"))},
        "targets": {"tp1": tp1, "tp2": tp2, "tp3": tp3, "runner": runner, "stage": stage},
        "risk": {"per_unit": _number(signal.get("risk_per_unit")), "amount_usd": _number(signal.get("risk_amount_usd")), "notional_usd": _number(signal.get("notional_usd")), "recommended_leverage": int(_number(signal.get("recommended_leverage"), 1))},
        "progress_pct": round(max(0, min(progress, 100)), 1),
        # Legacy field retained for existing clients; it now contains progress
        # to the current target rather than staying full after TP1.
        "progress_to_tp1_pct": round(max(0, min(progress, 100)), 1),
        "progress_label": progress_label,
        "progress_target": progress_target,
        "journey_progress_pct": round(journey_progress, 1),
        "entry_timeout_at": signal.get("entry_timeout_at"),
        "expires_at": signal.get("expires_at"), "last_evaluated_at": signal.get("last_evaluated_at"), "exit_price": signal.get("exit_price"),
        "approval": {"approved": True, "blockers": []}, "events": list(signal.get("events") or [])[-8:],
        "market_story": (signal.get("context") or {}).get("market_story", {}),
        "structure_event_id": (signal.get("context") or {}).get("structure_event_id"),
    }


# ── Safety Mechanisms ────────────────────────────────────────────────────────


# Correlation clusters: assets that move together
_CORRELATION_CLUSTERS: dict[str, list[str]] = {
    "BTC_CORRELATED": ["BTCUSDT", "BTCDOMUSDT"],
    "ETH_ECOSYSTEM": ["ETHUSDT", "ARBUSDT", "OPUSDT", "MATICUSDT", "STXUSDT", "MANTAUSDT"],
    "SOL_ECOSYSTEM": ["SOLUSDT", "JUPUSDT", "JITOSOLUSDT", "WUSDT", "PYTHUSD", "BONKUSDT"],
    "AI_NARRATIVE":  ["RENDERUSDT", "FETUSDT", "TAOUSDT", "NEARUSDT", "GRTUSDT", "ARUSDT"],
    "MEME":          ["DOGEUSDT", "SHIBUSDT", "PEPEUSDT", "WIFUSDT", "FLOKIUSDT", "BONKUSDT"],
    "DEFI_BLUE":     ["AAVEUSDT", "UNIUSDT", "MKRUSDT", "SNXUSDT", "COMPUSDT", "LINKUSDT"],
    "L1_ALT":        ["AVAXUSDT", "DOTUSDT", "ADAUSDT", "ATOMUSDT", "SUIUSDT", "APTUSDT", "TONUSDT", "TRXUSDT"],
    "GAMING":        ["AXSUSDT", "IMXUSDT", "GALAUSDT", "ILVUSDT", "PIXELUSDT"],
}


def check_correlation_risk(
    symbol: str,
    side: str | None,
    active_signals: list[dict[str, Any]],
    max_cluster_exposure: int = 2,
) -> list[str]:
    """Check if adding a new signal creates concentrated correlation risk.

    Returns a list of warning strings. If the list is non-empty, the caller
    should add them as blockers or warnings.
    """
    if not side or not active_signals:
        return []

    # Find which cluster the new symbol belongs to
    symbol_upper = symbol.upper()
    my_clusters = [
        name for name, members in _CORRELATION_CLUSTERS.items()
        if symbol_upper in members
    ]

    # All crypto is BTC-correlated to some degree
    if not my_clusters:
        my_clusters = ["BTC_CORRELATED"]

    warnings: list[str] = []

    for cluster_name in my_clusters:
        cluster_members = _CORRELATION_CLUSTERS.get(cluster_name, [])
        same_direction_in_cluster = []

        for sig in active_signals:
            sig_symbol = str(sig.get("symbol", "")).upper()
            sig_side = str(sig.get("side", "")).upper()
            sig_status = str(sig.get("status", ""))

            # Only count open signals
            if sig_status not in OPEN_SIGNAL_STATUSES:
                continue

            # Check if in same cluster and same direction
            if sig_symbol in cluster_members and sig_side == side.upper():
                same_direction_in_cluster.append(sig_symbol)

        if len(same_direction_in_cluster) >= max_cluster_exposure:
            warnings.append(
                f"Correlation risk: {len(same_direction_in_cluster)} active {side} signals "
                f"in the {cluster_name.replace('_', ' ')} cluster ({', '.join(same_direction_in_cluster)}). "
                f"Adding {symbol} increases concentrated exposure."
            )

    return warnings


def check_drawdown_breaker(
    recent_signals: list[dict[str, Any]],
    max_consecutive_losses: int = 4,
    max_loss_rate_window: int = 10,
    max_loss_rate_pct: float = 70.0,
) -> list[str]:
    """Check if the system is in a losing streak and should pause signal generation.

    Examines the most recent completed (terminal) signals to detect:
    1. Consecutive losses exceeding the threshold.
    2. Loss rate exceeding max_loss_rate_pct in the last N signals.

    Returns warning strings that should block new signal publication.
    """
    if not recent_signals:
        return []

    # Filter to terminal signals only (ones with known outcomes)
    terminal = [
        s for s in recent_signals
        if str(s.get("status", "")) in TERMINAL_SIGNAL_STATUSES
    ]

    if not terminal:
        return []

    # Sort by creation time (most recent first)
    terminal.sort(key=lambda s: s.get("created_at", ""), reverse=True)

    warnings: list[str] = []

    # Check 1: Consecutive losses
    consecutive_losses = 0
    for sig in terminal:
        status = str(sig.get("status", ""))
        if status in {"STOPPED_OUT", "INVALIDATED"}:
            consecutive_losses += 1
        else:
            break  # Streak broken

    if consecutive_losses >= max_consecutive_losses:
        warnings.append(
            f"Drawdown breaker: {consecutive_losses} consecutive losing signals. "
            f"System is pausing new signal generation until a winning signal occurs."
        )

    # Check 2: Loss rate in window
    window = terminal[:max_loss_rate_window]
    if len(window) >= 5:  # Need at least 5 signals for meaningful rate
        losses = sum(1 for s in window if str(s.get("status", "")) in {"STOPPED_OUT", "INVALIDATED"})
        loss_rate = (losses / len(window)) * 100.0
        if loss_rate >= max_loss_rate_pct:
            warnings.append(
                f"Drawdown breaker: {losses}/{len(window)} ({loss_rate:.0f}%) recent signals were losses. "
                f"Exceeds the {max_loss_rate_pct:.0f}% safety threshold."
            )

    return warnings
