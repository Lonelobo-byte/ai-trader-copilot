from __future__ import annotations

from typing import Any

from app.data_sources.binance_public import Candle
from app.indicators.market_story import (
    build_market_story,
    evaluate_story_direction,
    evaluate_story_playbook,
    observable_liquidity_sweep,
    observable_structure_events,
)


_BASE_TIME_MS = 1_700_000_000_000
_INTERVAL_MS = 60_000


def _candle(
    index: int,
    *,
    open_price: float,
    high: float,
    low: float,
    close: float,
    volume: float = 100.0,
    close_time: int | None = None,
) -> Candle:
    quote_volume = volume * close
    return Candle(
        open_time=_BASE_TIME_MS + index * _INTERVAL_MS,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        close_time=(
            close_time
            if close_time is not None
            else _BASE_TIME_MS + (index + 1) * _INTERVAL_MS - 1
        ),
        quote_volume=quote_volume,
        trade_count=100,
        taker_buy_base_volume=volume * 0.55,
        taker_buy_quote_volume=quote_volume * 0.55,
    )


def _base_range(count: int = 25) -> list[Candle]:
    return [
        _candle(
            index,
            open_price=100.0,
            high=100.2,
            low=99.8,
            close=100.0,
        )
        for index in range(count)
    ]


def _bullish_break() -> Candle:
    # The large high keeps subsequent lifecycle candles from being mistaken
    # for a second rolling-range break while they test or extend the first one.
    return _candle(
        25,
        open_price=100.0,
        high=104.5,
        low=99.8,
        close=100.8,
        volume=200.0,
    )


def _event_by_id(story: dict[str, Any], event_id: str) -> dict[str, Any]:
    return next(
        event
        for event in story["structure_events"]
        if event["event_id"] == event_id
    )


def test_prior_bos_is_retained_after_the_event_candle() -> None:
    candles = [
        *_base_range(),
        _bullish_break(),
        _candle(
            26,
            open_price=100.8,
            high=101.0,
            low=100.1,
            close=100.5,
        ),
    ]

    story = build_market_story(candles)
    bos = observable_structure_events(story)["bos"]

    assert bos["detected"] is True
    assert bos["type"] == "BOS"
    assert bos["direction"] == "bullish"
    assert bos["event_index"] == 25
    assert bos["age_bars"] == 1
    assert bos["break_level"] == 100.2


def test_held_retest_is_actionable() -> None:
    story = build_market_story(
        [
            *_base_range(),
            _bullish_break(),
            _candle(
                26,
                open_price=100.8,
                high=101.0,
                low=100.1,
                close=100.5,
            ),
        ]
    )
    bullish = evaluate_story_direction(story, "BULLISH")

    assert bullish["state"] == "RETESTING"
    assert bullish["actionable"] is True
    assert bullish["chase_prohibited"] is False
    assert bullish["aligned_event"]["retest_observed"] is True
    assert bullish["aligned_event"]["recent_retest"] is True
    assert bullish["aligned_event"]["invalidated_at_index"] is None


def test_extended_move_is_explicitly_do_not_chase() -> None:
    story = build_market_story(
        [
            *_base_range(),
            _bullish_break(),
            _candle(
                26,
                open_price=100.8,
                high=104.4,
                low=100.7,
                close=104.0,
            ),
        ]
    )
    bullish = evaluate_story_direction(story, "BULLISH")

    assert bullish["state"] == "EXTENDED_DO_NOT_CHASE"
    assert bullish["actionable"] is False
    assert bullish["chase_prohibited"] is True
    assert bullish["aligned_event"]["current_distance_atr"] > 2.5


