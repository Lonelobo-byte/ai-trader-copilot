import pytest
from app.quant.momentum_scanner import (
    _apply_live_confirmation,
    _ai_score_candidates,
    _candidate_from_causal_context,
    _legacy_get_breakout_candidates,
    _refresh_causal_context_from_live,
    analyze_radar_structure_confluence,
    calculate_ema,
    calculate_rsi,
    calculate_atr,
    calculate_keltner_channels,
    get_breakout_candidates,
)
from app.data_sources.binance_public import Candle
from unittest.mock import patch


@pytest.mark.asyncio
async def test_legacy_indicator_and_blended_ai_radar_paths_are_retired() -> None:
    with pytest.raises(RuntimeError, match="RSI/EMA Radar scoring is retired"):
        await _legacy_get_breakout_candidates()
    with pytest.raises(RuntimeError, match="blended Radar AI scoring is retired"):
        await _ai_score_candidates([])

def test_calculate_ema():
    prices = [10.0, 11.0, 12.0, 13.0, 14.0]
    ema = calculate_ema(prices, 3)
    assert len(ema) == len(prices)
    assert ema[0] == 0.0
    assert ema[1] == 0.0
    assert ema[2] > 0.0

def test_calculate_rsi():
    prices = [100.0] * 20
    rsi = calculate_rsi(prices, 14)
    assert len(rsi) == len(prices)
    assert 40 <= rsi[-1] <= 60

def test_calculate_atr():
    highs = [10.0] * 20
    lows = [9.0] * 20
    closes = [9.5] * 20
    atr = calculate_atr(highs, lows, closes, 14)
    assert len(atr) == len(closes)

def test_calculate_keltner_channels():
    closes = [10.0] * 30
    highs = [11.0] * 30
    lows = [9.0] * 30
    basis, upper, lower = calculate_keltner_channels(closes, highs, lows, 20, 2.0)
    assert len(basis) == 30
    assert len(upper) == 30
    assert len(lower) == 30
    assert upper[-1] > basis[-1] > lower[-1]

@pytest.mark.asyncio
async def test_get_breakout_candidates():
    # Mocking system time to ensure candle checks pass
    candidates = await get_breakout_candidates(ltf="15m", htf="4h")
    assert isinstance(candidates, list)
    if candidates:
        candidate = candidates[0]
        assert "symbol" in candidate
        assert "score" in candidate
        assert "rvol" in candidate
        assert "atr_ratio" in candidate
        assert "rsi" in candidate
        assert "price_change_pct" in candidate
        assert "close_price" in candidate
        assert "direction" in candidate
        assert "status" in candidate
        assert "htf_direction" in candidate
        assert "review_status" in candidate
        assert candidate["evaluation_mode"] == "causal_market_discovery_then_live_confirmation"
        assert "advanced_confirmation" in candidate
        assert "market_context" in candidate
        assert "liquidity_map" in candidate
        assert "positioning" in candidate
        assert "rsi" in candidate and candidate["rsi"] is None


def test_live_confirmation_rejects_opposing_order_flow():
    candidate = {
        "symbol": "TESTUSDT",
        "direction": "BULLISH",
        "score": 70,
        "risk_flags": [],
    }
    _apply_live_confirmation(candidate, {
        "data_complete": True,
        "spread_bps": 2.0,
        "depth_imbalance": -0.12,
        "taker_buy_sell_ratio": 0.86,
        "oi_change_pct": 1.2,
        "funding_rate": 0.0001,
    })
    assert candidate["review_status"] == "WATCH_ONLY"
    assert candidate["status"] == "LIVE_CONFIRMATION_REJECTED"


def test_live_confirmation_requires_all_directional_checks():
    candidate = {
        "symbol": "TESTUSDT",
        "direction": "BEARISH",
        "score": 70,
        "risk_flags": [],
    }
    _apply_live_confirmation(candidate, {
        "data_complete": True,
        "spread_bps": 2.0,
        "depth_imbalance": -0.12,
        "taker_buy_sell_ratio": 0.86,
        "oi_change_pct": 1.2,
        "funding_rate": 0.0001,
    })
    assert candidate["review_status"] == "REVIEW_CANDIDATE"
    assert candidate["status"] == "LIVE_CONFIRMED_REVIEW"
    assert candidate["advanced_confirmation"]["checks"]["depth_aligned"] is True


