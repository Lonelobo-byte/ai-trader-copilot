"""Shared deterministic structure and live-execution confirmation gates.

Radar and the main signal path use this module so a trade cannot be approved
by one screen while being rejected as ``LIVE CHECK FAILED`` by the other.
"""
from __future__ import annotations

from time import time
from typing import Any

from app.data_sources.binance_public import Candle, completed_candles
from app.indicators.liquidity import detect_liquidity_sweep
from app.indicators.structure import classify_market_phase, find_swing_points
from app.quant.market_context import build_volume_profile, build_vwap_context


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


def _observable_structure_events(candles: list[Candle]) -> dict[str, dict[str, Any]]:
    """Read completed swing breaks without an indicator-derived trend gate."""
    highs, lows = find_swing_points(candles, N=3)
    if not highs or not lows:
        unavailable = {"detected": False, "direction": "none", "reason": "insufficient_confirmed_swing_points"}
        return {"bos": unavailable, "choch": unavailable.copy()}
    close = _number(candles[-1].close)
    if close > _number(highs[-1]["price"]):
        bos = {"detected": True, "direction": "bullish", "broken_level": highs[-1]["price"], "current_close": close, "type": "BOS"}
    elif close < _number(lows[-1]["price"]):
        bos = {"detected": True, "direction": "bearish", "broken_level": lows[-1]["price"], "current_close": close, "type": "BOS"}
    else:
        bos = {"detected": False, "direction": "none", "reason": "no_completed_swing_break"}
    # A CHoCH needs a confirmed historical swing sequence.  Do not invent one
    # from a moving-average trend label; an explicit opposite BOS is the veto.
    return {"bos": bos, "choch": {"detected": False, "direction": "none", "reason": "requires_historical_structure_sequence"}}