def test_expanded_move_becomes_missed_after_fresh_entry_window() -> None:
    story = build_market_story(
        [
            *_base_range(),
            _bullish_break(),
            _candle(
                26,
                open_price=100.8,
                high=103.0,
                low=100.7,
                close=101.4,
            ),
            _candle(
                27,
                open_price=101.4,
                high=102.2,
                low=101.4,
                close=101.7,
            ),
            _candle(
                28,
                open_price=101.7,
                high=101.9,
                low=101.2,
                close=101.5,
            ),
        ],
        fresh_entry_bars=2,
        max_active_age=20,
    )
    bullish = evaluate_story_direction(story, "BULLISH")

    assert bullish["state"] == "MISSED"
    assert bullish["actionable"] is False
    assert bullish["chase_prohibited"] is True
    assert bullish["aligned_event"]["age_bars"] == 3
    assert bullish["aligned_event"]["max_favourable_excursion_atr"] >= 1.5


def test_old_event_expires_even_if_its_level_has_not_failed() -> None:
    story = build_market_story(
        [
            *_base_range(),
            _bullish_break(),
            _candle(
                26,
                open_price=100.8,
                high=101.1,
                low=100.4,
                close=100.7,
            ),
            _candle(
                27,
                open_price=100.7,
                high=101.0,
                low=100.4,
                close=100.7,
            ),
            _candle(
                28,
                open_price=100.7,
                high=101.0,
                low=100.4,
                close=100.7,
            ),
        ],
        max_active_age=2,
    )
    bullish = evaluate_story_direction(story, "BULLISH")

    assert bullish["state"] == "EXPIRED"
    assert bullish["actionable"] is False
    assert bullish["chase_prohibited"] is True
    assert bullish["aligned_event"]["age_bars"] == 3
    assert bullish["aligned_event"]["invalidated_at_index"] is None


def test_completed_close_through_level_invalidates_prior_bos() -> None:
    story = build_market_story(
        [
            *_base_range(),
            _bullish_break(),
            _candle(
                26,
                open_price=100.8,
                high=100.9,
                low=99.85,
                close=99.9,
            ),
        ]
    )
    bullish = evaluate_story_direction(story, "BULLISH")

    assert bullish["state"] == "INVALIDATED"
    assert bullish["actionable"] is False
    assert bullish["chase_prohibited"] is True
    assert bullish["aligned_event"]["invalidated_at_index"] == 26
    assert bullish["aligned_event"]["current_close"] < bullish["aligned_event"]["invalidation_level"]


def test_invalidated_event_cannot_be_rehabilitated_by_a_later_volatility_spike() -> None:
    invalidated_prefix = [
        *_base_range(),
        _bullish_break(),
        _candle(
            26,
            open_price=100.8,
            high=100.9,
            low=99.85,
            close=99.9,
        ),
    ]
    prefix_story = build_market_story(invalidated_prefix)
    original = prefix_story["latest_structure_event"]

    # This completed candle massively expands latest ATR and then closes back
    # above the event level.  Latest-ATR lifecycle scaling used to erase the
    # earlier invalidation and reconstruct the event as a held retest.
    later_story = build_market_story(
        [
            *invalidated_prefix,
            _candle(
                27,
                open_price=99.9,
                high=104.0,
                low=50.0,
                close=100.5,
                volume=500.0,
            ),
        ]
    )
    reconstructed = _event_by_id(later_story, original["event_id"])

    assert original["state"] == "INVALIDATED"
    assert reconstructed["state"] == "INVALIDATED"
    assert reconstructed["actionable"] is False
    assert reconstructed["invalidated_at_index"] == original["invalidated_at_index"] == 26
    assert reconstructed["terminal_at_index"] == original["terminal_at_index"] == 26
    assert reconstructed["invalidation_level"] == original["invalidation_level"]
    assert reconstructed["lifecycle_atr"] == original["lifecycle_atr"]
    assert reconstructed["lifecycle_atr_basis"] == "EVENT_CANDLE_ATR"
    assert (
        reconstructed["max_favourable_excursion_atr"]
        == original["max_favourable_excursion_atr"]
    )


