"""Market structure detection — BOS, CHoCH, Order Blocks, and FVGs."""
from __future__ import annotations

from typing import Any

from app.data_sources.binance_public import Candle, completed_candles
from app.indicators.trend import analyze_trend


def classify_market_phase(candles: list[Candle]) -> str:
    """Classify observable price phase without claiming formal Wyckoff proof.

    The phase is an evidence label (accumulation, markup, distribution,
    markdown, or ranging), not participant attribution. It is safe to use as
    a bounded confluence input alongside measured trend and order flow.
    """
    closed = completed_candles(candles)
    recent = closed[-30:]
    if len(recent) < 20 or recent[0].close <= 0:
        return "RANGING"

    closes = [c.close for c in recent]
    change_pct = (closes[-1] - closes[0]) / closes[0] * 100.0
    total_move = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    efficiency = abs(closes[-1] - closes[0]) / total_move if total_move else 0.0
    recent_slope = closes[-1] - closes[-5]

    if change_pct >= 1.0 and recent_slope > 0 and efficiency >= 0.35:
        return "MARKUP"
    if change_pct <= -1.0 and recent_slope < 0 and efficiency >= 0.35:
        return "MARKDOWN"

    flow_numerator = 0.0
    flow_denominator = 0.0
    for candle in recent[-20:]:
        candle_range = candle.high - candle.low
        if candle_range > 0:
            close_location = ((candle.close - candle.low) - (candle.high - candle.close)) / candle_range
            flow_numerator += close_location * candle.volume
        flow_denominator += candle.volume
    flow = flow_numerator / flow_denominator if flow_denominator else 0.0
    if flow >= 0.12:
        return "ACCUMULATION"
    if flow <= -0.12:
        return "DISTRIBUTION"
    return "RANGING"