def test_causal_radar_rejects_an_intrabar_quote_that_chased_the_event():
    candidate = {
        "symbol": "TESTUSDT",
        "direction": "BULLISH",
        "score": 75,
        "risk_flags": [],
        "causal_radar": True,
        "market_context": {
            "actionability": {
                "state": "ACTIONABLE_NOW",
                "actionable": True,
                "aligned_event": {
                    "event_id": "fresh-bullish-event",
                    "direction": "BULLISH",
                    "break_level": 100.0,
                    "atr_at_event": 1.0,
                },
            },
        },
        "structure_confirmation": {"passed": True},
    }
    _apply_live_confirmation(candidate, {
        "data_complete": True,
        "current_price": 103.0,
        "spread_bps": 2.0,
        "depth_imbalance": 0.12,
        "taker_buy_sell_ratio": 1.2,
        "oi_change_pct": 1.2,
        "price_change_pct": 1.0,
        "funding_rate": 0.0001,
        "multi_venue": {"available": False, "flow_confirmed": False},
    })

    location = candidate["advanced_confirmation"]["market_story_live_location"]
    assert candidate["review_status"] == "WATCH_ONLY"
    assert candidate["status"] == "LIVE_CONFIRMATION_REJECTED"
    assert location["distance_atr"] == 3.0
    assert location["passed"] is False
    assert any("review location has moved away" in flag for flag in candidate["risk_flags"])


def test_radar_smc_confluence_is_bounded_context_not_a_signal_gate():
    candles = [
        Candle(
            open_time=index * 60_000,
            open=100.0 + index * 0.2,
            high=100.3 + index * 0.2,
            low=99.9 + index * 0.2,
            close=100.2 + index * 0.2,
            volume=1_000.0 + index,
            close_time=(index + 1) * 60_000,
            quote_volume=100_000.0,
            trade_count=100,
            taker_buy_base_volume=600.0,
            taker_buy_quote_volume=60_000.0,
        )
        for index in range(60)
    ]
    result = analyze_radar_structure_confluence(candles, "BULLISH")
    assert result["phase"] in {"MARKUP", "ACCUMULATION", "DISTRIBUTION", "MARKDOWN", "RANGING"}
    assert -8 <= result["score_adjustment"] <= 12
    assert "liquidity_sweep" in result
    assert "limitations" in result


def test_actionable_story_with_unconfirmed_playbook_is_not_called_a_story_contradiction():
    candles = [
        Candle(
            open_time=index * 60_000,
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=1_000.0,
            close_time=(index + 1) * 60_000,
            quote_volume=100_000.0,
            trade_count=100,
            taker_buy_base_volume=600.0,
            taker_buy_quote_volume=60_000.0,
        )
        for index in range(60)
    ]
    story = {
        "available": True,
        "current_state": "ACTIONABLE_NOW",
        "actionability": {"actionable": True},
        "structure_events": [],
        "liquidity_events": [],
    }
    context = {
        "direction": "LONG",
        "score": 80,
        "status": "SETUP_CANDIDATE",
        "contradictions": [],
        "coverage": {"complete": True, "available_domains": 4, "required_domains": 4},
        "components": {},
    }
    playbook = {
        "passed": False,
        "playbook": "NONE",
        "reason": "A range sweep is still required.",
        "reason_code": "RANGE_SWEEP_UNCONFIRMED",
        "directional_view": {
            "state": "ACTIONABLE_NOW",
            "actionable": True,
            "chase_prohibited": False,
        },
        "checks": {"structure_event_quality_ready": True},
        "selected_event": None,
    }

    with (
        patch("app.quant.momentum_scanner.classify_market_phase", side_effect=["ACCUMULATION", "RANGING"]),
        patch("app.quant.momentum_scanner.build_market_story", side_effect=[story, story]),
        patch("app.quant.momentum_scanner.score_market_context", return_value=context),
        patch("app.quant.momentum_scanner.evaluate_story_playbook", return_value=playbook),
    ):
        candidate = _candidate_from_causal_context(
            symbol="TESTUSDT",
            candles=candles,
            higher_candles=candles,
            quote_volume_24h=1_000_000.0,
        )

    assert candidate is not None
    assert "structure_playbook_range_sweep_unconfirmed" in candidate["risk_flags"]
    assert "market_story_actionable_now" not in candidate["risk_flags"]