def test_terminal_extension_stays_terminal_after_price_retests() -> None:
    extended_prefix = [
        *_base_range(),
        _bullish_break(),
        _candle(
            26,
            open_price=100.8,
            high=104.4,
            low=100.7,
            close=104.0,
        ),
    ]
    prefix_story = build_market_story(extended_prefix)
    original = prefix_story["latest_structure_event"]
    later_story = build_market_story(
        [
            *extended_prefix,
            _candle(
                27,
                open_price=104.0,
                high=104.1,
                low=100.1,
                close=100.5,
            ),
        ]
    )
    reconstructed = _event_by_id(later_story, original["event_id"])

    assert original["state"] == "EXTENDED_DO_NOT_CHASE"
    assert reconstructed["state"] == "EXTENDED_DO_NOT_CHASE"
    assert reconstructed["terminal_at_index"] == original["terminal_at_index"] == 26
    assert reconstructed["max_favourable_excursion_atr"] >= original[
        "max_favourable_excursion_atr"
    ]


def test_prior_liquidity_sweep_is_retained_during_held_retest() -> None:
    story = build_market_story(
        [
            *_base_range(),
            _candle(
                25,
                open_price=100.0,
                high=100.1,
                low=99.0,
                close=100.0,
                volume=200.0,
            ),
            _candle(
                26,
                open_price=100.0,
                high=100.2,
                low=99.85,
                close=100.1,
            ),
        ]
    )
    sweep = observable_liquidity_sweep(story)

    assert sweep["detected"] is True
    assert sweep["type"] == "LIQUIDITY_SWEEP"
    assert sweep["direction"] == "bullish_reversal_watch"
    assert sweep["event_index"] == 25
    assert sweep["age_bars"] == 1
    assert sweep["state"] == "RETESTING"
    assert sweep["retest_observed"] is True


def test_incomplete_candle_is_completely_ignored() -> None:
    completed = [
        *_base_range(),
        _bullish_break(),
        _candle(
            26,
            open_price=100.8,
            high=101.0,
            low=100.1,
            close=100.5,
        ),
    ]
    incomplete = _candle(
        27,
        open_price=100.5,
        high=110.0,
        low=90.0,
        close=90.5,
        volume=10_000.0,
        close_time=9_999_999_999_999,
    )

    assert build_market_story([*completed, incomplete]) == build_market_story(completed)


def test_event_detection_is_prefix_invariant_without_lookahead() -> None:
    prefix = [*_base_range(), _bullish_break()]
    prefix_story = build_market_story(prefix)
    original = prefix_story["latest_structure_event"]
    completed_suffix = _candle(
        26,
        open_price=100.8,
        high=101.0,
        low=100.1,
        close=100.5,
    )
    later_story = build_market_story([*prefix, completed_suffix])
    reconstructed = _event_by_id(later_story, original["event_id"])

    immutable_event_fields = {
        "event_id",
        "type",
        "direction",
        "break_level",
        "source",
        "reference_levels",
        "swing_index",
        "event_index",
        "event_open_time",
        "event_close_time",
        "prior_structure_bias",
        "event_open",
        "event_high",
        "event_low",
        "event_close",
        "atr_at_event",
        "body_ratio",
        "close_location",
        "relative_volume",
        "displacement_atr",
        "decisive_candle",
        "relative_volume_confirmed",
        "quality",
    }

    assert prefix_story["no_lookahead"] is True
    assert {key: original[key] for key in immutable_event_fields} == {
        key: reconstructed[key] for key in immutable_event_fields
    }
    assert original["state"] == "ACTIONABLE_NOW"
    assert reconstructed["state"] == "RETESTING"


def test_opposite_break_after_higher_highs_and_lows_is_choch() -> None:
    rows = [
        (100.0, 100.5, 99.5, 100.0),
        (100.0, 100.2, 99.0, 99.5),
        (99.5, 101.5, 99.4, 101.0),
        (101.0, 102.0, 100.5, 101.5),
        (101.5, 101.6, 100.8, 101.0),
        (101.0, 101.2, 100.0, 100.5),
        (100.5, 102.4, 100.4, 102.0),
        (102.0, 103.0, 101.5, 102.5),
        (102.5, 102.7, 101.8, 102.0),
        (102.0, 102.1, 99.0, 99.5),
    ]
    candles = [
        _candle(
            index,
            open_price=open_price,
            high=high,
            low=low,
            close=close,
            volume=200.0 if index == 9 else 100.0,
        )
        for index, (open_price, high, low, close) in enumerate(rows)
    ]

    story = build_market_story(
        candles,
        event_lookback=10,
        structure_lookback=5,
        sweep_lookback=5,
        swing_window=1,
    )
    choch = observable_structure_events(story)["choch"]

    assert choch["detected"] is True
    assert choch["type"] == "CHoCH"
    assert choch["direction"] == "bearish"
    assert choch["prior_structure_bias"] == "BULLISH"
    assert choch["event_index"] == 9


