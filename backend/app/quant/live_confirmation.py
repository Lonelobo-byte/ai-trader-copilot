"""Shared deterministic structure and live-execution confirmation gates.

Radar and the main signal path use this module so a trade cannot be approved
by one screen while being rejected as ``LIVE CHECK FAILED`` by the other.
"""
from __future__ import annotations

from time import time
from typing import Any

from app.data_sources.binance_public import Candle, completed_candles
from app.indicators.structure import classify_market_phase, detect_bos, detect_choch


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ema(prices: list[float], period: int) -> float:
    if len(prices) < period:
        return 0.0
    value = sum(prices[:period]) / period
    alpha = 2.0 / (period + 1.0)
    for price in prices[period:]:
        value = price * alpha + value * (1.0 - alpha)
    return value


def _rsi(prices: list[float], period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    changes = [prices[index] - prices[index - 1] for index in range(1, len(prices))]
    gains = [max(change, 0.0) for change in changes[:period]]
    losses = [max(-change, 0.0) for change in changes[:period]]
    average_gain = sum(gains) / period
    average_loss = sum(losses) / period
    for change in changes[period:]:
        average_gain = (average_gain * (period - 1) + max(change, 0.0)) / period
        average_loss = (average_loss * (period - 1) + max(-change, 0.0)) / period
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + average_gain / average_loss)


def apply_live_confirmation(candidate: dict[str, Any], live: dict[str, Any]) -> None:
    """Apply the exact direction-aware Radar execution checks to a candidate."""
    direction = candidate["direction"]
    imbalance = live.get("depth_imbalance")
    taker_ratio = live.get("taker_buy_sell_ratio")
    funding = _number(live.get("funding_rate"))
    oi_change = live.get("oi_change_pct")
    spread_bps = live.get("spread_bps")

    depth_aligned = (
        imbalance is not None
        and ((direction == "BULLISH" and imbalance >= 0.02) or (direction == "BEARISH" and imbalance <= -0.02))
    )
    flow_aligned = (
        taker_ratio is not None
        and ((direction == "BULLISH" and taker_ratio >= 1.02) or (direction == "BEARISH" and taker_ratio <= 0.98))
    )
    oi_expanding = oi_change is not None and oi_change >= 0.10
    liquid = spread_bps is not None and spread_bps <= 12.0
    crowded = (direction == "BULLISH" and funding > 0.0005) or (direction == "BEARISH" and funding < -0.0005)

    live_points = sum((8 if liquid else 0, 10 if depth_aligned else 0, 10 if flow_aligned else 0, 7 if oi_expanding else 0, 5 if not crowded else 0))
    candidate["score"] = min(100, int(candidate["score"]) + live_points)
    checks = {
        "data_complete": bool(live.get("data_complete")),
        "spread_within_limit": liquid,
        "depth_aligned": depth_aligned,
        "taker_flow_aligned": flow_aligned,
        "open_interest_expanding": oi_expanding,
        "funding_not_crowded": not crowded,
    }
    risk_flags = candidate.setdefault("risk_flags", [])
    messages = {
        "data_complete": "Live depth, funding, open-interest, or taker-flow data is incomplete.",
        "spread_within_limit": "Live spread exceeds the execution-quality limit.",
        "depth_aligned": "Displayed 20-level order-book depth does not support the proposed direction.",
        "taker_flow_aligned": "Recent taker buy/sell flow does not support the proposed direction.",
        "open_interest_expanding": "Open interest is not expanding with the move; participation is unconfirmed.",
        "funding_not_crowded": "Funding is crowded in the proposed direction; squeeze/flush risk is elevated.",
    }
    risk_flags.extend(message for key, message in messages.items() if not checks[key])
    candidate["advanced_confirmation"] = {**live, "checks": checks, "live_points": live_points}
    accepted = all(checks.values()) and candidate["score"] >= 75
    candidate["review_status"] = "REVIEW_CANDIDATE" if accepted else "WATCH_ONLY"
    candidate["status"] = "LIVE_CONFIRMED_REVIEW" if accepted else "LIVE_CONFIRMATION_REJECTED"
    candidate["quality_badge"] = "LIVE CHECK PASSED" if accepted else "LIVE CHECK FAILED"


