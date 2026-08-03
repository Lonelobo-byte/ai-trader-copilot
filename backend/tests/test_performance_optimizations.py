from __future__ import annotations

import asyncio
from pathlib import Path
from time import sleep

import pytest

from app.data_sources import data_aggregator
from app import analysis_pipeline
from app.routes.radar import _radar_card_row
from app.settings import Settings


@pytest.mark.asyncio
async def test_market_intelligence_starts_all_source_groups_concurrently(monkeypatch) -> None:
    """Core, MTF, derivatives and slow context must share one wait window."""
    started: list[str] = []
    release = asyncio.Event()

    async def wait_for_release(name: str, result):
        started.append(name)
        await release.wait()
        return result

    class FakeMarket:
        async def klines(self, symbol, timeframe, limit=200):
            return await wait_for_release(f"klines:{timeframe}", [])

        async def ticker_24hr(self, symbol):
            return await wait_for_release("ticker", {})

        async def order_book(self, symbol, limit=100):
            return await wait_for_release("order_book", {"bids": [], "asks": []})

        async def recent_trades(self, symbol, limit=200):
            return await wait_for_release("trades", [])

    class FakeFutures:
        async def get_funding_rate(self, symbol):
            return await wait_for_release("funding", {})

        async def get_open_interest(self, symbol):
            return await wait_for_release("open_interest", {})

    monkeypatch.setattr(data_aggregator, "BinancePublicClient", lambda *args, **kwargs: FakeMarket())
    monkeypatch.setattr(data_aggregator, "BinanceFuturesClient", lambda *args, **kwargs: FakeFutures())
    monkeypatch.setattr(
        data_aggregator,
        "fetch_derivatives_intelligence",
        lambda symbol: wait_for_release("derivatives", {}),
    )
    monkeypatch.setattr(data_aggregator, "fetch_gdelt_news", lambda *args: wait_for_release("news", []))
    monkeypatch.setattr(data_aggregator, "fetch_global_news", lambda *args: wait_for_release("global_news", []))
    monkeypatch.setattr(data_aggregator, "fetch_macro_data", lambda: wait_for_release("macro", {}))
    monkeypatch.setattr(
        data_aggregator,
        "fetch_sentiment_snapshot",
        lambda: wait_for_release("sentiment", {"fear_greed": {"available": False}}),
    )
    monkeypatch.setattr(data_aggregator, "fetch_economic_events", lambda *args: wait_for_release("calendar", []))
    monkeypatch.setattr(
        data_aggregator,
        "fetch_global_liquidity_index",
        lambda: wait_for_release("global_liquidity", {"available": False}),
    )
    monkeypatch.setattr(data_aggregator, "_cached", lambda *args: None)

    task = asyncio.create_task(
        data_aggregator.fetch_market_intelligence("BTCUSDT", "15m", Settings())
    )
    try:
        for _ in range(100):
            if len(started) == 16:
                break
            await asyncio.sleep(0.001)
        assert len(started) == 16, f"Only these source tasks started together: {started}"
    finally:
        release.set()
    result = await task
    assert result["symbol"] == "BTCUSDT"


