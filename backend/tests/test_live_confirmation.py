from app.data_sources.binance_public import Candle
from app.quant.live_confirmation import apply_live_confirmation, verify_main_signal_snapshot
from unittest.mock import patch


def _candles(*, start: float, step: float, last_volume: float) -> list[Candle]:
    result = []
    for index in range(60):
        close = start + index * step
        if index == 59:
            close += 1.0  # completed breakout beyond the preceding candle high
        volume = last_volume if index == 59 else 1_000.0
        open_price = close - (0.50 if index == 59 else 0.15)
        high = close + 0.10 if index == 59 else close + 0.25
        low = close - 0.20 if index == 59 else close - 0.25
        result.append(Candle(
            open_time=index * 60_000,
            open=open_price, high=high, low=low, close=close,
            volume=volume, close_time=(index + 1) * 60_000, quote_volume=volume * close,
            trade_count=100, taker_buy_base_volume=volume * 0.60,
            taker_buy_quote_volume=volume * close * 0.60,
        ))
    return result


def _live_inputs(price: float) -> dict:
    return {
        "order_book": {
            "bids": [[price - 0.01, 30.0], [price - 0.02, 20.0]],
            "asks": [[price + 0.01, 10.0], [price + 0.02, 8.0]],
        },
        "funding": {"funding_rate": 0.0001},
        "derivatives": {
            "oi_history": {"available": True, "oi_change_pct": 1.2},
            "taker_buy_sell_volume": {"available": True, "buy_sell_ratio": 1.10},
        },
        "multi_venue": {
            "actual_flow": {
                "available": True,
                "status": "BUYING_CONFIRMED",
                "bias": "BULLISH",
                "qualified_source_count": 2,
                "qualified_venue_count": 2,
                "cross_venue_alignment": "ALIGNED",
                "active_aggressor": "BUYERS",
                "production_qualified": True,
            },
            "displayed_liquidity_stability": {
                "status": "STABLE",
                "publication_veto": False,
            },
        },
    }


def _actionable_sweep_story(direction: str = "BULLISH") -> dict:
    event = {
        "detected": True,
        "event_id": f"test-sweep-{direction.lower()}",
        "type": "LIQUIDITY_SWEEP",
        "direction": direction,
        "event_index": 59,
        "age_bars": 0,
        "state": "ACTIONABLE_NOW",
        "actionable": True,
        "chase_prohibited": False,
        "break_level": 100.0,
        "swept_level": 100.0,
        "atr_at_event": 1.0,
        "invalidation_level": 99.0 if direction == "BULLISH" else 101.0,
        "quality": "STRONG",
        "reason": "Test sweep remains actionable.",
    }
    return {
        "available": True,
        "structure_events": [],
        "liquidity_events": [event],
        "latest_event": event,
        "latest_liquidity_event": event,
        "current_state": "ACTIONABLE_NOW",
        "actionability": {
            "status": "ACTIONABLE_NOW",
            "actionable": True,
            "direction": direction,
        },
    }


def test_live_quote_below_bullish_invalidation_blocks_causal_review() -> None:
    event = {
        "detected": True,
        "event_id": "CHOCH:BULLISH:10",
        "type": "CHOCH",
        "direction": "BULLISH",
        "event_index": 10,
        "state": "RETESTING",
        "actionable": True,
        "break_level": 100.0,
        "atr_at_event": 1.0,
        "invalidation_level": 99.8,
    }
    candidate = {
        "direction": "BULLISH",
        "score": 80,
        "risk_flags": [],
        "causal_radar": True,
        "market_context": {
            "actionability": {
                "actionable": True,
                "state": "RETESTING",
                "aligned_event": event,
            }
        },
    }
    live = {
        "current_price": 99.7,
        "data_complete": True,
        "spread_bps": 2.0,
        "depth_imbalance": 0.1,
        "funding_rate": 0.0001,
        "oi_change_pct": 1.0,
        "price_change_pct": 0.5,
        "execution_tape": _live_inputs(99.7)["multi_venue"],
    }

    apply_live_confirmation(candidate, live)

    checks = candidate["advanced_confirmation"]["checks"]
    assert checks["market_story_live_location_not_chased"] is True
    assert checks["market_story_live_invalidation_held"] is False
    assert candidate["review_status"] == "WATCH_ONLY"
    assert any("invalidation" in flag.lower() for flag in candidate["risk_flags"])