def find_swing_points(candles: list[Candle], N: int = 3) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Scan historical candles to find structural Swing Highs and Swing Lows.

    Parameters
    ----------
    candles : list[Candle]
        The candle data.
    N : int
        The number of candles on each side to define a swing extreme.
    """
    closed = completed_candles(candles)
    swing_highs = []
    swing_lows = []
    
    for i in range(N, len(closed) - N):
        highs = [c.high for c in closed[i - N : i + N + 1]]
        lows = [c.low for c in closed[i - N : i + N + 1]]
        
        if closed[i].high == max(highs):
            if highs.count(closed[i].high) == 1:
                swing_highs.append({
                    "price": closed[i].high,
                    "index": i,
                    "time": closed[i].open_time
                })
        
        if closed[i].low == min(lows):
            if lows.count(closed[i].low) == 1:
                swing_lows.append({
                    "price": closed[i].low,
                    "index": i,
                    "time": closed[i].open_time
                })
                
    return swing_highs, swing_lows


def detect_bos(candles: list[Candle]) -> dict[str, Any]:
    """Detect Break of Structure (BOS) in the direction of the dominant trend."""
    closed = completed_candles(candles)
    if len(closed) < 20:
        return {"detected": False, "direction": "none", "reason": "need_more_candles"}

    swing_highs, swing_lows = find_swing_points(closed, N=3)
    if not swing_highs or not swing_lows:
        return {"detected": False, "direction": "none", "reason": "no_swing_points_found"}

    last_high = swing_highs[-1]["price"]
    last_low = swing_lows[-1]["price"]
    current_close = closed[-1].close
    
    trend = analyze_trend(closed)
    is_bullish = trend.get("status") == "bullish"
    is_bearish = trend.get("status") == "bearish"

    if is_bullish and current_close > last_high:
        return {
            "detected": True,
            "direction": "bullish",
            "broken_level": last_high,
            "current_close": current_close,
            "type": "BOS",
        }
    elif is_bearish and current_close < last_low:
        return {
            "detected": True,
            "direction": "bearish",
            "broken_level": last_low,
            "current_close": current_close,
            "type": "BOS",
        }

    return {"detected": False, "direction": "none", "reason": "no_structure_break"}


def detect_choch(candles: list[Candle]) -> dict[str, Any]:
    """Detect Change of Character (CHoCH) signifying trend structure reversals."""
    closed = completed_candles(candles)
    if len(closed) < 20:
        return {"detected": False, "direction": "none", "reason": "need_more_candles"}

    swing_highs, swing_lows = find_swing_points(closed, N=3)
    if not swing_highs or not swing_lows:
        return {"detected": False, "direction": "none", "reason": "no_swing_points_found"}

    last_high = swing_highs[-1]["price"]
    last_low = swing_lows[-1]["price"]
    current_close = closed[-1].close
    
    trend = analyze_trend(closed)
    is_bullish = trend.get("status") == "bullish"
    is_bearish = trend.get("status") == "bearish"

    if is_bullish and current_close < last_low:
        return {
            "detected": True,
            "direction": "bearish",
            "broken_level": last_low,
            "current_close": current_close,
            "type": "CHoCH",
        }
    elif is_bearish and current_close > last_high:
        return {
            "detected": True,
            "direction": "bullish",
            "broken_level": last_high,
            "current_close": current_close,
            "type": "CHoCH",
        }

    return {"detected": False, "direction": "none", "reason": "no_trend_reversal_detected"}


def find_order_blocks(candles: list[Candle]) -> list[dict[str, Any]]:
    """Identify supply/demand order blocks matching breakout imbalances."""
    closed = completed_candles(candles)
    blocks = []
    if len(closed) < 10:
        return blocks

    gaps = find_fair_value_gaps(closed)
    for gap in gaps:
        idx = gap["candle_index"]
        if idx <= 1:
            continue

        if gap["type"] == "bullish":
            ob_candle = None
            for k in range(idx - 1, max(0, idx - 5), -1):
                if closed[k].close < closed[k].open:  # Bearish candle
                    ob_candle = closed[k]
                    break
            if ob_candle:
                blocks.append({
                    "type": "bullish_demand",
                    "high": round(ob_candle.high, 4),
                    "low": round(ob_candle.low, 4),
                    "open_time": ob_candle.open_time,
                })
        elif gap["type"] == "bearish":
            ob_candle = None
            for k in range(idx - 1, max(0, idx - 5), -1):
                if closed[k].close > closed[k].open:  # Bullish candle
                    ob_candle = closed[k]
                    break
            if ob_candle:
                blocks.append({
                    "type": "bearish_supply",
                    "high": round(ob_candle.high, 4),
                    "low": round(ob_candle.low, 4),
                    "open_time": ob_candle.open_time,
                })

    # Deduplicate order blocks
    unique_blocks = []
    seen = set()
    for b in blocks:
        key = (b["type"], b["high"], b["low"])
        if key not in seen:
            seen.add(key)
            unique_blocks.append(b)
            
    return unique_blocks


def find_fair_value_gaps(candles: list[Candle]) -> list[dict[str, Any]]:
    """Identify active (unmitigated) Fair Value Gaps (FVG)."""
    closed = completed_candles(candles)
    gaps = []
    if len(closed) < 3:
        return gaps

    start = max(0, len(closed) - 50)
    for i in range(start, len(closed) - 2):
        c1, c2, c3 = closed[i], closed[i + 1], closed[i + 2]

        if c3.low > c1.high:
            # Check for mitigation by subsequent prices
            mitigated = False
            gap_low = c1.high
            gap_high = c3.low
            for j in range(i + 3, len(closed)):
                if closed[j].low <= gap_low:
                    mitigated = True
                    break
            if not mitigated:
                gaps.append({
                    "type": "bullish",
                    "low": round(gap_low, 4),
                    "high": round(gap_high, 4),
                    "size_pct": round(((gap_high - gap_low) / gap_low) * 100.0, 3),
                    "candle_index": i + 1,
                })

        elif c3.high < c1.low:
            mitigated = False
            gap_low = c3.high
            gap_high = c1.low
            for j in range(i + 3, len(closed)):
                if closed[j].high >= gap_high:
                    mitigated = True
                    break
            if not mitigated:
                gaps.append({
                    "type": "bearish",
                    "low": round(gap_low, 4),
                    "high": round(gap_high, 4),
                    "size_pct": round(((gap_high - gap_low) / gap_low) * 100.0, 3),
                    "candle_index": i + 1,
                })

    return gaps