def test_live_direction_change_rebuilds_the_story_playbook_and_old_flags():
    candles = [
        Candle(
            open_time=index * 60_000,
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=1_000.0,
            close_time=(index + 1) * 60_000,
            quote_volume=100_000.0,
            trade_count=100,
            taker_buy_base_volume=500.0,
            taker_buy_quote_volume=50_000.0,
        )
        for index in range(60)
    ]
    candidate = {
        "direction": "BULLISH",
        "price_change_pct": 1.0,
        "htf_direction": "BEARISH",
        "risk_flags": ["old_bullish_playbook_flag"],
        "higher_timeframe_story": {"available": True},
        "structure_confirmation": {"passed": True, "direction": "BULLISH"},
        "_candles": candles,
        "_causal_features": {
            "market_story": {"available": True},
            "market_structure": {
                "phase": "MARKDOWN",
                "higher_timeframe_phase": "MARKDOWN",
            },
            "vwap_context": {},
            "volume_profile": {},
        },
    }
    refreshed_context = {
        "direction": "SHORT",
        "score": 72,
        "status": "SETUP_CANDIDATE",
        "contradictions": [],
        "coverage": {"complete": True},
        "components": {},
    }
    refreshed_playbook = {
        "passed": False,
        "playbook": "NONE",
        "reason": "The bearish event is no longer actionable.",
        "reason_code": "STRUCTURE_EVENT_NOT_ACTIONABLE",
        "directional_view": {
            "state": "MISSED",
            "actionable": False,
        },
        "checks": {"structure_event_quality_ready": True},
        "selected_event": {"event_id": "bearish-event"},
    }
    execution_tape = {
        "actual_flow": {
            "available": True,
            "status": "SELLING_CONFIRMED",
            "signed_flow": -0.6,
        }
    }

    with (
        patch("app.quant.momentum_scanner.classify_positioning", return_value={"available": True}),
        patch("app.quant.momentum_scanner.score_market_context", return_value=refreshed_context),
        patch(
            "app.quant.momentum_scanner.evaluate_story_playbook",
            return_value=refreshed_playbook,
        ) as playbook_mock,
    ):
        _refresh_causal_context_from_live(
            candidate,
            {
                "data_complete": True,
                "taker_buy_sell_ratio": 0.7,
                "oi_change_pct": 1.0,
                "funding_rate": 0.0,
                "depth_imbalance": -0.2,
                "spread_bps": 1.0,
                "execution_tape": execution_tape,
            },
        )

    assert candidate["_causal_features"]["execution_tape"] is execution_tape
    assert candidate["_causal_features"]["multi_venue"] is execution_tape
    assert (
        playbook_mock.call_args.kwargs["primary_story"]
        is candidate["_causal_features"]["market_story"]
    )
    assert (
        candidate["_causal_features"]["positioning"]["available"] is True
    )
    assert playbook_mock.call_args.kwargs["direction"] == "BEARISH"
    assert candidate["direction"] == "BEARISH"
    assert candidate["structure_confirmation"]["passed"] is False
    assert candidate["risk_flags"] == ["market_story_missed"]
    assert "old_bullish_playbook_flag" not in candidate["risk_flags"]

def test_verify_setup_endpoint_uses_deterministic_evidence():
    from fastapi.testclient import TestClient
    from app.main import app
    from unittest.mock import AsyncMock, patch
    from time import time

    client = TestClient(app)

    now_ms = int(time() * 1000)
    candles = [
        Candle(
            open_time=now_ms - (101 - index) * 60_000,
            open=100.0 + index * 0.1,
            high=100.2 + index * 0.1,
            low=99.9 + index * 0.1,
            close=100.1 + index * 0.1,
            volume=1_000.0,
            close_time=now_ms - (100 - index) * 60_000,
            quote_volume=120_000.0 + index * 50.0,
            trade_count=100,
            taker_buy_base_volume=600.0,
            taker_buy_quote_volume=60_000.0,
        )
        for index in range(100)
    ]

    with patch("app.data_sources.binance_public.BinancePublicClient") as MockBinanceClient:
        mock_instance = MockBinanceClient.return_value
        mock_instance.klines = AsyncMock(side_effect=[candles, candles])
        mock_instance.order_book = AsyncMock(return_value={
            "bids": [[110.0, 500.0]], "asks": [[110.01, 500.0]],
        })

        response = client.post("/quant/verify-setup", json={"symbol": "BTCUSDT", "ltf": "15m", "htf": "4h"})
        assert response.status_code == 200
        assert [call.args[1] for call in mock_instance.klines.await_args_list] == ["15m", "4h"]
        assert [call.kwargs["limit"] for call in mock_instance.klines.await_args_list] == [200, 200]
        data = response.json()
        assert data["verdict"] in {"REVIEW_CANDIDATE", "WATCH_ONLY"}
        assert data["evaluation_mode"] == "causal_manual_review"
        assert "not a probability" in data["confidence_label"].lower()
        assert "liquidity" in data
        assert "spread_pct" in data["liquidity"]
        assert "bid_depth_notional" in data["liquidity"]
        assert "token_health" not in data
