from datetime import datetime, timedelta, timezone

from app.brains.signal_lifecycle import (
    advance_signal,
    build_signal_seed,
    build_signal_view,
    evaluate_signal_approval,
    market_story_matches_signal,
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


def _live_flow(side: str = "LONG") -> dict:
    bullish = side == "LONG"
    return {
        "execution_tape": {
            "available": True,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "actual_flow": {
                "available": True,
                "bias": "BULLISH" if bullish else "BEARISH",
                "status": "BUYING_CONFIRMED" if bullish else "SELLING_CONFIRMED",
                "price_response": "ACCEPTING_HIGHER" if bullish else "ACCEPTING_LOWER",
                "active_aggressor": "BUYERS" if bullish else "SELLERS",
                "qualified_source_count": 2,
                "qualified_venue_count": 2,
                "cross_venue_alignment": "ALIGNED",
            },
        },
    }


def _story_setup(event_id: str = "BOS:BULLISH:1:100") -> dict:
    setup = _setup()
    setup["market_story"] = {
        "state": "ACTIONABLE_NOW",
        "actionable": True,
        "selected_event": {
            "event_id": event_id,
            "direction": "BULLISH",
            "break_level": 100.0,
            "atr_at_event": 1.0,
            "state": "ACTIONABLE_NOW",
        },
    }
    return setup


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


def test_final_publication_contract_rejects_missing_canonical_council_approval() -> None:
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
        require_council_approval=True,
    )
    assert approval["approved"] is False
    assert any("canonical" in blocker.lower() for blocker in approval["blockers"])


def test_publication_rechecks_live_quote_distance_from_originating_event() -> None:
    approval = evaluate_signal_approval(
        decision=_decision(),
        trade_setup=_story_setup(),
        risk_idea={"risk_reward": 2.0, "entry_zone_low": 99.5, "entry_zone_high": 100.5},
        trend={"status": "bullish"},
        momentum={"bias": "bullish"},
        order_book={"pressure": "buyers"},
        data_freshness={"passed": True},
        liquidity={"passed": True},
        ai_result=_ai(),
        current_price=103.0,
    )
    assert approval["approved"] is False
    assert any("3.00 ATR beyond" in blocker for blocker in approval["blockers"])


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


def test_signal_story_updates_require_origin_event_identity_and_direction() -> None:
    now = datetime.now(timezone.utc)
    seed = build_signal_seed(
        symbol="BTCUSDT",
        timeframe="15m",
        decision=_decision(),
        trade_setup=_story_setup("origin-event"),
        approval={"side": "LONG"},
        current_price=100.0,
        context={},
        ai_review=_ai(),
        now=now,
    )
    assert seed["context"]["structure_event_id"] == "origin-event"
    assert seed["context"]["structure_event_direction"] == "BULLISH"

    unrelated_story = {
        "state": "INVALIDATED",
        "reason": "A newer unrelated setup failed.",
        "selected_event": {
            "event_id": "different-event",
            "direction": "BULLISH",
            "state": "INVALIDATED",
        },
    }
    assert market_story_matches_signal(seed, unrelated_story) is False
    unchanged = advance_signal(
        seed,
        current_price=100.0,
        market_context={"market_story": unrelated_story},
        now=now + timedelta(seconds=10),
    )
    assert unchanged["status"] == "PENDING_ENTRY"

    wrong_direction_story = {
        "state": "INVALIDATED",
        "selected_event": {
            "event_id": "origin-event",
            "direction": "BEARISH",
            "state": "INVALIDATED",
        },
    }
    assert market_story_matches_signal(seed, wrong_direction_story) is False
    still_unchanged = advance_signal(
        unchanged,
        current_price=100.0,
        market_context={"market_story": wrong_direction_story},
        now=now + timedelta(seconds=20),
    )
    assert still_unchanged["status"] == "PENDING_ENTRY"

    origin_invalidated = {
        "state": "INVALIDATED",
        "reason": "The originating completed-candle event failed.",
        "selected_event": {
            "event_id": "origin-event",
            "direction": "BULLISH",
            "state": "INVALIDATED",
        },
    }
    assert market_story_matches_signal(seed, origin_invalidated) is True
    invalidated = advance_signal(
        still_unchanged,
        current_price=100.0,
        market_context={"market_story": origin_invalidated},
        now=now + timedelta(seconds=30),
    )
    assert invalidated["status"] == "INVALIDATED"
    assert "originating" in invalidated["exit_reason"]


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

    # A fresh leave-and-retest only arms the entry. It needs a favourable
    # reclaim plus current aggressor flow before becoming active.
    left_zone = advance_signal(still_waiting, current_price=101.0, now=now + timedelta(minutes=1))
    armed = advance_signal(
        left_zone,
        current_price=100.0,
        market_context=_live_flow(),
        now=now + timedelta(minutes=2),
    )
    assert armed["status"] == "PENDING_ENTRY"
    assert armed["context"]["entry_confirmation"]["state"] == "ARMED_AWAITING_RECLAIM_AND_FLOW"
    active = advance_signal(
        armed,
        current_price=100.2,
        market_context=_live_flow(),
        now=now + timedelta(minutes=2, seconds=5),
    )
    assert active["status"] == "ACTIVE"

    tp1 = advance_signal(active, current_price=102.0, now=now + timedelta(minutes=3))
    assert tp1["status"] == "TP1_SECURED"
    assert tp1["stop_current"] == 100.2
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

