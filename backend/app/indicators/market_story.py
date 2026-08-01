"""Completed-candle market-story reconstruction.

The signal path previously asked only whether the *latest* completed candle
created a structure break or liquidity sweep.  That loses the causal sequence
as soon as the market starts a retest or continuation.  This module scans a
bounded recent window, records events using information that was available at
each event candle, and then evaluates their present lifecycle.

It is intentionally not a backtest or a prediction engine:

* event detection never reads candles to the right of the event;
* later candles are used only to determine acceptance, retest, failure, age,
  and whether an entry would now be a chase;
* every output is derived from completed candles.
"""
from __future__ import annotations

from typing import Any

from app.data_sources.binance_public import Candle, completed_candles
from app.indicators.structure import find_swing_points


DEFAULT_EVENT_LOOKBACK = 24
DEFAULT_STRUCTURE_LOOKBACK = 20
DEFAULT_SWEEP_LOOKBACK = 24
DEFAULT_MAX_ACTIVE_AGE = 12
DEFAULT_FRESH_ENTRY_BARS = 6
# A fresh BOS can occur late in an already mature directional campaign.  The
# campaign is measured from the causal opposing swing using volatility known
# before the first event.  Above the first boundary a completed retest is
# mandatory; above the second boundary the move is considered consumed.
DEFAULT_CAMPAIGN_PULLBACK_ATR = 3.0
DEFAULT_MAX_CAMPAIGN_ENTRY_ATR = 5.0


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _average_true_range(candles: list[Candle], end_index: int, period: int = 14) -> float:
    start = max(0, end_index - period + 1)
    rows = candles[start : end_index + 1]
    if not rows:
        return 0.0
    ranges: list[float] = []
    for offset, candle in enumerate(rows):
        absolute_index = start + offset
        previous_close = (
            _number(candles[absolute_index - 1].close)
            if absolute_index > 0
            else _number(candle.open)
        )
        ranges.append(
            max(
                _number(candle.high) - _number(candle.low),
                abs(_number(candle.high) - previous_close),
                abs(_number(candle.low) - previous_close),
            )
        )
    return sum(ranges) / len(ranges)


def _volume_ratio(candles: list[Candle], index: int, period: int = 20) -> float:
    prior = candles[max(0, index - period) : index]
    if not prior:
        return 0.0
    average = sum(
        _number(candle.quote_volume) or _number(candle.volume)
        for candle in prior
    ) / len(prior)
    current = _number(candles[index].quote_volume) or _number(candles[index].volume)
    return current / average if average > 0 else 0.0


def _swing_bias(
    highs: list[dict[str, Any]],
    lows: list[dict[str, Any]],
) -> str:
    """Classify the structure known immediately before an event candle."""
    if len(highs) < 2 or len(lows) < 2:
        return "NEUTRAL"
    higher_high = _number(highs[-1]["price"]) > _number(highs[-2]["price"])
    higher_low = _number(lows[-1]["price"]) > _number(lows[-2]["price"])
    lower_high = _number(highs[-1]["price"]) < _number(highs[-2]["price"])
    lower_low = _number(lows[-1]["price"]) < _number(lows[-2]["price"])
    if higher_high and higher_low:
        return "BULLISH"
    if lower_high and lower_low:
        return "BEARISH"
    return "NEUTRAL"


def _event_geometry(
    candles: list[Candle],
    *,
    index: int,
    direction: str,
    level: float,
) -> dict[str, Any]:
    candle = candles[index]
    candle_range = max(_number(candle.high) - _number(candle.low), 1e-12)
    body_ratio = abs(_number(candle.close) - _number(candle.open)) / candle_range
    close_location = (
        (_number(candle.close) - _number(candle.low)) / candle_range
        if direction == "BULLISH"
        else (_number(candle.high) - _number(candle.close)) / candle_range
    )
    atr_before_event = max(
        _average_true_range(candles, max(0, index - 1)),
        abs(level) * 1e-8,
        1e-12,
    )
    atr = max(_average_true_range(candles, index), abs(level) * 1e-8, 1e-12)
    displacement = (
        (_number(candle.close) - level) / atr
        if direction == "BULLISH"
        else (level - _number(candle.close)) / atr
    )
    relative_volume = _volume_ratio(candles, index)
    decisive = body_ratio >= 0.55 and close_location >= 0.60
    return {
        "event_open": _number(candle.open),
        "event_high": _number(candle.high),
        "event_low": _number(candle.low),
        "event_close": _number(candle.close),
        "atr_before_event": atr_before_event,
        "atr_at_event": atr,
        "body_ratio": round(body_ratio, 4),
        "close_location": round(close_location, 4),
        "relative_volume": round(relative_volume, 3),
        "displacement_atr": round(displacement, 3),
        "decisive_candle": decisive,
        "relative_volume_confirmed": relative_volume >= 1.5,
        "quality": (
            "STRONG"
            if decisive and relative_volume >= 1.5
            else "VALID"
            if decisive or displacement >= 0.10
            else "WEAK"
        ),
    }


