"""Causal market-context features and a transparent setup scoring contract.

This module deliberately does not use RSI, MACD, moving averages, or other
price transforms to choose a direction.  It turns observable market context
into bounded evidence: where liquidity sits, whether positioning is building
or unwinding, whether aggressive flow agrees, and whether volatility/macro
conditions permit a trade.  Missing evidence remains unavailable.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import math
from typing import Any

from app.data_sources.binance_public import Candle, completed_candles
from app.indicators.structure import find_swing_points


def _number(value: Any, default: float = 0.0) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _direction(value: float, threshold: float = 0.0) -> str:
    if value > threshold:
        return "BULLISH"
    if value < -threshold:
        return "BEARISH"
    return "NEUTRAL"


def build_liquidity_map(candles: list[Candle], atr: float) -> dict[str, Any]:
    """Map observable resting-liquidity references from completed candles.

    Equal levels are tolerance clusters of confirmed swing points.  They are
    not claims about participant intent or the exact quantity of resting
    orders.  Day/week levels use UTC candle timestamps for deterministic,
    venue-independent behaviour.
    """
    closed = completed_candles(candles)
    if len(closed) < 20:
        return {"available": False, "reason": "need_at_least_20_completed_candles", "pools": []}

    price = _number(closed[-1].close)
    tolerance = max(abs(_number(atr)) * 0.25, price * 0.0005)
    swing_highs, swing_lows = find_swing_points(closed, N=3)

    def clusters(points: list[dict[str, Any]], side: str) -> list[dict[str, Any]]:
        grouped: list[list[dict[str, Any]]] = []
        for point in sorted(points, key=lambda item: _number(item.get("price"))):
            for group in grouped:
                centre = sum(_number(item.get("price")) for item in group) / len(group)
                if abs(_number(point.get("price")) - centre) <= tolerance:
                    group.append(point)
                    break
            else:
                grouped.append([point])
        return [
            {
                "kind": "equal_highs" if side == "above" else "equal_lows",
                "side": side,
                "price": round(sum(_number(item.get("price")) for item in group) / len(group), 8),
                "touches": len(group),
                "distance_pct": round(abs((sum(_number(item.get("price")) for item in group) / len(group) - price) / price * 100.0), 4) if price else None,
            }
            for group in grouped if len(group) >= 2
        ]

    pools = clusters(swing_highs, "above") + clusters(swing_lows, "below")
    last_day = datetime.fromtimestamp(_number(closed[-1].open_time) / 1000.0, tz=timezone.utc).date()
    prior_day = [c for c in closed if datetime.fromtimestamp(_number(c.open_time) / 1000.0, tz=timezone.utc).date() < last_day]
    if prior_day:
        day = prior_day[-min(len(prior_day), 96):]
        pools.extend([
            {"kind": "previous_day_high", "side": "above" if max(c.high for c in day) >= price else "below", "price": round(max(c.high for c in day), 8), "touches": 1},
            {"kind": "previous_day_low", "side": "below" if min(c.low for c in day) <= price else "above", "price": round(min(c.low for c in day), 8), "touches": 1},
        ])

    last_week = datetime.fromtimestamp(_number(closed[-1].open_time) / 1000.0, tz=timezone.utc).isocalendar()[:2]
    prior_week = [c for c in closed if datetime.fromtimestamp(_number(c.open_time) / 1000.0, tz=timezone.utc).isocalendar()[:2] < last_week]
    if prior_week:
        pools.extend([
            {"kind": "previous_week_high", "side": "above" if max(c.high for c in prior_week) >= price else "below", "price": round(max(c.high for c in prior_week), 8), "touches": 1},
            {"kind": "previous_week_low", "side": "below" if min(c.low for c in prior_week) <= price else "above", "price": round(min(c.low for c in prior_week), 8), "touches": 1},
        ])

    pools.sort(key=lambda item: abs(_number(item.get("price")) - price))
    return {
        "available": True,
        "reference_price": round(price, 8),
        "equal_level_tolerance": round(tolerance, 8),
        "pools": pools[:12],
        "nearest_above": next((pool for pool in pools if _number(pool.get("price")) > price), None),
        "nearest_below": next((pool for pool in pools if _number(pool.get("price")) < price), None),
        "limitations": "Candle-derived liquidity references estimate likely stop/interest areas; they do not reveal resting order quantities.",
    }


def classify_positioning(candles: list[Candle], derivatives: dict[str, Any]) -> dict[str, Any]:
    """Classify the price/OI relationship before interpreting funding."""
    closed = completed_candles(candles)
    history = derivatives.get("oi_history", {}) or {}
    if len(closed) < 6 or not history.get("available"):
        return {"available": False, "state": "UNKNOWN", "reason": "price_history_or_oi_history_unavailable"}
    reference = _number(closed[-6].close)
    price_change_pct = ((_number(closed[-1].close) - reference) / reference * 100.0) if reference else 0.0
    oi_change_pct = _number(history.get("oi_change_pct"))
    if price_change_pct > 0.05 and oi_change_pct > 0.10:
        state, bias = "BUILDING_LONGS", "BULLISH"
    elif price_change_pct < -0.05 and oi_change_pct > 0.10:
        state, bias = "BUILDING_SHORTS", "BEARISH"
    elif price_change_pct > 0.05 and oi_change_pct < -0.10:
        state, bias = "SHORT_COVERING", "BULLISH"
    elif price_change_pct < -0.05 and oi_change_pct < -0.10:
        state, bias = "LONG_LIQUIDATION", "BEARISH"
    else:
        state, bias = "STABLE_OR_MIXED", "NEUTRAL"

    funding = _number(derivatives.get("funding_rate"))
    crowded = "LONGS_CROWDED" if funding > 0.0005 else "SHORTS_CROWDED" if funding < -0.0005 else "NEUTRAL"
    taker = derivatives.get("taker_volume", {}) or {}
    cvd = str(taker.get("cvd_trend", "CVD_NEUTRAL"))
    delta_bias = "BULLISH" if "BULLISH" in cvd else "BEARISH" if "BEARISH" in cvd else "NEUTRAL"
    divergence = (
        "BEARISH_DELTA_DIVERGENCE" if price_change_pct > 0.05 and delta_bias == "BEARISH"
        else "BULLISH_DELTA_DIVERGENCE" if price_change_pct < -0.05 and delta_bias == "BULLISH"
        else "NONE"
    )
    return {
        "available": True,
        "state": state,
        "bias": bias,
        "price_change_pct": round(price_change_pct, 4),
        "oi_change_pct": round(oi_change_pct, 4),
        "funding_rate": funding,
        "crowding": crowded,
        "delta_bias": delta_bias,
        "delta_divergence": divergence,
        "taker_aggression": taker.get("aggression", "NEUTRAL"),
    }


def build_volatility_context(candles: list[Candle]) -> dict[str, Any]:
    closed = completed_candles(candles)
    if len(closed) < 31:
        return {"available": False, "reason": "need_at_least_31_completed_candles"}
    returns = [(_number(closed[i].close) / _number(closed[i - 1].close) - 1.0) for i in range(1, len(closed)) if _number(closed[i - 1].close) > 0]
    short = returns[-10:]
    long = returns[-30:]
    short_vol = math.sqrt(sum(value * value for value in short) / len(short)) if short else 0.0
    long_vol = math.sqrt(sum(value * value for value in long) / len(long)) if long else 0.0
    ratio = short_vol / long_vol if long_vol else 1.0
    state = "EXPANSION" if ratio >= 1.35 else "COMPRESSION" if ratio <= 0.70 else "NORMAL"
    return {"available": True, "state": state, "realized_volatility_10": round(short_vol * 100.0, 4), "realized_volatility_30": round(long_vol * 100.0, 4), "short_to_long_ratio": round(ratio, 4)}


def build_volume_profile(candles: list[Candle], bins: int = 24) -> dict[str, Any]:
    """Build a clearly-labelled candle-volume profile, not an exchange VAP feed."""
    closed = completed_candles(candles)[-120:]
    if len(closed) < 20:
        return {"available": False, "reason": "need_at_least_20_completed_candles"}
    low, high = min(_number(c.low) for c in closed), max(_number(c.high) for c in closed)
    if high <= low:
        return {"available": False, "reason": "zero_price_range"}
    width = (high - low) / bins
    volume_by_bin: dict[int, float] = defaultdict(float)
    for candle in closed:
        typical = (_number(candle.high) + _number(candle.low) + _number(candle.close)) / 3.0
        index = min(bins - 1, max(0, int((typical - low) / width)))
        volume_by_bin[index] += max(_number(candle.quote_volume), _number(candle.volume) * typical)
    ranked = sorted(volume_by_bin, key=volume_by_bin.get, reverse=True)
    poc_index = ranked[0]
    def level(index: int) -> float:
        return low + (index + 0.5) * width
    max_volume = volume_by_bin[poc_index]
    hvn = [round(level(index), 8) for index in ranked if volume_by_bin[index] >= max_volume * 0.65][:3]
    nonzero = [value for value in volume_by_bin.values() if value > 0]
    min_threshold = min(nonzero) * 1.25 if nonzero else 0.0
    lvn = [round(level(index), 8) for index in sorted(volume_by_bin, key=volume_by_bin.get) if 0 < volume_by_bin[index] <= min_threshold][:3]
    price = _number(closed[-1].close)
    location = "ABOVE_POC_ACCEPTANCE" if price > level(poc_index) + width * 0.25 else "BELOW_POC_ACCEPTANCE" if price < level(poc_index) - width * 0.25 else "AT_POC"
    return {
        "available": True,
        "poc": round(level(poc_index), 8),
        "high_volume_nodes": hvn,
        "low_volume_nodes": lvn,
        "location": location,
        "lookback_candles": len(closed),
        "limitations": "Profile distributes each candle's reported volume at its typical price; it is an approximation, not trade-at-price exchange data.",
    }


def build_vwap_context(candles: list[Candle]) -> dict[str, Any]:
    """Return daily, weekly, and swing-anchored VWAP references."""
    closed = completed_candles(candles)
    if len(closed) < 5:
        return {"available": False, "reason": "need_at_least_5_completed_candles"}

    def vwap(rows: list[Candle]) -> float:
        volume = sum(max(_number(row.volume), 0.0) for row in rows)
        return sum(((_number(row.high) + _number(row.low) + _number(row.close)) / 3.0) * max(_number(row.volume), 0.0) for row in rows) / volume if volume else _number(rows[-1].close)

    last_time = datetime.fromtimestamp(_number(closed[-1].open_time) / 1000.0, tz=timezone.utc)
    daily = [row for row in closed if datetime.fromtimestamp(_number(row.open_time) / 1000.0, tz=timezone.utc).date() == last_time.date()]
    weekly = [row for row in closed if datetime.fromtimestamp(_number(row.open_time) / 1000.0, tz=timezone.utc).isocalendar()[:2] == last_time.isocalendar()[:2]]
    swings_high, swings_low = find_swing_points(closed, N=3)
    anchors = swings_high + swings_low
    anchor_index = max((_number(point.get("index")) for point in anchors), default=max(0, len(closed) - 20))
    anchored = closed[int(anchor_index):]
    price = _number(closed[-1].close)
    daily_vwap, weekly_vwap, anchored_vwap = vwap(daily), vwap(weekly), vwap(anchored)
    above = sum(price > value for value in (daily_vwap, weekly_vwap, anchored_vwap))
    below = sum(price < value for value in (daily_vwap, weekly_vwap, anchored_vwap))
    return {
        "available": True,
        "daily": round(daily_vwap, 8),
        "weekly": round(weekly_vwap, 8),
        "anchored": round(anchored_vwap, 8),
        "anchor_candle_index": int(anchor_index),
        "price_relation": "ABOVE_ALL" if above == 3 else "BELOW_ALL" if below == 3 else "MIXED",
        "limitations": "VWAP uses reported candle volume and UTC session boundaries; it is not a venue-specific execution benchmark.",
    }


def score_market_context(features: dict[str, Any]) -> dict[str, Any]:
    """Produce an auditable directional score from causal evidence domains."""
    structure = features.get("market_structure", {}) or {}
    positioning = features.get("positioning", {}) or {}
    liquidity = features.get("liquidity_map", {}) or {}
    micro = features.get("microstructure", {}) or {}
    trade_flow = features.get("trade_flow", {}) or {}
    volatility = features.get("volatility_context", {}) or {}
    profile = features.get("volume_profile", {}) or {}
    vwap = features.get("vwap_context", {}) or {}
    cross = features.get("cross_asset", {}) or {}
    components: dict[str, dict[str, Any]] = {}

    phase = str(structure.get("phase", "RANGING"))
    bos = structure.get("bos", {}) or {}
    structure_score = (1.0 if phase in {"MARKUP", "ACCUMULATION"} else -1.0 if phase in {"MARKDOWN", "DISTRIBUTION"} else 0.0)
    if bos.get("detected"):
        structure_score += 0.5 if bos.get("direction") == "bullish" else -0.5 if bos.get("direction") == "bearish" else 0.0
    components["regime_structure"] = {"available": bool(structure), "score": structure_score, "weight": 0.20, "bias": _direction(structure_score, 0.2), "evidence": phase}

    sweep = features.get("sweep", {}) or {}
    sweep_direction = str(sweep.get("direction", ""))
    liquidity_score = 0.6 if sweep_direction.startswith("bullish") else -0.6 if sweep_direction.startswith("bearish") else 0.0
    components["liquidity"] = {"available": bool(liquidity.get("available")), "score": liquidity_score, "weight": 0.17, "bias": _direction(liquidity_score, 0.1), "evidence": sweep_direction or "unswept_or_unconfirmed"}

    positioning_score = 1.0 if positioning.get("bias") == "BULLISH" else -1.0 if positioning.get("bias") == "BEARISH" else 0.0
    if positioning.get("crowding") == "LONGS_CROWDED": positioning_score -= 0.45
    if positioning.get("crowding") == "SHORTS_CROWDED": positioning_score += 0.45
    if positioning.get("delta_divergence") == "BEARISH_DELTA_DIVERGENCE": positioning_score -= 0.7
    if positioning.get("delta_divergence") == "BULLISH_DELTA_DIVERGENCE": positioning_score += 0.7
    components["positioning"] = {"available": bool(positioning.get("available")), "score": positioning_score, "weight": 0.23, "bias": _direction(positioning_score, 0.15), "evidence": positioning.get("state", "UNKNOWN")}

    depth = _number(micro.get("depth_imbalance"))
    buy_ratio = _number(trade_flow.get("buy_ratio"), 0.5)
    flow_score = 0.55 * depth + 0.45 * ((buy_ratio - 0.5) * 2.0)
    components["order_flow"] = {"available": bool(micro.get("available")), "score": flow_score, "weight": 0.23, "bias": _direction(flow_score, 0.08), "evidence": {"depth_imbalance": depth, "buy_ratio": buy_ratio}}

    vol_state = volatility.get("state", "UNKNOWN")
    components["volatility"] = {"available": bool(volatility.get("available")), "score": 0.15 if vol_state == "EXPANSION" else 0.05 if vol_state == "COMPRESSION" else 0.0, "weight": 0.04, "bias": "NEUTRAL", "evidence": vol_state}
    profile_location = profile.get("location", "UNKNOWN")
    profile_score = 0.35 if profile_location == "ABOVE_POC_ACCEPTANCE" else -0.35 if profile_location == "BELOW_POC_ACCEPTANCE" else 0.0
    components["volume_profile"] = {"available": bool(profile.get("available")), "score": profile_score, "weight": 0.07, "bias": _direction(profile_score, 0.1), "evidence": profile_location}
    vwap_relation = vwap.get("price_relation", "UNKNOWN")
    vwap_score = 0.25 if vwap_relation == "ABOVE_ALL" else -0.25 if vwap_relation == "BELOW_ALL" else 0.0
    components["vwap"] = {"available": bool(vwap.get("available")), "score": vwap_score, "weight": 0.04, "bias": _direction(vwap_score, 0.1), "evidence": vwap_relation}
    risk_env = cross.get("risk_environment", "NEUTRAL")
    macro_score = 0.35 if risk_env == "RISK_ON" else -0.35 if risk_env == "RISK_OFF" else 0.0
    components["cross_market"] = {"available": bool(cross), "score": macro_score, "weight": 0.04, "bias": _direction(macro_score, 0.1), "evidence": risk_env}

    available = [component for component in components.values() if component["available"]]
    weighted = sum(component["score"] * component["weight"] for component in available)
    normalizer = sum(component["weight"] for component in available) or 1.0
    normalized = weighted / normalizer
    direction = "LONG" if normalized >= 0.16 else "SHORT" if normalized <= -0.16 else "WAIT"
    score = min(100.0, round(50.0 + abs(normalized) * 50.0 + min(len(available), 6) * 3.0, 2))
    contradictions = [name for name, component in components.items() if component["available"] and ((direction == "LONG" and component["bias"] == "BEARISH") or (direction == "SHORT" and component["bias"] == "BULLISH"))]
    return {
        "method": "causal_market_context_v1",
        "direction": direction,
        "score": score if direction != "WAIT" else 0.0,
        "normalized_directional_score": round(normalized, 4),
        "coverage": {"available_domains": len(available), "required_domains": 4, "complete": len(available) >= 4},
        "components": components,
        "contradictions": contradictions,
        "status": "SETUP_CANDIDATE" if direction != "WAIT" and len(available) >= 4 and not contradictions else "WAIT",
        "limitations": ["Displayed order-book depth remains a snapshot until incremental depth history is captured.", "Liquidation levels are not treated as observed liquidation events without a dedicated event feed."],
    }
