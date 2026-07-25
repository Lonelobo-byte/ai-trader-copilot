from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from app.data_sources.binance_public import interval_seconds


OPEN_SIGNAL_STATUSES = {"PENDING_ENTRY", "ACTIVE", "TP1_SECURED", "TP2_SECURED"}
TERMINAL_SIGNAL_STATUSES = {"COMPLETED", "STOPPED_OUT", "INVALIDATED", "CANCELLED", "EXPIRED"}


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


def evaluate_signal_approval(
    *, decision: Mapping[str, Any], trade_setup: Mapping[str, Any] | None,
    risk_idea: Mapping[str, Any] | None, trend: Mapping[str, Any],
    momentum: Mapping[str, Any], order_book: Mapping[str, Any],
    data_freshness: Mapping[str, Any], liquidity: Mapping[str, Any],
    ai_result: Mapping[str, Any] | None, current_price: float,
    council_approval: Mapping[str, Any] | None = None,
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

    if side is None:
        blockers.append("No directional AI Council decision.")
    # Legacy callers without a council release decision retain the original
    # deterministic approval contract.
    if council_approval is None:
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

    confirmations = 0
    if side and council_approval is None:
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
    elif council_approval is None:
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
    # A setup is not a position. Even if it is published while price is in the
    # entry zone, require price to leave and retest the zone before activating
    # it. This prevents entering a stale snapshot and then immediately hitting
    # a nearby stop on the next live tick.
    status = "PENDING_ENTRY"
    initial_detail = (
        f"Price {current_price:.4f} is inside the approved entry zone. Wait for a fresh leave-and-retest confirmation; do not enter yet."
        if price_in_zone else f"Waiting for price to reach {entry_low:.4f} - {entry_high:.4f}."
    )
    return {
        "symbol": symbol, "timeframe": timeframe, "side": approval["side"], "status": status,
        "decision": decision["decision"], "confidence": _number(decision.get("confidence")),
        "entry_low": entry_low, "entry_high": entry_high, "entry_reference": entry_reference,
        "entry_price": current_price if price_in_zone else None,
        "stop_initial": stop_price, "stop_current": stop_price,
        "target_1": _number(targets.get("tp1_1r")), "target_2": _number(targets.get("tp2_2r")),
        "target_3": _number(targets.get("tp3_3r")), "target_runner": _number(targets.get("runner_5r")),
        "target_stage": 0, "risk_per_unit": abs(entry_reference - stop_price),
        "risk_amount_usd": _number(position.get("risk_amount_usd")),
        "notional_usd": _number(position.get("notional_usd")),
        "recommended_leverage": int(_number((trade_setup.get("leverage") or {}).get("recommended"), 1)),
        "current_price": current_price, "entry_timeout_at": now + timedelta(seconds=interval * 6),
        "expires_at": now + timedelta(seconds=interval * 32), "published_at": now,
        "last_evaluated_at": now, "events": [
            _event("entry_confirmed" if price_in_zone else "signal_published", "Entry confirmed" if price_in_zone else "Signal published", initial_detail, now)
        ],
        "context": {
            **dict(context),
            "requires_fresh_entry_retest": True,
            "left_entry_zone_after_publication": not price_in_zone,
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
    risk = _number(state.get("risk_per_unit"))

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

    if status == "PENDING_ENTRY":
        breached = current_price <= stop if side == "LONG" else current_price >= stop
        if breached:
            return close("INVALIDATED", "entry_invalidated", "Signal invalidated", "Price reached the invalidation stop before entry.")
        if now >= _utc(state.get("entry_timeout_at"), now):
            return close("EXPIRED", "entry_timeout", "Entry window expired", "Price did not reach the approved entry zone in time.")
        in_entry_zone = _number(state.get("entry_low")) <= current_price <= _number(state.get("entry_high"))
        context_state = dict(state.get("context") or {})
        if not in_entry_zone:
            context_state["left_entry_zone_after_publication"] = True
            state["context"] = context_state
            return state
        if context_state.get("requires_fresh_entry_retest") and not context_state.get("left_entry_zone_after_publication"):
            return state
        if in_entry_zone:
            state.update({"status": "ACTIVE", "entry_price": current_price})
            entry = current_price
            events.append(_event("entry_confirmed", "Entry confirmed", f"Fresh retest confirmed entry at {current_price:.4f}.", now))
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

    context = market_context or {}
    reversed_thesis = (
        side == "LONG" and context.get("trend_status") == "bearish" and context.get("momentum_bias") == "bearish" and context.get("order_book_pressure") == "sellers"
    ) or (
        side == "SHORT" and context.get("trend_status") == "bullish" and context.get("momentum_bias") == "bullish" and context.get("order_book_pressure") == "buyers"
    )
    adverse = current_price <= entry - risk * 0.65 if side == "LONG" else current_price >= entry + risk * 0.65
    if reversed_thesis and adverse:
        return close("INVALIDATED", "thesis_invalidated", "Exit now", "Trend, momentum, and order flow reversed against the thesis before TP1.")
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
    return {
        "id": signal.get("id"), "symbol": signal.get("symbol"), "timeframe": signal.get("timeframe"), "side": side,
        "status": status, "action": signal_action(status), "headline": signal_action(status).replace("_", " "),
        "reason": signal.get("exit_reason") or "Monitoring the published signal against its fixed plan.",
        "exit_now": status in {"STOPPED_OUT", "INVALIDATED"},
        "confidence": round(_number(signal.get("confidence")), 2), "current_price": current,
        "entry": {
            "low": _number(signal.get("entry_low")),
            "high": _number(signal.get("entry_high")),
            "reference": _number(signal.get("entry_reference")),
            "price": signal.get("entry_price"),
        },
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