def test_signal_exits_at_stop_but_ignores_legacy_indicator_reversal() -> None:
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
    armed = advance_signal(
        left_zone,
        current_price=100.0,
        market_context=_live_flow(),
        now=now + timedelta(minutes=1),
    )
    active = advance_signal(
        armed,
        current_price=100.2,
        market_context=_live_flow(),
        now=now + timedelta(minutes=1, seconds=5),
    )

    unchanged = advance_signal(
        active,
        current_price=98.7,
        market_context={
            "trend_status": "bearish",
            "momentum_bias": "bearish",
            "order_book_pressure": "sellers",
        },
        now=now + timedelta(minutes=1),
    )
    assert unchanged["status"] == "ACTIVE"


def test_zone_touch_does_not_activate_without_reclaim_and_aligned_live_flow() -> None:
    now = datetime.now(timezone.utc)
    seed = build_signal_seed(
        symbol="BTCUSDT", timeframe="15m", decision=_decision(),
        trade_setup=_setup(), approval={"side": "LONG"}, current_price=101.0,
        context={}, ai_review=_ai(), now=now,
    )
    armed = advance_signal(
        seed,
        current_price=100.0,
        market_context=_live_flow("SHORT"),
        now=now + timedelta(seconds=10),
    )
    assert armed["status"] == "PENDING_ENTRY"
    assert armed["events"][-1]["kind"] == "entry_zone_armed"

    opposed = advance_signal(
        armed,
        current_price=100.2,
        market_context=_live_flow("SHORT"),
        now=now + timedelta(seconds=20),
    )
    assert opposed["status"] == "PENDING_ENTRY"
    assert opposed["context"]["entry_confirmation"]["opposed"] is True
    assert opposed["context"]["entry_confirmation"]["reclaim_confirmed"] is True

    confirmed = advance_signal(
        opposed,
        current_price=100.25,
        market_context=_live_flow("LONG"),
        now=now + timedelta(seconds=30),
    )
    assert confirmed["status"] == "ACTIVE"
    assert confirmed["context"]["entry_confirmation"]["passed"] is True


