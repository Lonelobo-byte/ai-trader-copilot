from datetime import datetime, timedelta, timezone

from app.brains.signal_lifecycle import (
    advance_signal,
    build_signal_seed,
    build_signal_view,
    evaluate_signal_approval,
)


def _decision():
    return {"decision": "BUY_WATCH", "confidence": 82, "trade_grade": "A"}


def _setup():
    return {
        "entry": {"reference": 100.0, "zone_low": 99.5, "zone_high": 100.5},
        "stop": {"selected": 98.0},
        "targets": {"tp1_1r": 102.0, "tp2_2r": 104.0, "tp3_3r": 106.0, "runner_5r": 110.0},
        "position": {"risk_amount_usd": 10.0, "notional_usd": 500.0},
        "leverage": {"recommended": 3},
    }


def _ai():
    return {
        "decision": "BUY_WATCH",
        "macro_blockout": {"active": False},
        "agent_reports": {
            "risk_manager": {"approved": True},
            "pre_mortem_analyst": {"severity_score": 3},
        },
    }


def test_signal_requires_full_approval() -> None:
    approval = evaluate_signal_approval(
        decision=_decision(),
        trade_setup=_setup(),
        risk_idea={"risk_reward": 2.0, "entry_zone_low": 99.5, "entry_zone_high": 100.5},
        trend={"status": "bullish"},
        momentum={"bias": "bullish"},
        order_book={"pressure": "buyers"},
        data_freshness={"passed": True},
        liquidity={"passed": True},
        ai_result=_ai(),
        current_price=100.0,
    )
    assert approval["approved"] is True

    rejected = evaluate_signal_approval(
        decision=_decision(),
        trade_setup=_setup(),
        risk_idea={"risk_reward": 2.0},
        trend={"status": "bullish"},
        momentum={"bias": "bullish"},
        order_book={"pressure": "buyers"},
        data_freshness={"passed": True},
        liquidity={"passed": True},
        ai_result={**_ai(), "decision": "SELL_WATCH"},
        current_price=100.0,
    )
    assert rejected["approved"] is False
    assert any("CIO" in item for item in rejected["blockers"])


def test_council_approval_is_not_rejected_by_legacy_agent_contract() -> None:
    """Single-call council mode has a synthetic risk report, not approved=True."""
    single_call_ai = {
        "decision": "BUY_WATCH",
        "macro_blockout": {"active": False},
        "agent_reports": {
            "risk_manager": {"bias": "NEUTRAL", "details": "Derived by CIO."},
            "pre_mortem_analyst": {"severity_score": 3},
        },
    }
    approval = evaluate_signal_approval(
        decision=_decision(),
        trade_setup=_setup(),
        risk_idea={"risk_reward": 2.0, "entry_zone_low": 99.5, "entry_zone_high": 100.5},
        trend={"status": "balanced"},
        momentum={"bias": "neutral"},
        order_book={"pressure": "balanced"},
        data_freshness={"passed": True},
        liquidity={"passed": True},
        ai_result=single_call_ai,
        current_price=100.0,
        council_approval={"approved": True, "side": "LONG", "confirmations": 5, "blockers": []},
    )
    assert approval["approved"] is True


def test_trade_setup_preserves_low_price_pair_precision() -> None:
    from app.brains.signal_builder import build_ai_driven_trade_setup
    from app.settings import get_settings

    setup = build_ai_driven_trade_setup(
        {
            "decision": "BUY_WATCH", "confidence_pct": 75, "trade_grade": "A",
            # Deliberately unrealistic model levels are ignored. Execution
            # parameters and sizing come from measured features and committee limits.
            "suggested_entry": 999.0, "suggested_stop": 1.0,
            "suggested_targets": [1000.0, 2000.0, 3000.0],
            "institutional_dossier": {
                "risk_committee": {
                    "allocation_ceiling": {"risk_fraction": 0.005, "max_notional_usd": 500.0}
                }
            },
        },
        {"current_price": 0.00001234, "volatility": {"atr": 0.00000034}},
        get_settings(),
    )
    assert setup["entry"]["reference"] == 0.00001234
    assert setup["stop"]["selected"] == 0.00001183
    assert setup["entry"]["reference"] != 999.0
    assert setup["targets"]["tp1_1r"] > setup["entry"]["reference"]