def _prior_break_candles(*, extended: bool = False) -> list[Candle]:
    candles: list[Candle] = []
    for index in range(58):
        candles.append(Candle(
            open_time=index * 60_000,
            open=100.0, high=100.2, low=99.8, close=100.0,
            volume=1_000.0, close_time=(index + 1) * 60_000,
            quote_volume=100_000.0, trade_count=100,
            taker_buy_base_volume=550.0, taker_buy_quote_volume=55_000.0,
        ))
    candles.append(Candle(
        open_time=58 * 60_000,
        open=100.0, high=104.5 if extended else 101.0, low=99.9, close=100.8,
        volume=2_000.0, close_time=59 * 60_000,
        quote_volume=201_600.0, trade_count=200,
        taker_buy_base_volume=1_300.0, taker_buy_quote_volume=131_040.0,
    ))
    final_close = 104.0 if extended else 100.5
    candles.append(Candle(
        open_time=59 * 60_000,
        open=100.8, high=104.4 if extended else 101.0,
        low=100.7 if extended else 100.1, close=final_close,
        volume=1_000.0, close_time=60 * 60_000,
        quote_volume=1_000.0 * final_close, trade_count=100,
        taker_buy_base_volume=600.0, taker_buy_quote_volume=600.0 * final_close,
    ))
    return candles


def test_main_signal_uses_radar_equivalent_confirmation_gate() -> None:
    # Use an early held-retest event. A monotonic 60-candle advance is now
    # deliberately classified as a consumed campaign, not a valid entry.
    primary = _prior_break_candles(extended=False)
    higher = _candles(start=100.0, step=0.40, last_volume=2_000.0)
    inputs = _live_inputs(primary[-1].close)
    with patch("app.quant.live_confirmation.classify_market_phase", return_value="MARKUP"):
        result = verify_main_signal_snapshot(
            symbol="TESTUSDT", timeframe="5m", side="LONG", candles=primary, higher_candles=higher,
            order_book=inputs["order_book"], funding=inputs["funding"], derivatives=inputs["derivatives"],
            multi_venue=inputs["multi_venue"],
        )
    assert result["passed"] is True
    assert result["status"] == "LIVE_CONFIRMED_REVIEW"
    assert all(result["structure_checks"].values())
    assert all(result["live_checks"].values())
    assert result["publication_coverage"]["ready"] is True
    assert result["publication_coverage"]["inputs_complete"] is True
    assert result["publication_coverage"]["confirmation_ready"] is True
    assert result["publication_coverage"]["confirmation_status"] == "CONFIRMED"
    assert result["publication_coverage"]["missing"] == []


def test_neutral_snapshot_keeps_observational_confirmation_evidence() -> None:
    primary = _candles(start=100.0, step=0.20, last_volume=2_000.0)
    result = verify_main_signal_snapshot(
        symbol="TESTUSDT",
        timeframe="5m",
        side=None,
        candles=primary,
        higher_candles=[],
        order_book={},
        funding={},
        derivatives={},
    )

    assert result["passed"] is False
    assert result["direction"] == "NEUTRAL"
    assert result["status"] == "STRUCTURE_REJECTED"
    assert result["metrics"]["primary_phase"] != "UNAVAILABLE"
    assert result["metrics"]["rvol"] > 0
    assert result["metrics"]["selected_structure_event"]
    assert result["structure_story"]["primary"]["available"] is True


def test_main_signal_keeps_depth_as_supporting_evidence_not_a_snapshot_veto() -> None:
    primary = _prior_break_candles(extended=False)
    higher = _candles(start=100.0, step=0.40, last_volume=2_000.0)
    inputs = _live_inputs(primary[-1].close)
    inputs["order_book"] = {
        "bids": [[primary[-1].close - 0.01, 2.0]],
        "asks": [[primary[-1].close + 0.01, 50.0]],
    }
    with patch("app.quant.live_confirmation.classify_market_phase", return_value="MARKUP"):
        result = verify_main_signal_snapshot(
            symbol="TESTUSDT", timeframe="5m", side="LONG", candles=primary, higher_candles=higher,
            order_book=inputs["order_book"], funding=inputs["funding"], derivatives=inputs["derivatives"],
            multi_venue=inputs["multi_venue"],
        )
    assert result["passed"] is True
    assert result["status"] == "LIVE_CONFIRMED_REVIEW"
    assert result["live_checks"]["depth_aligned"] is False
    assert result["live_evidence"]["depth_evidence"] == "CONTRADICTORY_SNAPSHOT"
    assert all(result["live_evidence"]["required_checks"].values())


