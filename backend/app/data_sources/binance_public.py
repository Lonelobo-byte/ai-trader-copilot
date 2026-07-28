from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from time import time
from typing import Any

import httpx
from .http_client import get_http_client

logger = logging.getLogger(__name__)

SUPPORTED_INTERVALS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
}


@dataclass(frozen=True)
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int
    quote_volume: float
    trade_count: int
    taker_buy_base_volume: float
    taker_buy_quote_volume: float

    @property
    def taker_sell_base_volume(self) -> float:
        return max(self.volume - self.taker_buy_base_volume, 0.0)

    @property
    def taker_buy_ratio(self) -> float:
        if self.volume <= 0:
            return 0.0
        return self.taker_buy_base_volume / self.volume

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["taker_sell_base_volume"] = self.taker_sell_base_volume
        data["taker_buy_ratio"] = self.taker_buy_ratio
        data["is_bullish"] = self.is_bullish
        return data


def interval_seconds(interval: str) -> int:
    if interval not in SUPPORTED_INTERVALS:
        supported = ", ".join(sorted(SUPPORTED_INTERVALS))
        raise ValueError(f"Unsupported interval '{interval}'. Supported: {supported}")
    return SUPPORTED_INTERVALS[interval]


def parse_kline(raw: list[Any]) -> Candle:
    return Candle(
        open_time=int(raw[0]),
        open=float(raw[1]),
        high=float(raw[2]),
        low=float(raw[3]),
        close=float(raw[4]),
        volume=float(raw[5]),
        close_time=int(raw[6]),
        quote_volume=float(raw[7]),
        trade_count=int(raw[8]),
        taker_buy_base_volume=float(raw[9]),
        taker_buy_quote_volume=float(raw[10]),
    )


class BinancePublicClient:
    def __init__(
        self,
        base_url: str = "https://api.binance.com",
        timeout_seconds: float = 10.0,
        market: str = "spot",
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = httpx.Timeout(timeout_seconds)
        if market not in {"spot", "futures"}:
            raise ValueError("market must be 'spot' or 'futures'")
        self.market = market
        self.is_futures_mode = (market == "futures")

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        current_path = path.replace("/api/v3/", "/fapi/v1/") if self.market == "futures" else path
        url = f"{self.base_url}{current_path}"
        client = await get_http_client()
        try:
            response = await client.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            if self.market == "spot" and exc.response is not None and exc.response.status_code == 400:
                futures_base_url = "https://fapi.binance.com"
                futures_path = path.replace("/api/v3/", "/fapi/v1/")
                futures_url = f"{futures_base_url}{futures_path}"
                try:
                    futures_resp = await client.get(futures_url, params=params, timeout=self.timeout)
                    if futures_resp.status_code == 200:
                        self.market = "futures"
                        self.base_url = futures_base_url
                        self.is_futures_mode = True
                        logger.info(
                            f"Symbol '{params.get('symbol')}' returned 400 on Binance Spot. "
                            f"Automatically fell back to Binance Futures."
                        )
                        return futures_resp.json()
                except Exception:
                    pass
            raise

    async def klines(self, symbol: str, interval: str = "15m", limit: int = 200, **kwargs) -> list[Candle]:
        interval_seconds(interval)
        safe_limit = max(50, min(int(limit), 1000))
        params = {"symbol": symbol.upper(), "interval": interval, "limit": safe_limit}
        params.update(kwargs)
        data = await self._get(
            "/api/v3/klines",
            params,
        )
        return [parse_kline(item) for item in data]

    async def order_book(self, symbol: str, limit: int = 100) -> dict[str, Any]:
        safe_limit = max(5, min(int(limit), 5000))
        data = await self._get(
            "/api/v3/depth",
            {"symbol": symbol.upper(), "limit": safe_limit},
        )
        return {
            "last_update_id": data.get("lastUpdateId"),
            "bids": [[float(price), float(qty)] for price, qty in data.get("bids", [])],
            "asks": [[float(price), float(qty)] for price, qty in data.get("asks", [])],
        }

    async def recent_trades(self, symbol: str, limit: int = 100) -> list[dict[str, Any]]:
        """Fetch recent trades for trade-flow analysis."""
        safe_limit = max(1, min(int(limit), 1000))
        data = await self._get(
            "/api/v3/trades",
            {"symbol": symbol.upper(), "limit": safe_limit},
        )
        return [
            {
                "price": float(t["price"]),
                "qty": float(t["qty"]),
                "quoteQty": float(t["quoteQty"]),
                "time": int(t["time"]),
                "isBuyerMaker": t["isBuyerMaker"],
            }
            for t in data
        ]

    async def agg_trades(self, symbol: str, limit: int = 200) -> list[dict[str, Any]]:
        """Fetch aggregated trades for volume-at-price profiling."""
        safe_limit = max(1, min(int(limit), 1000))
        data = await self._get(
            "/api/v3/aggTrades",
            {"symbol": symbol.upper(), "limit": safe_limit},
        )
        return [
            {
                "price": float(t["p"]),
                "qty": float(t["q"]),
                "time": int(t["T"]),
                "isBuyerMaker": t["m"],
            }
            for t in data
        ]

    async def ticker_24hr(self, symbol: str) -> dict[str, Any]:
        data = await self._get("/api/v3/ticker/24hr", {"symbol": symbol.upper()})
        float_keys = [
            "priceChange",
            "priceChangePercent",
            "weightedAvgPrice",
            "prevClosePrice",
            "lastPrice",
            "lastQty",
            "bidPrice",
            "bidQty",
            "askPrice",
            "askQty",
            "openPrice",
            "highPrice",
            "lowPrice",
            "volume",
            "quoteVolume",
        ]
        parsed: dict[str, Any] = {"symbol": data.get("symbol", symbol.upper())}
        for key in float_keys:
            if key in data:
                parsed[key] = float(data[key])
        for key in ["openTime", "closeTime", "firstId", "lastId", "count"]:
            if key in data:
                parsed[key] = int(data[key])
        return parsed


def completed_candles(candles: list[Candle]) -> list[Candle]:
    now_ms = int(time() * 1000)
    closed = [candle for candle in candles if candle.close_time <= now_ms]
    return closed if closed else candles[:-1]