def _structure_events(
    candles: list[Candle],
    *,
    event_lookback: int,
    structure_lookback: int,
    swing_window: int,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if len(candles) < structure_lookback + 2:
        return events

    all_highs, all_lows = find_swing_points(candles, N=swing_window)
    start = max(structure_lookback, len(candles) - event_lookback)
    previous_event: dict[str, Any] | None = None
    for index in range(start, len(candles)):
        # A pivot is usable only after its right-side confirmation candles had
        # completed. Newer pivots found in the full scan are deliberately
        # filtered out, preserving prefix invariance without recomputing every
        # historical prefix.
        known_highs = [
            swing for swing in all_highs
            if int(swing["index"]) + swing_window < index
        ]
        known_lows = [
            swing for swing in all_lows
            if int(swing["index"]) + swing_window < index
        ]
        prior = candles[index - structure_lookback : index]
        previous_close = _number(candles[index - 1].close)
        current_close = _number(candles[index].close)
        rolling_high = max(_number(candle.high) for candle in prior)
        rolling_low = min(_number(candle.low) for candle in prior)

        bullish_references: list[tuple[str, float, int | None]] = []
        bearish_references: list[tuple[str, float, int | None]] = []
        if known_highs:
            swing = known_highs[-1]
            level = _number(swing["price"])
            if current_close > level >= previous_close:
                bullish_references.append(("CONFIRMED_SWING", level, int(swing["index"])))
        if known_lows:
            swing = known_lows[-1]
            level = _number(swing["price"])
            if current_close < level <= previous_close:
                bearish_references.append(("CONFIRMED_SWING", level, int(swing["index"])))
        if current_close > rolling_high >= previous_close:
            bullish_references.append(("ROLLING_20_STRUCTURE", rolling_high, None))
        if current_close < rolling_low <= previous_close:
            bearish_references.append(("ROLLING_20_STRUCTURE", rolling_low, None))

        direction = ""
        references: list[tuple[str, float, int | None]] = []
        if bullish_references:
            direction, references = "BULLISH", bullish_references
            source, level, swing_index = max(references, key=lambda item: item[1])
        elif bearish_references:
            direction, references = "BEARISH", bearish_references
            source, level, swing_index = min(references, key=lambda item: item[1])
        else:
            continue

        prior_bias = _swing_bias(known_highs, known_lows)
        event_type = (
            "CHoCH"
            if prior_bias in {"BULLISH", "BEARISH"} and prior_bias != direction
            else "BOS"
        )
        geometry = _event_geometry(
            candles,
            index=index,
            direction=direction,
            level=level,
        )
        # Locate the opposing swing that began this directional campaign using
        # information already confirmed before the event. If no confirmed
        # pivot exists, use the bounded pre-event range extreme.
        origin_swings = known_lows if direction == "BULLISH" else known_highs
        if origin_swings:
            local_origin = origin_swings[-1]
            local_origin_index = int(local_origin["index"])
            local_origin_price = _number(local_origin["price"])
            local_origin_source = "CONFIRMED_OPPOSING_SWING"
        else:
            origin_window_start = max(0, index - structure_lookback)
            origin_window = candles[origin_window_start:index]
            if direction == "BULLISH":
                local_offset, local_candle = min(
                    enumerate(origin_window),
                    key=lambda item: _number(item[1].low),
                )
                local_origin_price = _number(local_candle.low)
            else:
                local_offset, local_candle = max(
                    enumerate(origin_window),
                    key=lambda item: _number(item[1].high),
                )
                local_origin_price = _number(local_candle.high)
            local_origin_index = origin_window_start + local_offset
            local_origin_source = "BOUNDED_PRE_EVENT_EXTREME"

        continue_campaign = bool(
            previous_event
            and previous_event.get("direction") == direction
            and index - int(previous_event.get("event_index", index)) <= DEFAULT_MAX_ACTIVE_AGE
        )
        if continue_campaign:
            campaign_origin_index = int(previous_event["campaign_origin_index"])
            campaign_origin_price = _number(previous_event["campaign_origin_price"])
            campaign_origin_source = str(previous_event["campaign_origin_source"])
            campaign_atr = max(_number(previous_event["campaign_atr"]), 1e-12)
            campaign_sequence = int(previous_event.get("campaign_event_sequence", 1)) + 1
            campaign_id = str(previous_event["campaign_id"])
        else:
            campaign_origin_index = local_origin_index
            campaign_origin_price = local_origin_price
            campaign_origin_source = local_origin_source
            campaign_atr = max(_number(geometry.get("atr_before_event")), 1e-12)
            campaign_sequence = 1
            origin_close_time = int(candles[campaign_origin_index].close_time)
            campaign_id = f"{direction}:{origin_close_time}:{campaign_origin_price:.12g}"

        campaign_distance = (
            (_number(candles[index].close) - campaign_origin_price) / campaign_atr
            if direction == "BULLISH"
            else (campaign_origin_price - _number(candles[index].close)) / campaign_atr
        )
        event = {
                "event_id": f"{event_type}:{direction}:{int(candles[index].close_time)}:{level:.12g}",
                "detected": True,
                "type": event_type,
                "direction": direction,
                "direction_lower": direction.lower(),
                "break_level": level,
                "broken_level": level,
                "source": source,
                "reference_levels": [
                    {"source": item[0], "price": item[1], "swing_index": item[2]}
                    for item in references
                ],
                "swing_index": swing_index,
                "event_index": index,
                "event_open_time": int(candles[index].open_time),
                "event_close_time": int(candles[index].close_time),
                "prior_structure_bias": prior_bias,
                "local_impulse_origin_index": local_origin_index,
                "local_impulse_origin_price": local_origin_price,
                "local_impulse_origin_source": local_origin_source,
                "campaign_id": campaign_id,
                "campaign_origin_index": campaign_origin_index,
                "campaign_origin_price": campaign_origin_price,
                "campaign_origin_source": campaign_origin_source,
                "campaign_atr": campaign_atr,
                "campaign_atr_basis": "PRE_FIRST_EVENT_ATR",
                "campaign_event_sequence": campaign_sequence,
                "campaign_age_bars_at_event": index - campaign_origin_index,
                "campaign_distance_atr_at_event": round(campaign_distance, 3),
                **geometry,
            }
        events.append(event)
        previous_event = event
    return events


def _liquidity_events(
    candles: list[Candle],
    *,
    event_lookback: int,
    sweep_lookback: int,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if len(candles) < sweep_lookback + 1:
        return events
    start = max(sweep_lookback, len(candles) - event_lookback)
    for index in range(start, len(candles)):
        signal = candles[index]
        prior = candles[index - sweep_lookback : index]
        prior_high = max(_number(candle.high) for candle in prior)
        prior_low = min(_number(candle.low) for candle in prior)
        direction = ""
        level = 0.0
        extreme = 0.0
        if _number(signal.high) > prior_high and _number(signal.close) < prior_high:
            direction, level, extreme = "BEARISH", prior_high, _number(signal.high)
        elif _number(signal.low) < prior_low and _number(signal.close) > prior_low:
            direction, level, extreme = "BULLISH", prior_low, _number(signal.low)
        else:
            continue
        geometry = _event_geometry(
            candles,
            index=index,
            direction=direction,
            level=level,
        )
        events.append(
            {
                "event_id": f"LIQUIDITY_SWEEP:{direction}:{int(signal.close_time)}:{level:.12g}",
                "detected": True,
                "type": "LIQUIDITY_SWEEP",
                "direction": direction,
                "direction_lower": direction.lower(),
                "swept_level": level,
                "break_level": level,
                "sweep_extreme": extreme,
                "close_back_inside": _number(signal.close),
                "event_index": index,
                "event_open_time": int(signal.open_time),
                "event_close_time": int(signal.close_time),
                "source": f"PRIOR_{sweep_lookback}_CANDLE_RANGE",
                **geometry,
            }
        )
    return events


def _complete_lifecycle(
    event: dict[str, Any],
    candles: list[Candle],
    *,
    max_active_age: int,
    fresh_entry_bars: int,
    campaign_pullback_atr: float,
    max_campaign_entry_atr: float,
) -> dict[str, Any]:
    result = dict(event)
    index = int(event["event_index"])
    direction = str(event["direction"])
    level = _number(event["break_level"])
    subsequent = candles[index + 1 :]
    latest = candles[-1]
    age = len(candles) - 1 - index
    # Lifecycle boundaries must not move after the event.  Using the latest
    # ATR here used to widen an old event's invalidation tolerance and rescale
    # its MFE whenever a later volatility spike arrived.  In the worst case,
    # an INVALIDATED event could be reconstructed as RETESTING.  The event ATR
    # is causal at detection time and remains the fixed unit of account for
    # this event's entire lifecycle.
    event_atr = max(_number(event["atr_at_event"]), abs(level) * 1e-8, 1e-12)
    campaign_origin = _number(event.get("campaign_origin_price"), _number(event.get("event_open")))
    campaign_atr = max(
        _number(event.get("campaign_atr"), _number(event.get("atr_before_event"), event_atr)),
        abs(level) * 1e-8,
        1e-12,
    )
    tolerance = max(event_atr * 0.20, abs(level) * 0.0003)

    if direction == "BULLISH":
        favourable = lambda price: price >= level - tolerance
        invalid_close = lambda candle: _number(candle.close) < level - tolerance
        touches_retest = lambda candle: _number(candle.low) <= level + tolerance and favourable(_number(candle.close))
        distance_atr = lambda candle: (_number(candle.close) - level) / event_atr
        favourable_excursion = lambda candle: (_number(candle.high) - level) / event_atr
        campaign_distance = lambda candle: (_number(candle.close) - campaign_origin) / campaign_atr
        invalidation_level = level - tolerance
    else:
        favourable = lambda price: price <= level + tolerance
        invalid_close = lambda candle: _number(candle.close) > level + tolerance
        touches_retest = lambda candle: _number(candle.high) >= level - tolerance and favourable(_number(candle.close))
        distance_atr = lambda candle: (level - _number(candle.close)) / event_atr
        favourable_excursion = lambda candle: (level - _number(candle.low)) / event_atr
        campaign_distance = lambda candle: (campaign_origin - _number(candle.close)) / campaign_atr
        invalidation_level = level + tolerance

    signed_distance = distance_atr(latest)
    # Walk forward in candle time and freeze the first terminal transition.
    # Reconstructing the same event with a longer completed-candle suffix can
    # add observations, but can never rehabilitate a terminal event.
    invalidated_at: int | None = None
    terminal_state: str | None = None
    terminal_at: int | None = None
    terminal_reason = ""
    running_max_favourable = 0.0
    campaign_distance_at_event = campaign_distance(candles[index])
    for candle_index in range(index, len(candles)):
        candle = candles[candle_index]
        bar_age = candle_index - index
        running_max_favourable = max(
            running_max_favourable,
            favourable_excursion(candle),
        )
        if candle_index > index and invalid_close(candle):
            terminal_state = "INVALIDATED"
            terminal_at = candle_index
            invalidated_at = candle_index
            terminal_reason = (
                f"The structure level failed on completed candle {bar_age} after the event."
            )
            break
        if bar_age > max_active_age:
            terminal_state = "EXPIRED"
            terminal_at = candle_index
            terminal_reason = (
                f"The event is {bar_age} bars old, beyond the "
                f"{max_active_age}-bar validity window."
            )
            break
        bar_distance = distance_atr(candle)
        if bar_distance > 2.50:
            terminal_state = "EXTENDED_DO_NOT_CHASE"
            terminal_at = candle_index
            terminal_reason = (
                f"Price moved {bar_distance:.2f} event ATR beyond the event "
                "level; the original entry has moved away."
            )
            break
        if bar_age > fresh_entry_bars and running_max_favourable >= 1.50:
            terminal_state = "MISSED"
            terminal_at = candle_index
            terminal_reason = (
                f"The move had already expanded {running_max_favourable:.2f} "
                f"event ATR when the {fresh_entry_bars}-bar fresh-entry window passed."
            )
            break
        # Evaluate total campaign consumption only after the event-local
        # terminal rules. This preserves the more precise explanation when an
        # event itself already extended or aged out, while still preventing a
        # brand-new late BOS from resetting a mature campaign to "fresh".
        leg_distance = campaign_distance(candle)
        if leg_distance > max_campaign_entry_atr:
            terminal_state = "LATE_STRUCTURE_DO_NOT_CHASE"
            terminal_at = candle_index
            terminal_reason = (
                f"The {direction.lower()} campaign has already travelled {leg_distance:.2f} "
                f"pre-event ATR from its causal origin at {campaign_origin:.8g}; "
                "the move is mature and a new entry would chase the structure."
            )
            break

    lifecycle_end = terminal_at if terminal_at is not None else len(candles) - 1
    # Excursion belongs to this event only until its first terminal
    # transition. A later unrelated move must not rewrite the event's recorded
    # opportunity after invalidation/expiry/miss/extension.
    max_favourable = max(
        [
            0.0,
            *(
                favourable_excursion(candle)
                for candle in candles[index : lifecycle_end + 1]
            ),
        ]
    )
    retest_indexes = [
        offset
        for offset, candle in enumerate(subsequent, start=index + 1)
        if offset <= lifecycle_end and touches_retest(candle)
    ]
    recent_retest = bool(
        terminal_state is None
        and retest_indexes
        and retest_indexes[-1] >= len(candles) - 2
    )
    accepted = (
        _number(event.get("displacement_atr")) >= 0.10
        or bool(event.get("decisive_candle"))
        or any(favourable(_number(candle.close)) for candle in subsequent[:2])
    )
    current_campaign_distance = campaign_distance(latest)
    pullback_required = campaign_distance_at_event > campaign_pullback_atr

    state = "DEVELOPING"
    actionable = False
    reason = "The completed event is waiting for acceptance and a usable entry location."
    if terminal_state is not None:
        state = terminal_state
        reason = terminal_reason
    elif recent_retest and accepted:
        state = "RETESTING"
        actionable = True
        reason = "Price is testing the event level and the latest completed candle still holds it."
    elif accepted and pullback_required:
        state = "PULLBACK_REQUIRED"
        reason = (
            f"The structure event formed {campaign_distance_at_event:.2f} pre-event ATR from "
            "its causal origin. Do not enter the breakout; wait for a completed retest."
        )
    elif accepted and age <= fresh_entry_bars and -0.20 <= signed_distance <= 1.75:
        state = "ACTIONABLE_NOW"
        actionable = True
        reason = "The event remains fresh, accepted, and close enough to its structure level for review."
    elif accepted:
        reason = "The event remains valid, but current location still needs a fresh retest or continuation trigger."

    result.update(
        {
            "age_bars": age,
            "state": state,
            "actionable": actionable,
            "chase_prohibited": state in {
                "EXTENDED_DO_NOT_CHASE",
                "LATE_STRUCTURE_DO_NOT_CHASE",
                "PULLBACK_REQUIRED",
                "MISSED",
                "INVALIDATED",
                "EXPIRED",
            },
            "accepted": accepted,
            "retest_observed": bool(retest_indexes),
            "recent_retest": recent_retest,
            "retest_count": len(retest_indexes),
            "last_retest_index": retest_indexes[-1] if retest_indexes else None,
            "current_close": _number(latest.close),
            "current_distance_atr": round(signed_distance, 3),
            "campaign_distance_atr_at_event": round(campaign_distance_at_event, 3),
            "campaign_distance_atr_current": round(current_campaign_distance, 3),
            "campaign_pullback_required_atr": campaign_pullback_atr,
            "campaign_max_entry_atr": max_campaign_entry_atr,
            "campaign_maturity": (
                "LATE"
                if max(campaign_distance_at_event, current_campaign_distance) > max_campaign_entry_atr
                else "PULLBACK_REQUIRED"
                if campaign_distance_at_event > campaign_pullback_atr
                else "EARLY"
            ),
            "entry_timing": (
                "DO_NOT_CHASE"
                if state in {"LATE_STRUCTURE_DO_NOT_CHASE", "EXTENDED_DO_NOT_CHASE", "MISSED", "EXPIRED", "INVALIDATED"}
                else "RETEST_ENTRY"
                if state == "RETESTING"
                else "WAIT_FOR_PULLBACK"
                if state == "PULLBACK_REQUIRED"
                else "EARLY_REVIEW"
            ),
            "max_favourable_excursion_atr": round(max_favourable, 3),
            "invalidation_level": invalidation_level,
            "invalidated_at_index": invalidated_at,
            "terminal_at_index": terminal_at,
            "lifecycle_atr": event_atr,
            "lifecycle_atr_basis": "EVENT_CANDLE_ATR",
            "reason": reason,
            "reason_code": {
                "ACTIONABLE_NOW": "FRESH_EVENT_LOCATION",
                "RETESTING": "EVENT_LEVEL_RETEST_HOLDING",
                "DEVELOPING": "EVENT_ACCEPTANCE_DEVELOPING",
                "EXTENDED_DO_NOT_CHASE": "ENTRY_EXTENDED_DO_NOT_CHASE",
                "LATE_STRUCTURE_DO_NOT_CHASE": "CAUSAL_CAMPAIGN_ALREADY_CONSUMED",
                "PULLBACK_REQUIRED": "MATURE_CAMPAIGN_REQUIRES_RETEST",
                "MISSED": "FRESH_ENTRY_WINDOW_MISSED",
                "INVALIDATED": "EVENT_LEVEL_INVALIDATED",
                "EXPIRED": "EVENT_AGE_EXPIRED",
            }.get(state, "EVENT_STATE_UNKNOWN"),
        }
    )
    return result


def _empty_event(reason: str) -> dict[str, Any]:
    return {
        "detected": False,
        "direction": "none",
        "state": "NO_ACTIVE_EVENT",
        "actionable": False,
        "chase_prohibited": False,
        "reason": reason,
        "reason_code": "NO_RECENT_EVENT",
    }


def evaluate_story_direction(story: dict[str, Any], direction: str) -> dict[str, Any]:
    """Return the current completed-candle event state for one proposed side."""
    normalized = str(direction).upper()
    if normalized not in {"BULLISH", "BEARISH"}:
        return {
            "direction": normalized or "NEUTRAL",
            "state": "NO_DIRECTION",
            "actionable": False,
            "reason": "A directional thesis is required before market-story evaluation.",
            "reason_code": "DIRECTION_REQUIRED",
            "aligned_event": None,
            "opposing_event": None,
        }

    events = list(story.get("structure_events") or [])
    sweeps = list(story.get("liquidity_events") or [])
    aligned = next((event for event in events if event.get("direction") == normalized), None)
    aligned_sweep = next((event for event in sweeps if event.get("direction") == normalized), None)
    opposite = "BEARISH" if normalized == "BULLISH" else "BULLISH"
    opposing = next(
        (
            event
            for event in events
            if event.get("direction") == opposite
            and event.get("state") not in {"INVALIDATED", "EXPIRED"}
        ),
        None,
    )

    candidate = aligned
    if aligned_sweep and (
        candidate is None
        or int(aligned_sweep.get("event_index", -1)) > int(candidate.get("event_index", -1))
    ):
        candidate = aligned_sweep
    if candidate is None:
        return {
            "direction": normalized,
            "state": "NO_ACTIVE_EVENT",
            "actionable": False,
            "reason": f"No recent completed-candle {normalized.lower()} structure event or liquidity sweep is available.",
            "reason_code": "NO_ALIGNED_RECENT_EVENT",
            "aligned_event": None,
            "aligned_structure_event": aligned,
            "aligned_liquidity_event": aligned_sweep,
            "opposing_event": opposing,
        }

    later_opposition = (
        opposing
        if opposing
        and int(opposing.get("event_index", -1)) > int(candidate.get("event_index", -1))
        else None
    )
    if later_opposition:
        return {
            "direction": normalized,
            "state": "INVALIDATED",
            "actionable": False,
            "chase_prohibited": True,
            "reason": (
                f"A newer {opposite.lower()} {later_opposition.get('type', 'structure event')} "
                "superseded the proposed market story."
            ),
            "reason_code": "SUPERSEDED_BY_OPPOSING_EVENT",
            "aligned_event": candidate,
            "aligned_structure_event": aligned,
            "aligned_liquidity_event": aligned_sweep,
            "opposing_event": later_opposition,
        }

    return {
        "direction": normalized,
        "state": candidate.get("state", "DEVELOPING"),
        "actionable": bool(candidate.get("actionable")),
        "chase_prohibited": bool(candidate.get("chase_prohibited")),
        "reason": candidate.get("reason"),
        "reason_code": candidate.get("reason_code"),
        "aligned_event": candidate,
        "aligned_structure_event": aligned,
        "aligned_liquidity_event": aligned_sweep,
        "opposing_event": opposing,
    }


def evaluate_story_playbook(
    *,
    primary_story: dict[str, Any],
    higher_story: dict[str, Any] | None,
    direction: str,
    primary_phase: str,
    higher_phase: str = "UNAVAILABLE",
    vwap_context: dict[str, Any] | None = None,
    volume_profile: dict[str, Any] | None = None,
    require_higher_timeframe: bool = True,
) -> dict[str, Any]:
    """Apply one shared structure-timing playbook across Radar and Research."""
    normalized = str(direction).upper()
    view = evaluate_story_direction(primary_story, normalized)
    higher_view = evaluate_story_direction(higher_story or {}, normalized)
    structure_event = view.get("aligned_structure_event") or {}
    sweep_event = view.get("aligned_liquidity_event") or {}
    opposing_event = view.get("opposing_event") or {}
    structure_opposed = (
        bool(opposing_event)
        and int(opposing_event.get("event_index", -1))
        > int(structure_event.get("event_index", -1))
    )
    sweep_opposed = (
        bool(opposing_event)
        and int(opposing_event.get("event_index", -1))
        > int(sweep_event.get("event_index", -1))
    )
    higher_opposition = higher_view.get("opposing_event") or {}
    higher_structure_opposed = (
        bool(higher_opposition)
        and int(higher_opposition.get("event_index", -1))
        > int((higher_view.get("aligned_structure_event") or {}).get("event_index", -1))
    )
    phase_direction = {
        "MARKUP": "BULLISH",
        "ACCUMULATION": "BULLISH",
        "MARKDOWN": "BEARISH",
        "DISTRIBUTION": "BEARISH",
    }
    primary_bias = phase_direction.get(str(primary_phase), "NEUTRAL")
    higher_bias = phase_direction.get(str(higher_phase), "NEUTRAL")
    event_quality_ready = (
        bool(structure_event.get("decisive_candle"))
        and bool(structure_event.get("relative_volume_confirmed"))
    )
    structure_actionable = (
        bool(structure_event.get("actionable"))
        and not structure_opposed
    )
    sweep_actionable = bool(sweep_event.get("actionable")) and not sweep_opposed
    vwap = vwap_context or {}
    profile = volume_profile or {}
    expected_vwap = "ABOVE_ALL" if normalized == "BULLISH" else "BELOW_ALL"
    expected_profile = "ABOVE_POC_ACCEPTANCE" if normalized == "BULLISH" else "BELOW_POC_ACCEPTANCE"
    vwap_aligned = bool(vwap.get("available", True)) and vwap.get("price_relation") == expected_vwap
    profile_aligned = bool(profile.get("available", True)) and profile.get("location") == expected_profile

    trend_regime = (
        primary_bias == normalized
        and (
            higher_bias == normalized
            if require_higher_timeframe
            else True
        )
    )
    range_regime = (
        primary_phase in {"RANGING", "ACCUMULATION", "DISTRIBUTION"}
        and primary_bias in {"NEUTRAL", normalized}
        and (
            higher_phase == "RANGING"
            if require_higher_timeframe
            else True
        )
    )
    higher_clear = not higher_structure_opposed if require_higher_timeframe else True
    trend_ready = trend_regime and structure_actionable and event_quality_ready and higher_clear
    range_ready = (
        range_regime
        and sweep_actionable
        and vwap_aligned
        and profile_aligned
        and higher_clear
    )
    passed = trend_ready or range_ready
    playbook = (
        "TREND_CONTINUATION"
        if trend_ready
        else "RANGE_SWEEP_REVERSAL"
        if range_ready
        else "NONE"
    )
    reasons: list[str] = []
    if not trend_regime and not range_regime:
        reasons.append(
            f"No approved regime playbook: primary={primary_phase}, higher={higher_phase}."
        )
    if trend_regime and not structure_actionable:
        reasons.append(
            f"Completed-candle market story is {view.get('state', 'not actionable')}: "
            f"{view.get('reason') or 'no fresh structure event is available.'}"
        )
    if trend_regime and structure_actionable and not event_quality_ready:
        if not structure_event.get("relative_volume_confirmed"):
            reasons.append(
                f"Structure-event relative volume {_number(structure_event.get('relative_volume')):.2f}x "
                "is below the 1.50x threshold."
            )
        if not structure_event.get("decisive_candle"):
            reasons.append("The originating structure-event candle lacks decisive body or close location.")
    if range_regime and not sweep_actionable:
        reasons.append(
            f"No fresh actionable {normalized.lower()} liquidity-sweep reversal is present."
        )
    if range_regime and not vwap_aligned:
        reasons.append(f"Price has not accepted {expected_vwap.replace('_', ' ').lower()}.")
    if range_regime and not profile_aligned:
        reasons.append(f"Price has not accepted {expected_profile.replace('_', ' ').lower()}.")
    if not higher_clear:
        reasons.append("A newer higher-timeframe structure event opposes the proposed direction.")

    if passed:
        reason_code = "PLAYBOOK_CONFIRMED"
    elif not trend_regime and not range_regime:
        reason_code = "REGIME_PLAYBOOK_UNAVAILABLE"
    elif not higher_clear:
        reason_code = "HIGHER_TIMEFRAME_STRUCTURE_OPPOSED"
    elif trend_regime and not structure_actionable:
        reason_code = "STRUCTURE_EVENT_NOT_ACTIONABLE"
    elif trend_regime and not event_quality_ready:
        reason_code = "STRUCTURE_EVENT_QUALITY_UNCONFIRMED"
    elif range_regime and not sweep_actionable:
        reason_code = "RANGE_SWEEP_UNCONFIRMED"
    elif range_regime and (not vwap_aligned or not profile_aligned):
        reason_code = "RANGE_ACCEPTANCE_UNCONFIRMED"
    else:
        reason_code = "PLAYBOOK_NOT_CONFIRMED"

    return {
        "passed": passed,
        "playbook": playbook,
        "direction": normalized,
        "story_state": view.get("state", "NO_ACTIVE_EVENT"),
        "actionable": bool(view.get("actionable")),
        "reason": reasons[0] if reasons else "Completed-candle structure timing and regime are aligned.",
        "reason_code": reason_code,
        "reasons": reasons,
        "selected_event": structure_event if trend_ready or trend_regime else sweep_event,
        "selected_structure_event": structure_event,
        "selected_liquidity_event": sweep_event,
        "directional_view": view,
        "higher_directional_view": higher_view,
        "checks": {
            "trend_regime_aligned": trend_regime,
            "range_regime_aligned": range_regime,
            "structure_event_actionable": structure_actionable,
            "structure_event_quality_ready": event_quality_ready,
            "liquidity_sweep_actionable": sweep_actionable,
            "vwap_acceptance_aligned": vwap_aligned,
            "profile_acceptance_aligned": profile_aligned,
            "higher_structure_not_opposed": higher_clear,
        },
    }


def _event_sentence(event: dict[str, Any] | None) -> str:
    if not event:
        return "No recent completed-candle structure break or liquidity sweep was confirmed."
    event_type = str(event.get("type", "structure event")).replace("_", " ")
    direction = str(event.get("direction", "")).lower()
    age = int(event.get("age_bars", 0))
    timing = "on the latest completed candle" if age == 0 else f"{age} completed bars ago"
    return (
        f"A {direction} {event_type} occurred {timing} through "
        f"{_number(event.get('break_level')):.8g}."
    )


def _current_sentence(event: dict[str, Any] | None) -> str:
    if not event:
        return "Price is not attached to a recent qualified structure event."
    state = str(event.get("state", "DEVELOPING"))
    messages = {
        "ACTIONABLE_NOW": "The event remains fresh and price is still in a reviewable location.",
        "RETESTING": "Price is retesting the event level and has not invalidated it on a completed close.",
        "DEVELOPING": "The event remains under observation while acceptance or a fresh trigger develops.",
        "EXTENDED_DO_NOT_CHASE": "Price has extended too far from the event level; chasing is prohibited.",
        "LATE_STRUCTURE_DO_NOT_CHASE": "The directional campaign was already mature when this structure event appeared; a new entry is prohibited.",
        "PULLBACK_REQUIRED": "The campaign is developed enough that only a completed pullback and retest can reopen entry review.",
        "MISSED": "The original opportunity has already travelled away from its fresh-entry window.",
        "INVALIDATED": "Later completed price action invalidated the event.",
        "EXPIRED": "The event is too old to authorize a new setup.",
    }
    return messages.get(state, str(event.get("reason", "The event is being monitored.")))


def build_market_story(
    candles: list[Candle],
    *,
    event_lookback: int = DEFAULT_EVENT_LOOKBACK,
    structure_lookback: int = DEFAULT_STRUCTURE_LOOKBACK,
    sweep_lookback: int = DEFAULT_SWEEP_LOOKBACK,
    swing_window: int = 3,
    max_active_age: int = DEFAULT_MAX_ACTIVE_AGE,
    fresh_entry_bars: int = DEFAULT_FRESH_ENTRY_BARS,
    campaign_pullback_atr: float = DEFAULT_CAMPAIGN_PULLBACK_ATR,
    max_campaign_entry_atr: float = DEFAULT_MAX_CAMPAIGN_ENTRY_ATR,
) -> dict[str, Any]:
    """Reconstruct recent events and describe the market's current location."""
    closed = completed_candles(candles)
    minimum = max(structure_lookback + 2, sweep_lookback + 1)
    if len(closed) < minimum:
        unavailable = _empty_event("Insufficient completed candles for recent market-story reconstruction.")
        return {
            "available": False,
            "schema_version": "market_story.v1",
            "method": "completed_candle_market_story_v1",
            "no_lookahead": True,
            "completed_candles": len(closed),
            "structure_events": [],
            "liquidity_events": [],
            "latest_structure_event": unavailable,
            "latest_liquidity_event": unavailable.copy(),
            "latest_event": unavailable.copy(),
            "current_state": "NO_ACTIVE_EVENT",
            "what_happened": unavailable["reason"],
            "what_is_happening": "Waiting for sufficient completed market history.",
            "next_scenarios": [],
            "actionability": unavailable.copy(),
        }

    raw_structure = _structure_events(
        closed,
        event_lookback=max(1, event_lookback),
        structure_lookback=max(5, structure_lookback),
        swing_window=max(1, swing_window),
    )
    raw_liquidity = _liquidity_events(
        closed,
        event_lookback=max(1, event_lookback),
        sweep_lookback=max(5, sweep_lookback),
    )
    structure_events = [
        _complete_lifecycle(
            event,
            closed,
            max_active_age=max_active_age,
            fresh_entry_bars=fresh_entry_bars,
            campaign_pullback_atr=max(1.0, campaign_pullback_atr),
            max_campaign_entry_atr=max(
                campaign_pullback_atr + 0.5,
                max_campaign_entry_atr,
            ),
        )
        for event in raw_structure
    ]
    liquidity_events = [
        _complete_lifecycle(
            event,
            closed,
            max_active_age=min(max_active_age, 10),
            fresh_entry_bars=min(fresh_entry_bars, 5),
            # Reversal sweeps begin at the swept level itself rather than a
            # preceding structure campaign, so retain their event-distance
            # lifecycle and do not apply the structure-campaign gate.
            campaign_pullback_atr=10_000.0,
            max_campaign_entry_atr=10_001.0,
        )
        for event in raw_liquidity
    ]
    structure_events.sort(key=lambda event: int(event["event_index"]), reverse=True)
    liquidity_events.sort(key=lambda event: int(event["event_index"]), reverse=True)
    # Keep the transport and persisted snapshots bounded even in exceptionally
    # noisy markets. Directional selection needs only the most recent events.
    structure_events = structure_events[:12]
    liquidity_events = liquidity_events[:8]
    combined = sorted(
        [*structure_events, *liquidity_events],
        key=lambda event: int(event["event_index"]),
        reverse=True,
    )
    latest_structure = structure_events[0] if structure_events else _empty_event("No recent structure break was confirmed.")
    latest_liquidity = liquidity_events[0] if liquidity_events else _empty_event("No recent liquidity sweep was confirmed.")
    latest_event = combined[0] if combined else _empty_event("No recent completed-candle market event was confirmed.")
    current_state = str(latest_event.get("state", "NO_ACTIVE_EVENT"))
    actionability = {
        "status": current_state,
        "actionable": bool(latest_event.get("actionable")),
        "chase_prohibited": bool(latest_event.get("chase_prohibited")),
        "direction": latest_event.get("direction", "none"),
        "reason": latest_event.get("reason"),
        "reason_code": latest_event.get("reason_code"),
        "event_age_bars": latest_event.get("age_bars"),
        "event_type": latest_event.get("type"),
        "event_level": latest_event.get("break_level"),
        "entry_timing": latest_event.get("entry_timing"),
        "campaign_maturity": latest_event.get("campaign_maturity"),
        "campaign_origin_price": latest_event.get("campaign_origin_price"),
        "campaign_distance_atr": latest_event.get("campaign_distance_atr_current"),
        "campaign_max_entry_atr": latest_event.get("campaign_max_entry_atr"),
    }
    direction = str(latest_event.get("direction", "")).upper()
    if direction == "BULLISH":
        continuation_condition = "Completed closes hold the event level while live flow and positioning remain bullish."
        failure_condition = "A completed close loses the event invalidation level or a newer bearish structure event appears."
    elif direction == "BEARISH":
        continuation_condition = "Completed closes hold below the event level while live flow and positioning remain bearish."
        failure_condition = "A completed close reclaims the event invalidation level or a newer bullish structure event appears."
    else:
        continuation_condition = "A fresh completed structure event forms with aligned causal evidence."
        failure_condition = "No directional event is active; remain in observation mode."

    return {
        "available": True,
        "schema_version": "market_story.v1",
        "method": "completed_candle_market_story_v1",
        "no_lookahead": True,
        "completed_candles": len(closed),
        "scan_window_bars": event_lookback,
        "fresh_entry_bars": fresh_entry_bars,
        "max_active_age_bars": max_active_age,
        "campaign_pullback_atr": campaign_pullback_atr,
        "max_campaign_entry_atr": max_campaign_entry_atr,
        "current_price": _number(closed[-1].close),
        "latest_completed_close_time": int(closed[-1].close_time),
        "as_of_close_time": int(closed[-1].close_time),
        "structure_events": structure_events,
        "liquidity_events": liquidity_events,
        "latest_structure_event": latest_structure,
        "latest_liquidity_event": latest_liquidity,
        "latest_event": latest_event,
        "current_state": current_state,
        "what_happened": _event_sentence(latest_event if latest_event.get("detected") else None),
        "what_is_happening": _current_sentence(latest_event if latest_event.get("detected") else None),
        "next_scenarios": [
            {
                "scenario": "CONTINUATION",
                "condition": continuation_condition,
                "action": "Review only while the event remains actionable and reward still exceeds risk.",
            },
            {
                "scenario": "FAILURE_OR_REVERSAL",
                "condition": failure_condition,
                "action": "Invalidate the old thesis and reconstruct the market story from the newer event.",
            },
        ],
        "actionability": actionability,
        "limitations": [
            "This is completed-candle event reconstruction, not certainty about the next price move.",
            "A later same-direction BOS does not reset a mature campaign back to an early entry.",
            "Live order flow, positioning, liquidity, spread, and reward-to-risk remain separate publication controls.",
        ],
    }


def observable_structure_events(story: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Compatibility view for existing BOS/CHoCH consumers."""
    structure_events = list(story.get("structure_events") or [])
    bos = next((event for event in structure_events if event.get("type") == "BOS"), None)
    choch = next((event for event in structure_events if event.get("type") == "CHoCH"), None)

    def compatible(event: dict[str, Any] | None, fallback: str) -> dict[str, Any]:
        if not event:
            return _empty_event(fallback)
        result = dict(event)
        result["story_direction"] = event.get("direction")
        result["direction"] = str(event.get("direction", "")).lower()
        return result

    return {
        "bos": compatible(bos, "No recent completed-candle BOS was confirmed."),
        "choch": compatible(choch, "No recent completed-candle CHoCH was confirmed."),
    }


def observable_liquidity_sweep(story: dict[str, Any]) -> dict[str, Any]:
    """Compatibility view with lifecycle metadata for sweep consumers."""
    event = story.get("latest_liquidity_event") or {}
    if not event.get("detected"):
        return event or _empty_event("No recent completed-candle liquidity sweep was confirmed.")
    result = dict(event)
    result["direction"] = f"{str(event.get('direction', '')).lower()}_reversal_watch"
    result["quality"] = str(event.get("quality", "VALID")).lower()
    return result
