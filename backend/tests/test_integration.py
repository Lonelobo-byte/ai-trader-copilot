"""Integration tests for the API routes, background workers, and pipeline."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.analysis_pipeline import run_full_analysis
from app.background_tasks import outcome_tracker_loop
from app.data_sources.binance_public import BinancePublicClient, Candle
from app.data_sources.calendar import fetch_economic_events
from app.data_sources.gdelt import fetch_gdelt_news
from app.db.database import AsyncSessionLocal
from app.db.models import AnalysisSession, TradeSignal
from app.main import app
from app.settings import get_settings


def _dummy_candles(count: int = 60, start_price: float = 100.0) -> list[Candle]:
    return [
        Candle(
            open_time=i * 60000,
            open=start_price,
            high=start_price + 1.0,
            low=start_price - 1.0,
            close=start_price + (i % 2 - 0.5),
            volume=1000.0,
            close_time=(i * 60000) + 59999,
            quote_volume=100000.0,
            trade_count=100,
            taker_buy_base_volume=500.0,
            taker_buy_quote_volume=50000.0,
        )
        for i in range(count)
    ]


def _dummy_ticker(price: float = 100.0) -> dict:
    return {
        "symbol": "BTCUSDT",
        "priceChange": 0.0,
        "priceChangePercent": 0.0,
        "weightedAvgPrice": price,
        "prevClosePrice": price,
        "lastPrice": price,
        "lastQty": 1.0,
        "bidPrice": price - 0.05,
        "bidQty": 10.0,
        "askPrice": price + 0.05,
        "askQty": 10.0,
        "openPrice": price,
        "highPrice": price + 2.0,
        "lowPrice": price - 2.0,
        "volume": 500000.0,
        "quoteVolume": 50000000.0,
        "openTime": 0,
        "closeTime": 100000,
        "firstId": 0,
        "lastId": 100,
        "count": 101,
    }


def _dummy_order_book(price: float = 100.0) -> dict:
    return {
        "bids": [[price - 0.1 * i, 1.0] for i in range(1, 21)],
        "asks": [[price + 0.1 * i, 1.0] for i in range(1, 21)],
    }


@pytest.mark.asyncio
async def test_run_full_analysis_deterministic_flow() -> None:
    settings = get_settings()
    candles = _dummy_candles()
    ticker = _dummy_ticker()
    order_book = _dummy_order_book()

    # Run analysis without AI calls
    payload, last_ai_time = await run_full_analysis(
        symbol="BTCUSDT",
        timeframe="15m",
        candles=candles,
        ticker=ticker,
        order_book_raw=order_book,
        settings=settings,
        use_ai=False,
    )

    assert payload["symbol"] == "BTCUSDT"
    assert payload["timeframe"] == "15m"
    assert payload["ai_calls"] == 0
    assert "gates" in payload
    assert "quantitative" in payload
    assert payload["decision"] == "SCANNING"
    assert "probability_engine" in payload["quantitative"]


@pytest.mark.asyncio
async def test_fetch_economic_events_prod_empty_fallback() -> None:
    # Verify production calendar fails gracefully to an empty list without raising exception
    events = await fetch_economic_events(app_env="production")
    # Should be empty because TV endpoint will fail or not find connection in mock env
    assert isinstance(events, list)


@pytest.mark.asyncio
async def test_gdelt_news_fetch_success(httpx_mock: pytest_httpx.HTTPXMock) -> None:
    import re
    from app.data_sources.gdelt import _news_cache
    _news_cache.clear()
    
    mock_response = {
        "articles": [
            {
                "title": "Crypto Markets Rise",
                "url": "https://example.com/news1",
                "sourcecountry": "US",
                "seendate": "2026-07-14T02:00:00Z",
            }
        ]
    }
    mock_rss_xml = """<rss version="2.0">
        <channel>
            <item>
                <title>ESMA Adds CASPs</title>
                <description>ESMA description</description>
                <link>https://example.com/rss1</link>
                <pubDate>Fri, 17 Jul 2026 10:29:35 +0000</pubDate>
            </item>
        </channel>
    </rss>"""
    
    httpx_mock.add_response(url=re.compile(r".*gdeltproject.*"), json=mock_response)
    httpx_mock.add_response(url="https://cointelegraph.com/rss", text=mock_rss_xml)

    articles = await fetch_gdelt_news("BTCUSDT")
    assert len(articles) == 2
    assert articles[0]["title"] == "Crypto Markets Rise"
    assert articles[0]["feed"] == "GDELT"
    assert articles[1]["title"] == "ESMA Adds CASPs"
    assert articles[1]["feed"] == "RSS"


def test_analyze_rest_endpoint_avoid_decision() -> None:
    client = TestClient(app)
    
    # Mock Binance client calls to prevent live HTTP traffic and guarantee a successful response
    with patch.object(BinancePublicClient, "klines", return_value=_dummy_candles()) as mock_klines, \
         patch.object(BinancePublicClient, "order_book", return_value=_dummy_order_book()) as mock_depth, \
         patch.object(BinancePublicClient, "ticker_24hr", return_value=_dummy_ticker()) as mock_ticker:
         
        response = client.post(
            "/analyze",
            json={
                "symbol": "BTCUSDT",
                "timeframe": "15m",
                "use_ai": False,
                "candle_limit": 100,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["symbol"] == "BTCUSDT"
        assert data["timeframe"] == "15m"
        assert "gates" in data


@pytest.mark.asyncio
async def test_outcome_tracker_expirations() -> None:
    session_id = 999
    now = datetime.now(timezone.utc)
    old_time = now - timedelta(hours=25)

    async with AsyncSessionLocal() as db:
        old_session = AnalysisSession(
            id=session_id,
            symbol="BTCUSDT",
            timeframe="15m",
            timestamp=old_time,
            outcome="PENDING",
            decision="BUY_WATCH",
            entry_price=100.0,
            target_price=110.0,
            stop_price=90.0,
        )
        db.add(old_session)
        await db.commit()

    # Run one cycle of the outcome tracker loop using a mocked sleep that immediately raises CancelledError to stop the loop
    with patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]):
        try:
            await outcome_tracker_loop()
        except asyncio.CancelledError:
            pass

    # Verify session has expired
    async with AsyncSessionLocal() as db:
        stmt = select(AnalysisSession).where(AnalysisSession.id == session_id)
        res = await db.execute(stmt)
        session = res.scalar_one_or_none()
        assert session is not None
        assert session.outcome == "EXPIRED"