def test_public_radar_card_contract_omits_full_dossier_payload() -> None:
    row = {
        "symbol": "BTCUSDT",
        "score": 82,
        "direction": "BULLISH",
        "review_status": "REVIEW_CANDIDATE",
        "market_context": {
            "status": "SETUP_CANDIDATE",
            "actionability": {
                "state": "RETESTING",
                "actionable": True,
                "selected_event": {
                    "event_id": "bos-1",
                    "type": "BOS",
                    "direction": "BULLISH",
                    "age_bars": 1,
                    "break_level": 100.0,
                    "invalidation_level": 98.0,
                    "large_internal_debug_blob": "x" * 10_000,
                },
            },
            "components": {"order_flow": {"available": True, "bias": "BULLISH", "raw": [1] * 500}},
        },
        "market_story": {
            "available": True,
            "what_happened": "Structure broke.",
            "latest_event": {"event_id": "bos-1", "type": "BOS", "direction": "BULLISH"},
            "all_events": [{"large": "history"}] * 100,
        },
        "market_structure": {"phase": "MARKUP", "story": {"duplicate": True}},
        "advanced_confirmation": {
            "state": "PASSED",
            "actual_flow_evidence": {"available": True, "status": "BUYING_CONFIRMED", "raw_trades": [1] * 500},
            "checks": {"large": "private dossier detail"},
        },
        "liquidity_map": {"large": [1] * 1000},
        "volume_profile": {"large": [1] * 1000},
        "higher_timeframe_story": {"large": [1] * 1000},
    }

    card = _radar_card_row(row)

    assert card["symbol"] == "BTCUSDT"
    assert card["market_context"]["actionability"]["selected_event"]["event_id"] == "bos-1"
    assert card["advanced_confirmation"]["actual_flow_evidence"]["status"] == "BUYING_CONFIRMED"
    assert "liquidity_map" not in card
    assert "volume_profile" not in card
    assert "higher_timeframe_story" not in card
    assert "large_internal_debug_blob" not in str(card)
    assert "raw_trades" not in str(card)


@pytest.mark.asyncio
async def test_identical_live_snapshots_share_quant_feature_computation(monkeypatch) -> None:
    analysis_pipeline._FEATURE_CACHE.clear()
    analysis_pipeline._FEATURE_INFLIGHT.clear()
    calls = 0

    def fake_compute(intelligence):
        nonlocal calls
        calls += 1
        sleep(0.02)
        return {"current_price": 101.0, "nested": {"value": 1}}

    monkeypatch.setattr(analysis_pipeline, "compute_quant_features", fake_compute)
    intelligence = {
        "candles": [{"open_time": 1, "open": 100, "high": 102, "low": 99, "close": 101, "volume": 10}],
        "ticker": {"lastPrice": 101.0},
        "order_book": {"last_update_id": 8, "bids": [[100.9, 2]], "asks": [[101.1, 3]]},
        "execution_tape": {"actual_flow": {"status": "READY", "bias": "BULLISH", "net_delta_usd": 10}},
    }

    first, second = await asyncio.gather(
        analysis_pipeline._compute_quant_features_shared("BTCUSDT", "15m", intelligence),
        analysis_pipeline._compute_quant_features_shared("BTCUSDT", "15m", intelligence),
    )
    first["nested"]["value"] = 99
    third = await analysis_pipeline._compute_quant_features_shared("BTCUSDT", "15m", intelligence)

    assert calls == 1
    assert second["nested"]["value"] == 1
    assert third["nested"]["value"] == 1
    analysis_pipeline._FEATURE_CACHE.clear()
    analysis_pipeline._FEATURE_INFLIGHT.clear()


def test_frontend_assets_are_lazy_shared_and_cacheable() -> None:
    root = Path(__file__).resolve().parents[2]
    index = (root / "backend/app/static/index.html").read_text(encoding="utf-8")
    radar = (root / "backend/app/static/radar.html").read_text(encoding="utf-8")
    app = (root / "backend/app/static/app.js").read_text(encoding="utf-8")
    hawk = (root / "backend/app/static/hawk-chart.js").read_text(encoding="utf-8")
    caddy = (root / "Caddyfile").read_text(encoding="utf-8")

    assert '<script src="https://cdn.jsdelivr.net/npm/echarts' not in index
    assert "loadResearchScript('hawk-chart.js" in index
    assert "echarts.min.js" in hawk and "script.async = true" in hawk
    assert "streamGeneration" in app and "payloadMatchesActiveStream" in app
    assert "display_reason" in hawk and "state.candles.clear()" in hawk
    assert 'ambient-motion.js?v=' in index
    assert 'ambient-motion.js?v=' in radar
    assert "particleCount = 70" not in index + radar
    assert "renderLegacyBreakoutTable" not in radar
    assert "renderLegacyCausalTable" not in radar
    assert "handle_path /static/*" in caddy
    assert "max-age=31536000, immutable" in caddy