def test_signal_advances_targets_and_locks_profit() -> None:
    now = datetime.now(timezone.utc)
    seed = build_signal_seed(
        symbol="BTCUSDT",
        timeframe="15m",
        decision=_decision(),
        trade_setup=_setup(),
        approval={"side": "LONG"},
        current_price=100.0,
        context={},
        ai_review=_ai(),
        now=now,
    )
    assert seed["status"] == "PENDING_ENTRY"

    # A price already inside the zone is only a setup, not an immediate entry.
    still_waiting = advance_signal(seed, current_price=100.0, now=now + timedelta(seconds=10))
    assert still_waiting["status"] == "PENDING_ENTRY"

    # A fresh leave-and-retest confirms the entry.
    left_zone = advance_signal(still_waiting, current_price=101.0, now=now + timedelta(minutes=1))
    active = advance_signal(left_zone, current_price=100.0, now=now + timedelta(minutes=2))
    assert active["status"] == "ACTIVE"

    tp1 = advance_signal(active, current_price=102.0, now=now + timedelta(minutes=3))
    assert tp1["status"] == "TP1_SECURED"
    assert tp1["stop_current"] == 100.0
    tp1_view = build_signal_view(tp1)
    assert tp1_view["progress_label"] == "Progress to TP2"
    assert tp1_view["progress_pct"] == 0.0
    assert tp1_view["journey_progress_pct"] == 25.0

    tp2 = advance_signal(tp1, current_price=104.0, now=now + timedelta(minutes=4))
    assert tp2["status"] == "TP2_SECURED"
    assert tp2["stop_current"] == 102.0
    tp2_view = build_signal_view(tp2)
    assert tp2_view["progress_label"] == "Progress to TP3"
    assert tp2_view["journey_progress_pct"] == 50.0

    complete = advance_signal(tp2, current_price=106.0, now=now + timedelta(minutes=5))
    assert complete["status"] == "COMPLETED"
    assert complete["target_stage"] == 3
    assert complete["exit_price"] == 106.0
    assert "successful" in complete["exit_reason"].lower()
    completed_view = build_signal_view(complete)
    assert completed_view["journey_progress_pct"] == 100.0
    assert completed_view["progress_label"] == "TP3 complete — successful trade"


def test_legacy_tp3_signal_is_finalised_as_success() -> None:
    legacy = {
        "status": "TP3_SECURED", "side": "LONG", "target_stage": 3,
        "target_3": 106.0, "events": [], "entry_reference": 100.0,
        "stop_initial": 98.0, "stop_current": 104.0, "risk_per_unit": 2.0,
    }
    finalised = advance_signal(legacy, current_price=106.0)
    assert finalised["status"] == "COMPLETED"
    assert finalised["target_stage"] == 3
    assert "successful" in finalised["exit_reason"].lower()

def test_signal_exits_when_stop_or_thesis_breaks() -> None:
    now = datetime.now(timezone.utc)
    seed = build_signal_seed(
        symbol="BTCUSDT",
        timeframe="15m",
        decision=_decision(),
        trade_setup=_setup(),
        approval={"side": "LONG"},
        current_price=100.0,
        context={},
        ai_review=_ai(),
        now=now,
    )
    stopped = advance_signal(seed, current_price=98.0, now=now + timedelta(minutes=1))
    assert stopped["status"] == "INVALIDATED"
    assert "invalidation" in stopped["exit_reason"].lower()
    view = build_signal_view(stopped)
    assert view["action"] == "EXIT_TRADE"
    assert view["exit_now"] is True

    left_zone = advance_signal(seed, current_price=101.0, now=now + timedelta(seconds=30))
    active = advance_signal(left_zone, current_price=100.0, now=now + timedelta(minutes=1))

    invalidated = advance_signal(
        active,
        current_price=98.7,
        market_context={
            "trend_status": "bearish",
            "momentum_bias": "bearish",
            "order_book_pressure": "sellers",
        },
        now=now + timedelta(minutes=1),
    )
    assert invalidated["status"] == "INVALIDATED"
    assert "reversed" in invalidated["exit_reason"].lower()