def test_main_signal_uses_prior_break_while_current_candle_retests() -> None:
    primary = _prior_break_candles()
    higher = _candles(start=100.0, step=0.40, last_volume=2_000.0)
    inputs = _live_inputs(primary[-1].close)
    with patch("app.quant.live_confirmation.classify_market_phase", return_value="MARKUP"):
        result = verify_main_signal_snapshot(
            symbol="TESTUSDT", timeframe="5m", side="LONG", candles=primary, higher_candles=higher,
            order_book=inputs["order_book"], funding=inputs["funding"], derivatives=inputs["derivatives"],
            multi_venue=inputs["multi_venue"],
        )

    event = result["metrics"]["selected_structure_event"]
    assert result["passed"] is True
    assert event["event_index"] == 58
    assert event["age_bars"] == 1
    assert event["state"] == "RETESTING"
    assert result["structure_story"]["setup_state"] == "RETESTING"


def test_main_signal_rejects_correct_direction_when_entry_is_extended() -> None:
    primary = _prior_break_candles(extended=True)
    higher = _candles(start=100.0, step=0.40, last_volume=2_000.0)
    inputs = _live_inputs(primary[-1].close)
    with patch("app.quant.live_confirmation.classify_market_phase", return_value="MARKUP"):
        result = verify_main_signal_snapshot(
            symbol="TESTUSDT", timeframe="5m", side="LONG", candles=primary, higher_candles=higher,
            order_book=inputs["order_book"], funding=inputs["funding"], derivatives=inputs["derivatives"],
            multi_venue=inputs["multi_venue"],
        )

    assert result["passed"] is False
    assert result["structure_story"]["setup_state"] == "PULLBACK_REQUIRED"
    assert "wait for a completed pullback" in result["reason"].lower()


def test_planned_notional_is_blocked_when_displayed_depth_capacity_is_too_small() -> None:
    primary = _candles(start=100.0, step=0.20, last_volume=2_000.0)
    higher = _candles(start=100.0, step=0.40, last_volume=2_000.0)
    inputs = _live_inputs(primary[-1].close)
    result = verify_main_signal_snapshot(
        symbol="TESTUSDT", timeframe="5m", side="LONG", candles=primary, higher_candles=higher,
        order_book=inputs["order_book"], funding=inputs["funding"], derivatives=inputs["derivatives"],
        multi_venue=inputs["multi_venue"],
        planned_notional_usd=10_000.0,
    )
    assert result["passed"] is False
    assert result["live_checks"]["execution_capacity_sufficient"] is False
    assert result["publication_coverage"]["inputs_complete"] is True
    assert result["publication_coverage"]["confirmation_ready"] is False
    assert result["publication_coverage"]["confirmation_status"] == "AWAITED"
    assert result["live_evidence"]["execution_capacity"]["evaluated"] is True
    assert any("10% of the displayed" in item for item in result["risk_flags"])


def test_errored_funding_payload_is_missing_data_not_neutral_funding() -> None:
    primary = _candles(start=100.0, step=0.20, last_volume=2_000.0)
    higher = _candles(start=100.0, step=0.40, last_volume=2_000.0)
    inputs = _live_inputs(primary[-1].close)
    result = verify_main_signal_snapshot(
        symbol="TESTUSDT", timeframe="5m", side="LONG", candles=primary, higher_candles=higher,
        order_book=inputs["order_book"],
        funding={"funding_rate": 0.0, "error": "provider unavailable"},
        derivatives=inputs["derivatives"],
        multi_venue=inputs["multi_venue"],
    )
    assert result["passed"] is False
    assert result["publication_coverage"]["requirements"]["funding"] is False
    assert result["publication_coverage"]["inputs_complete"] is False
    assert result["publication_coverage"]["confirmation_ready"] is False
    assert result["publication_coverage"]["confirmation_status"] == "INPUTS_PARTIAL"
    assert "funding" in result["publication_coverage"]["missing"]
    assert result["live_checks"]["data_complete"] is False