def verify_main_signal_snapshot(
    *, symbol: str, timeframe: str, side: str | None, candles: list[Candle],
    higher_candles: list[Candle], order_book: dict[str, Any], funding: dict[str, Any],
    derivatives: dict[str, Any],
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
    if not all(structure_checks.values()):
        if not structure_checks["directional_plan"]:
            risk_flags.append("No directional trade plan is available for live confirmation.")
        if not structure_checks["completed_primary_candles"] or not structure_checks["completed_higher_candles"]:
            risk_flags.append("Insufficient completed primary or higher-timeframe candles for structure confirmation.")
        return {
            "symbol": symbol, "timeframe": timeframe, "direction": direction,
            "passed": False, "status": "STRUCTURE_REJECTED", "quality_badge": "STRUCTURE CHECK FAILED",
            "structure_checks": structure_checks, "live_checks": {}, "risk_flags": risk_flags,
            "reason": risk_flags[0], "evaluation_mode": "shared_radar_live_confirmation",
        }

    closes = [_number(c.close) for c in primary]
    higher_closes = [_number(c.close) for c in higher]
    ema20, ema50 = _ema(closes, 20), _ema(closes, 50)
    htf_ema20, htf_ema50 = _ema(higher_closes, 20), _ema(higher_closes, 50)
    primary_direction = "BULLISH" if closes[-1] > ema20 > ema50 else "BEARISH" if closes[-1] < ema20 < ema50 else "NEUTRAL"
    higher_direction = "BULLISH" if higher_closes[-1] > htf_ema20 > htf_ema50 else "BEARISH" if higher_closes[-1] < htf_ema20 < htf_ema50 else "NEUTRAL"
    prior_high = max(_number(c.high) for c in primary[-21:-1])
    prior_low = min(_number(c.low) for c in primary[-21:-1])
    latest = primary[-1]
    candle_range = max(_number(latest.high) - _number(latest.low), 1e-12)
    body_ratio = abs(_number(latest.close) - _number(latest.open)) / candle_range
    close_location = ((_number(latest.close) - _number(latest.low)) / candle_range if direction == "BULLISH"
                      else (_number(latest.high) - _number(latest.close)) / candle_range)
    average_volume = sum(_number(c.quote_volume) for c in primary[-21:-1]) / 20.0
    rvol = _number(latest.quote_volume) / average_volume if average_volume > 0 else 0.0
    bos = detect_bos(primary)
    choch = detect_choch(primary)
    phase = classify_market_phase(primary)
    phase_aligned = phase in ({"MARKUP", "ACCUMULATION"} if direction == "BULLISH" else {"MARKDOWN", "DISTRIBUTION"})
    choch_opposes = bool(choch.get("detected")) and choch.get("direction") != direction.lower()
    breakout = _number(latest.close) > prior_high if direction == "BULLISH" else _number(latest.close) < prior_low
    structure_checks.update({
        "multi_timeframe_aligned": primary_direction == direction and higher_direction == direction,
        "confirmed_completed_breakout": breakout,
        "relative_volume_confirmed": rvol >= 1.5,
        "decisive_candle": body_ratio >= 0.55 and close_location >= 0.60,
        # Do not demand every SMC label: absence is unknown, while explicit
        # opposing structure is a veto. This avoids starving the system.
        "structure_not_opposed": not choch_opposes,
    })
    if not structure_checks["multi_timeframe_aligned"]:
        risk_flags.append(f"Primary/Higher trend mismatch: {primary_direction}/{higher_direction}; expected {direction}.")
    if not structure_checks["confirmed_completed_breakout"]:
        risk_flags.append("Latest completed candle has not closed through the relevant 20-candle structure level.")
    if not structure_checks["relative_volume_confirmed"]:
        risk_flags.append(f"Relative volume {rvol:.2f}x is below the 1.50x confirmation threshold.")
    if not structure_checks["decisive_candle"]:
        risk_flags.append("Latest completed breakout candle lacks decisive body/close location.")
    if not structure_checks["structure_not_opposed"]:
        risk_flags.append("A completed-candle change-of-character opposes the proposed direction.")

    bids, asks = order_book.get("bids", []) or [], order_book.get("asks", []) or []
    bid_notional = sum(_number(row[0]) * _number(row[1]) for row in bids[:20])
    ask_notional = sum(_number(row[0]) * _number(row[1]) for row in asks[:20])
    best_bid = _number(bids[0][0]) if bids else 0.0
    best_ask = _number(asks[0][0]) if asks else 0.0
    midpoint = (best_bid + best_ask) / 2.0 if best_bid and best_ask else 0.0
    oi_history = derivatives.get("oi_history", {}) or {}
    taker = derivatives.get("taker_buy_sell_volume", {}) or {}
    live = {
        "data_complete": bool(bids and asks and funding and oi_history.get("available") and taker.get("available")),
        "depth_imbalance": (bid_notional - ask_notional) / (bid_notional + ask_notional) if bid_notional + ask_notional else None,
        "spread_bps": (best_ask - best_bid) / midpoint * 10_000 if midpoint else None,
        "funding_rate": _number(funding.get("funding_rate")),
        "oi_change_pct": _number(oi_history.get("oi_change_pct")) if oi_history.get("available") else None,
        "taker_buy_sell_ratio": _number(taker.get("buy_sell_ratio")) if taker.get("available") else None,
    }
    candidate = {"symbol": symbol, "direction": direction, "score": 75 if all(structure_checks.values()) else 0, "risk_flags": risk_flags}
    apply_live_confirmation(candidate, live)
    live_checks = candidate["advanced_confirmation"]["checks"]
    passed = all(structure_checks.values()) and candidate["status"] == "LIVE_CONFIRMED_REVIEW"
    status = "LIVE_CONFIRMED_REVIEW" if passed else ("STRUCTURE_REJECTED" if not all(structure_checks.values()) else "LIVE_CONFIRMATION_REJECTED")
    return {
        "symbol": symbol, "timeframe": timeframe, "direction": direction, "passed": passed,
        "status": status, "quality_badge": "LIVE CHECK PASSED" if passed else "LIVE CHECK FAILED",
        "structure_checks": structure_checks, "live_checks": live_checks,
        "risk_flags": candidate["risk_flags"], "reason": candidate["risk_flags"][0] if candidate["risk_flags"] else "All shared Radar checks passed.",
        "metrics": {"primary_direction": primary_direction, "higher_direction": higher_direction, "rvol": round(rvol, 2), "body_ratio": round(body_ratio, 3), "phase": phase, "bos": bos, "choch": choch},
        "live_evidence": candidate["advanced_confirmation"],
        "evaluation_mode": "shared_radar_live_confirmation",
    }