def test_completed_retest_with_live_flow_activates_without_requiring_another_retest() -> None:
    now = datetime.now(timezone.utc)
    setup = _story_setup("retest-event")
    setup["market_story"].update({"state": "RETESTING", "actionable": True})
    setup["market_story"]["selected_event"].update({
        "state": "RETESTING",
        "actionable": True,
        "current_close": 100.0,
    })
    seed = build_signal_seed(
        symbol="BTCUSDT", timeframe="15m", decision=_decision(),
        trade_setup=setup, approval={"side": "LONG"}, current_price=100.0,
        context=_live_flow("LONG"), ai_review=_ai(), now=now,
    )
    assert seed["status"] == "ACTIVE"
    assert seed["entry_price"] == 100.0
    assert seed["context"]["requires_fresh_entry_retest"] is False
    assert seed["context"]["entry_confirmation"]["state"] == "CONFIRMED_AT_PUBLICATION"
    assert seed["events"][-1]["kind"] == "entry_confirmed"


def test_completed_retest_without_live_flow_remains_pending() -> None:
    setup = _story_setup("retest-no-flow")
    setup["market_story"].update({"state": "RETESTING", "actionable": True})
    setup["market_story"]["selected_event"].update({"state": "RETESTING", "actionable": True})
    seed = build_signal_seed(
        symbol="BTCUSDT", timeframe="15m", decision=_decision(),
        trade_setup=setup, approval={"side": "LONG"}, current_price=100.0,
        context={}, ai_review=_ai(),
    )
    assert seed["status"] == "PENDING_ENTRY"
    assert seed["context"]["entry_confirmation"]["available"] is False


def test_live_completed_story_arms_original_event_and_confirms_reclaim() -> None:
    now = datetime.now(timezone.utc)
    seed = build_signal_seed(
        symbol="BTCUSDT", timeframe="15m", decision=_decision(),
        trade_setup=_story_setup("live-retest-event"), approval={"side": "LONG"},
        current_price=101.0, context={}, ai_review=_ai(), now=now,
    )
    live_retest_story = {
        "state": "RETESTING",
        "actionable": True,
        "reason": "The originating level held on the latest completed candle.",
        "aligned_event": {
            "event_id": "live-retest-event",
            "direction": "BULLISH",
            "state": "RETESTING",
            "actionable": True,
            "break_level": 100.0,
            "atr_at_event": 1.0,
            "current_close": 100.0,
        },
    }
    confirmed = advance_signal(
        seed,
        current_price=100.2,
        market_context={
            **_live_flow("LONG"),
            "market_story": live_retest_story,
        },
        now=now + timedelta(minutes=1),
    )
    assert confirmed["status"] == "ACTIVE"
    assert any(event["kind"] == "completed_retest_armed" for event in confirmed["events"])
    assert confirmed["context"]["market_story"]["state"] == "RETESTING"
    assert confirmed["context"]["last_completed_story_update_at"]


def test_live_consumed_campaign_cancels_pending_entry_instead_of_chasing() -> None:
    now = datetime.now(timezone.utc)
    seed = build_signal_seed(
        symbol="BTCUSDT", timeframe="15m", decision=_decision(),
        trade_setup=_story_setup("late-campaign-event"), approval={"side": "LONG"},
        current_price=101.0, context={}, ai_review=_ai(), now=now,
    )
    consumed_story = {
        "state": "LATE_STRUCTURE_DO_NOT_CHASE",
        "actionable": False,
        "chase_prohibited": True,
        "reason": "The bullish campaign already consumed 6.2 pre-event ATR.",
        "reason_code": "CAUSAL_CAMPAIGN_ALREADY_CONSUMED",
        "aligned_event": {
            "event_id": "late-campaign-event",
            "direction": "BULLISH",
            "state": "LATE_STRUCTURE_DO_NOT_CHASE",
            "actionable": False,
            "current_close": 103.0,
        },
    }

    cancelled = advance_signal(
        seed,
        current_price=103.0,
        market_context={"market_story": consumed_story},
        now=now + timedelta(minutes=1),
    )

    assert cancelled["status"] == "CANCELLED"
    assert "consumed" in cancelled["exit_reason"]
    assert cancelled["events"][-1]["kind"] == "structure_entry_unavailable"
