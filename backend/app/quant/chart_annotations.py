"""Chart-ready projection of the authoritative Hawk Eye analysis snapshot.

The chart never re-runs structure, liquidity, or execution logic.  It only
normalizes the exact objects already reviewed by the analysis pipeline into a
small transport contract.  WebSocket callers can send one history snapshot
and then candle deltas while keeping annotations synchronized with every live
decision update.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Sequence


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _field(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _candle_row(candle: Any) -> dict[str, Any] | None:
    open_time = _integer(_field(candle, "open_time"))
    close_time = _integer(_field(candle, "close_time"))
    open_price = _number(_field(candle, "open"))
    high = _number(_field(candle, "high"))
    low = _number(_field(candle, "low"))
    close = _number(_field(candle, "close"))
    volume = _number(_field(candle, "volume"))
    if None in {open_time, open_price, high, low, close}:
        return None
    return {
        "open_time": open_time,
        "close_time": close_time,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume or 0.0,
        "taker_buy_base_volume": _number(_field(candle, "taker_buy_base_volume")),
    }


def _event_view(event: Mapping[str, Any], candles: Sequence[Any]) -> dict[str, Any] | None:
    if not event or not event.get("detected"):
        return None
    event_time = _integer(event.get("event_open_time"))
    event_index = _integer(event.get("event_index"))
    level = _number(event.get("break_level") or event.get("swept_level"))
    if event_time is None and event_index is not None and 0 <= event_index < len(candles):
        event_time = _integer(_field(candles[event_index], "open_time"))
    if event_time is None or level is None:
        return None

    event_type = str(event.get("type") or "EVENT").upper()
    price = (
        _number(event.get("sweep_extreme"))
        if event_type == "LIQUIDITY_SWEEP"
        else _number(event.get("event_close"))
    )
    return {
        "id": str(event.get("event_id") or f"{event_type}:{event_time}:{level}"),
        "type": event_type,
        "direction": str(event.get("direction") or "NEUTRAL").upper(),
        "event_index": event_index,
        "time": event_time,
        "price": price if price is not None else level,
        "level": level,
        "state": str(event.get("state") or "DEVELOPING").upper(),
        "actionable": bool(event.get("actionable")),
        "chase_prohibited": bool(event.get("chase_prohibited")),
        "age_bars": _integer(event.get("age_bars")),
        "retest_time": (
            _integer(_field(candles[int(event["last_retest_index"])], "open_time"))
            if _integer(event.get("last_retest_index")) is not None
            and 0 <= int(event["last_retest_index"]) < len(candles)
            else None
        ),
        "invalidation_level": _number(event.get("invalidation_level")),
        "reason": str(event.get("reason") or ""),
    }


def _liquidity_levels(liquidity_map: Mapping[str, Any]) -> list[dict[str, Any]]:
    levels: list[dict[str, Any]] = []
    seen: set[tuple[str, float]] = set()
    for pool in list(liquidity_map.get("pools") or [])[:8]:
        price = _number(pool.get("price"))
        if price is None:
            continue
        kind = str(pool.get("kind") or "liquidity").upper()
        key = (kind, price)
        if key in seen:
            continue
        seen.add(key)
        levels.append({
            "kind": kind,
            "side": str(pool.get("side") or "").upper(),
            "price": price,
            "touches": _integer(pool.get("touches")) or 0,
        })
    return levels


def _execution_levels(trade_setup: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not trade_setup.get("execution_permitted"):
        return []
    levels: list[dict[str, Any]] = []
    stop = _number((trade_setup.get("stop") or {}).get("selected"))
    if stop is not None:
        levels.append({"kind": "STOP", "price": stop})
    target_labels = {
        "tp1_1r": "TP1",
        "tp2_2r": "TP2",
        "tp3_3r": "TP3",
        "runner_5r": "RUNNER",
    }
    targets = trade_setup.get("targets") or {}
    for key, label in target_labels.items():
        price = _number(targets.get(key))
        if price is not None:
            levels.append({"kind": label, "price": price})
    return levels


def _entry_zone(trade_setup: Mapping[str, Any]) -> dict[str, Any] | None:
    entry = trade_setup.get("entry") or {}
    low = _number(entry.get("zone_low"))
    high = _number(entry.get("zone_high"))
    if low is None or high is None or high < low:
        return None
    return {
        "low": low,
        "high": high,
        "reference": _number(entry.get("reference")),
        "mode": str(entry.get("mode") or "WATCH").upper(),
        "execution_permitted": bool(trade_setup.get("execution_permitted")),
    }


def _campaign_view(selected_event: Mapping[str, Any] | None, candles: Sequence[Any]) -> dict[str, Any] | None:
    if not selected_event:
        return None
    origin_index = _integer(selected_event.get("campaign_origin_index"))
    origin_time = None
    if origin_index is not None and 0 <= origin_index < len(candles):
        origin_time = _integer(_field(candles[origin_index], "open_time"))
    origin_price = _number(selected_event.get("campaign_origin_price"))
    if origin_price is None:
        return None
    return {
        "id": selected_event.get("campaign_id"),
        "direction": str(selected_event.get("direction") or "NEUTRAL").upper(),
        "origin_time": origin_time,
        "origin_price": origin_price,
        "atr": _number(selected_event.get("campaign_atr")),
        "distance_atr": _number(selected_event.get("campaign_distance_atr_current")),
        "maturity": str(selected_event.get("campaign_maturity") or "UNKNOWN").upper(),
        "entry_timing": str(selected_event.get("entry_timing") or "UNKNOWN").upper(),
    }


def build_hawk_eye_chart_contract(
    candles: Sequence[Any],
    *,
    symbol: str,
    timeframe: str,
    story: Mapping[str, Any] | None,
    liquidity_map: Mapping[str, Any] | None,
    trade_setup: Mapping[str, Any] | None,
    signal_monitor: Mapping[str, Any] | None,
    live_confirmation: Mapping[str, Any] | None,
    execution_tape: Mapping[str, Any] | None,
    mode: str = "snapshot",
) -> dict[str, Any]:
    """Return a bounded chart snapshot or candle delta plus synced overlays."""
    story = story or {}
    liquidity_map = liquidity_map or {}
    trade_setup = trade_setup or {}
    signal_monitor = signal_monitor or {}
    live_confirmation = live_confirmation or {}
    execution_tape = execution_tape or {}
    normalized_mode = mode if mode in {"snapshot", "delta", "rollover"} else "snapshot"
    transport_candles: Iterable[Any]
    if normalized_mode == "snapshot":
        transport_candles = candles[-200:]
    elif normalized_mode == "rollover":
        transport_candles = candles[-2:]
    else:
        transport_candles = candles[-1:]
    candle_rows = [row for row in (_candle_row(item) for item in transport_candles) if row]

    structure_events = [
        view
        for view in (
            _event_view(event, candles)
            for event in list(story.get("structure_events") or [])[:8]
        )
        if view
    ]
    liquidity_events = [
        view
        for view in (
            _event_view(event, candles)
            for event in list(story.get("liquidity_events") or [])[:6]
        )
        if view
    ]
    selected_event = (
        ((trade_setup.get("market_story") or {}).get("selected_event"))
        or story.get("latest_event")
        or {}
    )
    selected_view = _event_view(selected_event, candles) if selected_event else None
    selected_id = selected_view.get("id") if selected_view else None
    selected_direction = selected_view.get("direction") if selected_view else None
    for event_view in (*structure_events, *liquidity_events):
        is_selected = bool(selected_id and event_view.get("id") == selected_id)
        event_view["selected"] = is_selected
        if is_selected:
            event_view["branch_status"] = "SELECTED"
            event_view["display_state"] = event_view.get("state")
            event_view["display_reason"] = event_view.get("reason")
        else:
            event_view["branch_status"] = (
                "OPPOSING_CONTEXT"
                if selected_direction and event_view.get("direction") != selected_direction
                else "HISTORICAL_CONTEXT"
            )
            event_view["display_state"] = "CONTEXT_ONLY"
            event_view["display_reason"] = (
                "Historical completed-candle context only; this is not the currently selected signal branch."
            )
    if selected_view:
        selected_view.update({
            "selected": True,
            "branch_status": "SELECTED",
            "display_state": selected_view.get("state"),
            "display_reason": selected_view.get("reason"),
        })
    actual_flow = execution_tape.get("actual_flow") or {}
    actionability = story.get("actionability") or {}

    return {
        "schema_version": "hawk_eye_chart.v1",
        "mode": normalized_mode,
        "symbol": symbol,
        "timeframe": timeframe,
        "source": "BINANCE_PERPETUAL",
        "candle_limit": 200,
        "candles": candle_rows,
        "latest_open_time": candle_rows[-1]["open_time"] if candle_rows else None,
        "annotations": {
            "structure_events": structure_events,
            "liquidity_events": liquidity_events,
            "selected_event": selected_view,
            "campaign": _campaign_view(selected_event, candles),
            "liquidity_levels": _liquidity_levels(liquidity_map),
            "entry_zone": _entry_zone(trade_setup),
            "execution_levels": _execution_levels(trade_setup),
        },
        "decision": {
            "action": str(signal_monitor.get("action") or "WATCH").upper(),
            "status": str(signal_monitor.get("status") or "MONITORING").upper(),
            "side": str(signal_monitor.get("side") or trade_setup.get("side") or "NEUTRAL").upper(),
            "reason": str(signal_monitor.get("reason") or trade_setup.get("reason") or ""),
            "story_state": str(actionability.get("status") or story.get("current_state") or "NO_ACTIVE_EVENT").upper(),
            "entry_timing": str(actionability.get("entry_timing") or "UNKNOWN").upper(),
            "campaign_maturity": str(actionability.get("campaign_maturity") or "UNKNOWN").upper(),
            "execution_permitted": bool(trade_setup.get("execution_permitted")),
            "live_confirmation_passed": live_confirmation.get("passed") is True,
            "live_confirmation_reason": str(live_confirmation.get("reason") or ""),
            "flow": {
                "available": bool(actual_flow.get("available")),
                "status": str(actual_flow.get("status") or "UNAVAILABLE").upper(),
                "bias": str(actual_flow.get("bias") or "UNAVAILABLE").upper(),
                "active_aggressor": str(actual_flow.get("active_aggressor") or "UNAVAILABLE").upper(),
                "confidence": str(actual_flow.get("confidence") or "UNAVAILABLE").upper(),
                "buy_notional": _number(actual_flow.get("buy_notional")),
                "sell_notional": _number(actual_flow.get("sell_notional")),
                "net_delta_usd": _number(actual_flow.get("net_delta_usd")),
                "cvd_trend": str(actual_flow.get("cvd_trend") or "UNAVAILABLE").upper(),
                "price_response": str(actual_flow.get("price_response") or "UNAVAILABLE").upper(),
                "cross_market_alignment": str(actual_flow.get("cross_market_alignment") or "UNAVAILABLE").upper(),
            },
        },
    }