def test_main_signal_accepts_completed_range_sweep_with_context_acceptance() -> None:
    """Neutral/ranging phases must be evaluated as a sweep reversal, not a failed trend."""
    primary = _candles(start=100.0, step=0.20, last_volume=2_000.0)
    higher = _candles(start=100.0, step=0.40, last_volume=2_000.0)
    inputs = _live_inputs(primary[-1].close)
    with (
        patch("app.quant.live_confirmation.classify_market_phase", return_value="RANGING"),
        patch("app.quant.live_confirmation.build_market_story", return_value=_actionable_sweep_story()),
        patch("app.quant.live_confirmation.build_vwap_context", return_value={"available": True, "price_relation": "ABOVE_ALL"}),
        patch("app.quant.live_confirmation.build_volume_profile", return_value={"available": True, "location": "ABOVE_POC_ACCEPTANCE"}),
    ):
        result = verify_main_signal_snapshot(
            symbol="TESTUSDT", timeframe="5m", side="LONG", candles=primary, higher_candles=higher,
            order_book=inputs["order_book"], funding=inputs["funding"], derivatives=inputs["derivatives"],
            multi_venue=inputs["multi_venue"],
        )

    assert result["passed"] is True
    assert result["metrics"]["playbook"] == "RANGE_SWEEP_REVERSAL"
    assert result["structure_checks"]["liquidity_sweep_aligned"] is True


def test_main_signal_accepts_accumulation_inside_higher_timeframe_range_with_sweep() -> None:
    """Accumulation within a higher-timeframe range is valid only after acceptance evidence."""
    primary = _candles(start=100.0, step=0.20, last_volume=2_000.0)
    higher = _candles(start=100.0, step=0.40, last_volume=2_000.0)
    inputs = _live_inputs(primary[-1].close)
    with (
        patch("app.quant.live_confirmation.classify_market_phase", side_effect=["ACCUMULATION", "RANGING"]),
        patch("app.quant.live_confirmation.build_market_story", return_value=_actionable_sweep_story()),
        patch("app.quant.live_confirmation.build_vwap_context", return_value={"available": True, "price_relation": "ABOVE_ALL"}),
        patch("app.quant.live_confirmation.build_volume_profile", return_value={"available": True, "location": "ABOVE_POC_ACCEPTANCE"}),
    ):
        result = verify_main_signal_snapshot(
            symbol="TESTUSDT", timeframe="5m", side="LONG", candles=primary, higher_candles=higher,
            order_book=inputs["order_book"], funding=inputs["funding"], derivatives=inputs["derivatives"],
            multi_venue=inputs["multi_venue"],
        )

    assert result["passed"] is True
    assert result["metrics"]["playbook"] == "RANGE_AUCTION_SWEEP_REVERSAL"
    assert result["structure_checks"]["primary_range_auction_aligned"] is True


def test_primary_setup_remains_visible_as_tactical_when_higher_timeframe_mismatches() -> None:
    """HTF disagreement blocks publication but must not erase valid LTF evidence."""
    primary = _prior_break_candles(extended=False)
    higher = _candles(start=100.0, step=0.40, last_volume=2_000.0)
    inputs = _live_inputs(primary[-1].close)
    with patch("app.quant.live_confirmation.classify_market_phase", side_effect=["MARKUP", "RANGING"]):
        result = verify_main_signal_snapshot(
            symbol="TESTUSDT", timeframe="5m", side="LONG", candles=primary, higher_candles=higher,
            order_book=inputs["order_book"], funding=inputs["funding"], derivatives=inputs["derivatives"],
            multi_venue=inputs["multi_venue"],
        )

    assert result["passed"] is False
    assert result["scenarios"]["institutional"]["passed"] is False
    assert result["scenarios"]["tactical"]["passed"] is True
    assert result["scenarios"]["tactical"]["status"] == "TACTICAL_CONFIRMED_WATCH"
    assert result["scenarios"]["tactical"]["higher_timeframe_aligned"] is False


def test_primary_scenario_stays_visible_while_higher_timeframe_is_unavailable() -> None:
    """A tactical evidence watch must not be hidden behind the HTF rejection."""
    primary = _candles(start=100.0, step=-0.20, last_volume=2_000.0)
    inputs = _live_inputs(primary[-1].close)
    with patch("app.quant.live_confirmation.classify_market_phase", return_value="DISTRIBUTION"):
        result = verify_main_signal_snapshot(
            symbol="TESTUSDT", timeframe="5m", side="SHORT", candles=primary, higher_candles=[],
            order_book=inputs["order_book"], funding=inputs["funding"], derivatives=inputs["derivatives"],
        )

    assert result["passed"] is False
    assert result["scenarios"]["institutional"]["reason"].startswith("No approved regime playbook")
    assert result["scenarios"]["tactical"]["candidate"] is True
    assert result["scenarios"]["tactical"]["status"] == "TACTICAL_EVIDENCE_WATCH"
    assert result["scenarios"]["tactical"]["higher_timeframe_state"] == "UNAVAILABLE"