def apply_live_confirmation(candidate: dict[str, Any], live: dict[str, Any]) -> None:
    """Apply the exact direction-aware Radar execution checks to a candidate."""
    direction = candidate["direction"]
    imbalance = live.get("depth_imbalance")
    taker_ratio = live.get("taker_buy_sell_ratio")
    funding = _number(live.get("funding_rate"))
    oi_change = live.get("oi_change_pct")
    spread_bps = live.get("spread_bps")
    multi_venue = live.get("multi_venue") if isinstance(live.get("multi_venue"), dict) else {}
    cross_venue_confirmed = bool(multi_venue.get("flow_confirmed"))
    cross_venue_consensus = str(multi_venue.get("flow_consensus", "UNAVAILABLE")).upper()
    opposite_direction = "BEARISH" if direction == "BULLISH" else "BULLISH"
    cross_venue_opposed = cross_venue_confirmed and cross_venue_consensus == opposite_direction
    quote_stability = multi_venue.get("displayed_liquidity_stability", {}) or {}
    quote_stability_status = str(quote_stability.get("status", "UNAVAILABLE")).upper()
    displayed_liquidity_stable = not bool(quote_stability.get("publication_veto"))
    cross_venue_status = (
        "UNAVAILABLE"
        if not cross_venue_confirmed
        else "OPPOSED"
        if cross_venue_opposed
        else "SUPPORTIVE"
        if cross_venue_consensus == direction
        else cross_venue_consensus
    )

    depth_aligned = (
        imbalance is not None
        and ((direction == "BULLISH" and imbalance >= 0.02) or (direction == "BEARISH" and imbalance <= -0.02))
    )
    flow_aligned = (
        taker_ratio is not None
        and ((direction == "BULLISH" and taker_ratio >= 1.02) or (direction == "BEARISH" and taker_ratio <= 0.98))
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
        "cross_venue_not_opposed": not cross_venue_opposed,
        "displayed_liquidity_stable": displayed_liquidity_stable,
        "execution_capacity_sufficient": execution_capacity_sufficient,
    }
    risk_flags = candidate.setdefault("risk_flags", [])
    messages = {
        "data_complete": "Live depth, funding, open-interest, or taker-flow data is incomplete.",
        "spread_within_limit": "Live spread exceeds the execution-quality limit.",
        "depth_aligned": "Displayed 20-level order-book depth does not support the proposed direction (a snapshot only; not used as a standalone veto).",
        "taker_flow_aligned": "Recent taker buy/sell flow does not support the proposed direction.",
        "price_oi_aligned": "Price and open interest do not form an aligned positioning regime.",
        "funding_not_crowded": "Funding is crowded in the proposed direction; squeeze/flush risk is elevated.",
        "execution_evidence_confirmed": "Aggressive flow and price/OI positioning are not jointly aligned.",
        "cross_venue_not_opposed": "Healthy Bybit/Coinbase aggressive flow is aligned against the proposed direction.",
        "displayed_liquidity_stable": "Incremental Bybit and Coinbase books both show elevated displayed-liquidity instability.",
        "execution_capacity_sufficient": "Planned notional exceeds 10% of the displayed 20-level opposing-side depth.",
    }
    required_names = (
        "data_complete", "spread_within_limit", "taker_flow_aligned",
        "price_oi_aligned", "funding_not_crowded",
        "execution_evidence_confirmed", "cross_venue_not_opposed",
        "displayed_liquidity_stable", "execution_capacity_sufficient",
    )
    required_checks = {key: checks[key] for key in required_names}
    risk_flags.extend(message for key, message in messages.items() if key in required_checks and not checks[key])
    supporting_warnings = [messages["depth_aligned"]] if not depth_aligned else []
    if cross_venue_status == "UNAVAILABLE":
        supporting_warnings.append("Cross-venue public flow is partial or unavailable; it was not counted as neutral confirmation.")
    elif cross_venue_status in {"MIXED", "NEUTRAL"}:
        supporting_warnings.append(f"Cross-venue public flow is {cross_venue_status.lower()}, so it adds no directional confirmation.")
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
        "execution_capacity": {
            "evaluated": execution_capacity_evaluated,
            "planned_notional_usd": round(planned_notional, 2),
            "opposing_depth_notional": round(opposing_depth, 2),
            "maximum_notional_at_10pct_depth": round(opposing_depth * 0.10, 2),
        },
        "displayed_liquidity_stability": quote_stability,
        "cross_venue_evidence": {
            "status": cross_venue_status,
            "confirmed": cross_venue_confirmed,
            "consensus": cross_venue_consensus,
            "flow_score": multi_venue.get("flow_score"),
            "fresh_venue_count": multi_venue.get("fresh_venue_count", 0),
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
            "scenarios": {
                "institutional": {"passed": False, "status": "UNAVAILABLE", "reason": risk_flags[0]},
                "tactical": {"passed": False, "candidate": False, "status": "UNAVAILABLE", "reason": risk_flags[0]},
            },
        }

    primary_phase = classify_market_phase(primary)
    higher_phase = classify_market_phase(higher) if structure_checks["completed_higher_candles"] else "UNAVAILABLE"
    phase_direction = {"MARKUP": "BULLISH", "ACCUMULATION": "BULLISH", "MARKDOWN": "BEARISH", "DISTRIBUTION": "BEARISH"}
    primary_direction = phase_direction.get(primary_phase, "NEUTRAL")
    higher_direction = phase_direction.get(higher_phase, "NEUTRAL")
    prior_high = max(_number(c.high) for c in primary[-21:-1])
    prior_low = min(_number(c.low) for c in primary[-21:-1])
    latest = primary[-1]
    candle_range = max(_number(latest.high) - _number(latest.low), 1e-12)
    body_ratio = abs(_number(latest.close) - _number(latest.open)) / candle_range
    close_location = ((_number(latest.close) - _number(latest.low)) / candle_range if direction == "BULLISH"
                      else (_number(latest.high) - _number(latest.close)) / candle_range)
    average_volume = sum(_number(c.quote_volume) for c in primary[-21:-1]) / 20.0
    rvol = _number(latest.quote_volume) / average_volume if average_volume > 0 else 0.0
    events = _observable_structure_events(primary)
    bos, choch = events["bos"], events["choch"]
    choch_opposes = bool(choch.get("detected")) and choch.get("direction") != direction.lower()
    breakout = _number(latest.close) > prior_high if direction == "BULLISH" else _number(latest.close) < prior_low
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
    sweep = detect_liquidity_sweep(primary)
    vwap = build_vwap_context(primary)
    profile = build_volume_profile(primary)
    sweep_aligned = bool(sweep.get("detected")) and str(sweep.get("direction", "")).startswith(direction.lower())
    expected_vwap = "ABOVE_ALL" if direction == "BULLISH" else "BELOW_ALL"
    expected_profile = "ABOVE_POC_ACCEPTANCE" if direction == "BULLISH" else "BELOW_POC_ACCEPTANCE"
    vwap_aligned = bool(vwap.get("available")) and vwap.get("price_relation") == expected_vwap
    profile_aligned = bool(profile.get("available")) and profile.get("location") == expected_profile

    # This is the primary-timeframe scenario. It is intentionally strict on
    # measured structure and live execution, but it does not turn an HTF
    # mismatch into an invisible setup. It never authorizes a trade signal.
    tactical_structure_checks: dict[str, bool] = {
        "directional_plan": structure_checks["directional_plan"],
        "completed_primary_candles": structure_checks["completed_primary_candles"],
        "structure_not_opposed": not choch_opposes,
    }
    tactical_risk_flags: list[str] = []
    if primary_direction == direction:
        tactical_playbook = "PRIMARY_TREND_CONTINUATION"
        tactical_structure_checks.update({
            "primary_trend_aligned": True,
            "confirmed_completed_breakout": breakout,
            "relative_volume_confirmed": rvol >= 1.5,
            "decisive_candle": body_ratio >= 0.55 and close_location >= 0.60,
        })
        if not breakout:
            tactical_risk_flags.append("Primary timeframe has not closed through the relevant 20-candle structure level.")
        if rvol < 1.5:
            tactical_risk_flags.append(f"Primary relative volume {rvol:.2f}x is below the 1.50x tactical threshold.")
        if body_ratio < 0.55 or close_location < 0.60:
            tactical_risk_flags.append("Primary continuation candle lacks decisive body or close location.")
    elif primary_phase in {"RANGING", "ACCUMULATION", "DISTRIBUTION"} and primary_direction in {"NEUTRAL", direction}:
        tactical_playbook = "PRIMARY_RANGE_SWEEP_REVERSAL"
        tactical_structure_checks.update({
            "primary_range_auction_aligned": True,
            "liquidity_sweep_aligned": sweep_aligned,
            "vwap_acceptance_aligned": vwap_aligned,
            "profile_acceptance_aligned": profile_aligned,
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
            "relative_volume_confirmed": rvol >= 1.5,
            "decisive_candle": body_ratio >= 0.55 and close_location >= 0.60,
            "structure_not_opposed": not choch_opposes,
        })
        if not structure_checks["confirmed_completed_breakout"]:
            risk_flags.append("Latest completed candle has not closed through the relevant 20-candle structure level.")
        if not structure_checks["relative_volume_confirmed"]:
            risk_flags.append(f"Relative volume {rvol:.2f}x is below the 1.50x continuation threshold.")
        if not structure_checks["decisive_candle"]:
            risk_flags.append("Latest completed continuation candle lacks decisive body/close location.")
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
            "structure_not_opposed": not choch_opposes,
        })
        if not sweep_aligned:
            risk_flags.append(
                f"Higher-timeframe range detected but no completed {direction.lower()} liquidity-sweep reversal is present."
            )
        if not vwap_aligned:
            risk_flags.append(f"Range reversal has not accepted {expected_vwap.replace('_', ' ').lower()}.")
        if not profile_aligned:
            risk_flags.append(f"Range reversal has not accepted {expected_profile.replace('_', ' ').lower()}.")
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
    live = {
        "data_complete": bool(bids and asks and funding_available and oi_history.get("available") and taker.get("available")),
        "depth_imbalance": (bid_notional - ask_notional) / (bid_notional + ask_notional) if bid_notional + ask_notional else None,
        "spread_bps": (best_ask - best_bid) / midpoint * 10_000 if midpoint else None,
        "funding_rate": _number(funding.get("funding_rate")),
        "oi_change_pct": _number(oi_history.get("oi_change_pct")) if oi_history.get("available") else None,
        "price_change_pct": ((_number(primary[-1].close) - _number(primary[-6].close)) / _number(primary[-6].close) * 100.0) if _number(primary[-6].close) else 0.0,
        "taker_buy_sell_ratio": _number(taker.get("ratio", taker.get("buy_sell_ratio"))) if taker.get("available") else None,
        "multi_venue": multi_venue or {},
        "planned_notional_usd": _number(planned_notional_usd),
        "opposing_depth_notional": ask_notional if direction == "BULLISH" else bid_notional,
    }
    coverage_requirements = {
        "order_book": bool(bids and asks),
        "funding": funding_available,
        "oi_history": bool(oi_history.get("available")),
        "taker_flow": bool(taker.get("available")),
    }
    publication_coverage = {
        "ready": all(coverage_requirements.values()),
        "requirements": coverage_requirements,
        "missing": [name for name, available in coverage_requirements.items() if not available],
        "label": "PUBLICATION DATA READY" if all(coverage_requirements.values()) else "PUBLICATION DATA PARTIAL",
        "supplemental": {
            "multi_venue_flow": {
                "ready": bool((multi_venue or {}).get("flow_confirmed")),
                "status": (multi_venue or {}).get("status", "UNAVAILABLE"),
                "consensus": (multi_venue or {}).get("flow_consensus", "UNAVAILABLE"),
            },
            "displayed_liquidity_stability": (multi_venue or {}).get(
                "displayed_liquidity_stability",
                {"status": "UNAVAILABLE", "publication_veto": False},
            ),
        },
    }
    candidate = {"symbol": symbol, "direction": direction, "score": 75 if all(structure_checks.values()) else 0, "risk_flags": risk_flags}
    apply_live_confirmation(candidate, live)
    live_checks = candidate["advanced_confirmation"]["checks"]
    passed = all(structure_checks.values()) and candidate["status"] == "LIVE_CONFIRMED_REVIEW"
    status = "LIVE_CONFIRMED_REVIEW" if passed else ("STRUCTURE_REJECTED" if not all(structure_checks.values()) else "LIVE_CONFIRMATION_REJECTED")
    tactical_candidate = {
        "symbol": symbol, "direction": direction,
        "score": 75 if all(tactical_structure_checks.values()) else 0,
        "risk_flags": tactical_risk_flags,
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
            "sweep": sweep,
            "vwap": vwap,
            "volume_profile": profile,
            "bos": bos,
            "choch": choch,
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
        },
    }
    return result
