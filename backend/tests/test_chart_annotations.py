from app.data_sources.binance_public import Candle
from app.quant.chart_annotations import build_hawk_eye_chart_contract


def _candles(count: int = 6) -> list[Candle]:
    rows = []
    for index in range(count):
        open_price = 100.0 + index
        rows.append(
            Candle(
                open_time=1_700_000_000_000 + index * 60_000,
                open=open_price,
                high=open_price + 1.5,
                low=open_price - 1.0,
                close=open_price + 0.5,
                volume=1000.0 + index,
                close_time=1_700_000_059_999 + index * 60_000,
                quote_volume=100_000.0,
                trade_count=100,
                taker_buy_base_volume=550.0,
                taker_buy_quote_volume=55_000.0,
            )
        )
    return rows


def _event() -> dict:
    return {
        "detected": True,
        "event_id": "BOS:BEARISH:1",
        "type": "BOS",
        "direction": "BEARISH",
        "event_index": 3,
        "event_open_time": 1_700_000_180_000,
        "event_close": 103.5,
        "break_level": 103.0,
        "state": "PULLBACK_REQUIRED",
        "actionable": False,
        "chase_prohibited": True,
        "age_bars": 2,
        "last_retest_index": 4,
        "invalidation_level": 104.2,
        "campaign_id": "BEARISH:origin",
        "campaign_origin_index": 2,
        "campaign_origin_price": 103.5,
        "campaign_atr": 1.0,
        "campaign_distance_atr_current": 3.4,
        "campaign_maturity": "PULLBACK_REQUIRED",
        "entry_timing": "WAIT_FOR_PULLBACK",
        "reason": "A mature bearish campaign requires a retest.",
    }


def _contract(mode: str = "snapshot", execution_permitted: bool = False) -> dict:
    event = _event()
    return build_hawk_eye_chart_contract(
        _candles(),
        symbol="BTCUSDT",
        timeframe="1m",
        story={
            "current_state": "PULLBACK_REQUIRED",
            "structure_events": [event],
            "liquidity_events": [],
            "latest_event": event,
            "actionability": {
                "status": "PULLBACK_REQUIRED",
                "entry_timing": "WAIT_FOR_PULLBACK",
                "campaign_maturity": "PULLBACK_REQUIRED",
            },
        },
        liquidity_map={
            "pools": [
                {"kind": "equal_highs", "side": "above", "price": 108.0, "touches": 2},
            ]
        },
        trade_setup={
            "side": "SHORT",
            "execution_permitted": execution_permitted,
            "market_story": {"selected_event": event},
            "entry": {"mode": "RETEST", "reference": 103.0, "zone_low": 102.8, "zone_high": 103.2},
            "stop": {"selected": 104.5},
            "targets": {"tp1_1r": 101.0, "tp2_2r": 99.5},
        },
        signal_monitor={"action": "WATCH", "status": "LIVE_RESEARCH_MONITORING", "side": "SHORT"},
        live_confirmation={"passed": False, "reason": "Live sell flow is not confirmed."},
        execution_tape={"actual_flow": {"available": True, "status": "BALANCED", "bias": "NEUTRAL", "confidence": "MEDIUM"}},
        mode=mode,
    )


def test_snapshot_contains_bounded_history_and_causal_annotations() -> None:
    contract = _contract()

    assert contract["schema_version"] == "hawk_eye_chart.v1"
    assert contract["mode"] == "snapshot"
    assert len(contract["candles"]) == 6
    assert contract["annotations"]["selected_event"]["id"] == "BOS:BEARISH:1"
    assert contract["annotations"]["campaign"]["origin_time"] == _candles()[2].open_time
    assert contract["annotations"]["entry_zone"]["execution_permitted"] is False
    assert contract["annotations"]["liquidity_levels"][0]["kind"] == "EQUAL_HIGHS"
    assert contract["decision"]["flow"]["available"] is True


def test_websocket_modes_send_only_required_candle_delta() -> None:
    assert len(_contract("delta")["candles"]) == 1
    assert len(_contract("rollover")["candles"]) == 2


def test_execution_levels_are_hidden_until_plan_is_permitted() -> None:
    assert _contract(execution_permitted=False)["annotations"]["execution_levels"] == []
    levels = _contract(execution_permitted=True)["annotations"]["execution_levels"]
    assert [level["kind"] for level in levels] == ["STOP", "TP1", "TP2"]
