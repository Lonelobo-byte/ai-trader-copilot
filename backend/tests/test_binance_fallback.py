"""Tests for BinancePublicClient futures fallback when symbol returns 400 Bad Request on Spot."""
from __future__ import annotations

import pytest
import httpx
from unittest.mock import patch, AsyncMock

from app.data_sources.binance_public import BinancePublicClient


@pytest.mark.asyncio
async def test_binance_spot_to_futures_fallback(httpx_mock):
    # Mock spot endpoint returning 400 Bad Request (Invalid symbol)
    httpx_mock.add_response(
        method="GET",
        url="https://api.binance.com/api/v3/klines?symbol=HYPEUSDT&interval=15m&limit=200",
        status_code=400,
        json={"code": -1121, "msg": "Invalid symbol."},
    )

    # Mock futures endpoint returning 200 OK with valid klines data
    dummy_kline = [
        1700000000000, "10.0", "11.0", "9.5", "10.5", "100.0",
        1700000900000, "1050.0", 50, "50.0", "525.0", "0"
    ]
    httpx_mock.add_response(
        method="GET",
        url="https://fapi.binance.com/fapi/v1/klines?symbol=HYPEUSDT&interval=15m&limit=200",
        status_code=200,
        json=[dummy_kline],
    )

    client = BinancePublicClient()
    assert client.market == "spot"
    assert getattr(client, "is_futures_mode", False) is False

    candles = await client.klines("HYPEUSDT", "15m", limit=200)

    assert len(candles) == 1
    assert candles[0].close == 10.5
    assert client.market == "futures"
    assert client.base_url == "https://fapi.binance.com"
    assert client.is_futures_mode is True


@pytest.mark.asyncio
async def test_binance_invalid_symbol_raises_400(httpx_mock):
    # Mock spot returning 400
    httpx_mock.add_response(
        method="GET",
        url="https://api.binance.com/api/v3/klines?symbol=NONEXISTENT&interval=15m&limit=200",
        status_code=400,
        json={"code": -1121, "msg": "Invalid symbol."},
    )

    # Mock futures also returning 400
    httpx_mock.add_response(
        method="GET",
        url="https://fapi.binance.com/fapi/v1/klines?symbol=NONEXISTENT&interval=15m&limit=200",
        status_code=400,
        json={"code": -1121, "msg": "Invalid symbol."},
    )

    client = BinancePublicClient()
    with pytest.raises(httpx.HTTPStatusError):
        await client.klines("NONEXISTENT", "15m", limit=200)