def test_radar_research_and_live_gate_project_the_same_event() -> None:
    from app.quant.live_confirmation import _observable_structure_events as live_events
    from app.quant.momentum_scanner import _observable_structure_events as radar_events
    from app.routes.radar import _observable_structure_events as research_events

    candles = [
        *_base_range(),
        _bullish_break(),
        _candle(
            26,
            open_price=100.8,
            high=101.0,
            low=100.1,
            close=100.5,
        ),
    ]

    event_ids = {
        live_events(candles)["bos"]["event_id"],
        radar_events(candles)["bos"]["event_id"],
        research_events(candles)["bos"]["event_id"],
    }
    assert len(event_ids) == 1


def test_playbook_distinguishes_actionable_event_from_missing_range_confirmation() -> None:
    event = {
        "event_id": "BOS:BULLISH:1:100",
        "event_index": 10,
        "type": "BOS",
        "direction": "BULLISH",
        "detected": True,
        "actionable": True,
        "state": "ACTIONABLE_NOW",
        "decisive_candle": True,
        "relative_volume_confirmed": True,
    }
    story = {
        "available": True,
        "structure_events": [event],
        "liquidity_events": [],
    }

    result = evaluate_story_playbook(
        primary_story=story,
        higher_story={"available": True, "structure_events": [], "liquidity_events": []},
        direction="BULLISH",
        primary_phase="ACCUMULATION",
        higher_phase="RANGING",
        vwap_context={"available": True, "price_relation": "ABOVE_ALL"},
        volume_profile={"available": True, "location": "ABOVE_POC_ACCEPTANCE"},
    )

    assert result["passed"] is False
    assert result["directional_view"]["actionable"] is True
    assert result["reason_code"] == "RANGE_SWEEP_UNCONFIRMED"


def test_higher_timeframe_opposition_compares_against_structure_not_newer_sweep() -> None:
    primary_structure = {
        "event_id": "BOS:BULLISH:1:100",
        "event_index": 10,
        "type": "BOS",
        "direction": "BULLISH",
        "detected": True,
        "actionable": True,
        "state": "ACTIONABLE_NOW",
        "decisive_candle": True,
        "relative_volume_confirmed": True,
    }
    higher_bullish_structure = {
        **primary_structure,
        "event_id": "BOS:BULLISH:2:100",
        "event_index": 10,
    }
    higher_bearish_structure = {
        **primary_structure,
        "event_id": "CHoCH:BEARISH:3:99",
        "event_index": 12,
        "type": "CHoCH",
        "direction": "BEARISH",
    }
    newer_bullish_sweep = {
        "event_id": "LIQUIDITY_SWEEP:BULLISH:4:98",
        "event_index": 20,
        "type": "LIQUIDITY_SWEEP",
        "direction": "BULLISH",
        "detected": True,
        "actionable": True,
        "state": "ACTIONABLE_NOW",
    }

    result = evaluate_story_playbook(
        primary_story={
            "available": True,
            "structure_events": [primary_structure],
            "liquidity_events": [],
        },
        higher_story={
            "available": True,
            "structure_events": [
                higher_bearish_structure,
                higher_bullish_structure,
            ],
            "liquidity_events": [newer_bullish_sweep],
        },
        direction="BULLISH",
        primary_phase="MARKUP",
        higher_phase="MARKUP",
    )

    assert result["passed"] is False
    assert result["reason_code"] == "HIGHER_TIMEFRAME_STRUCTURE_OPPOSED"
    assert result["checks"]["higher_structure_not_opposed"] is False
