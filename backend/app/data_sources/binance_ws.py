from __future__ import annotations

import asyncio
import json
import logging
from time import monotonic
from typing import Any, AsyncGenerator

import websockets

from app.data_sources.binance_public import BinancePublicClient, Candle, parse_kline

logger = logging.getLogger(__name__)


class BinanceWSSubscriber:
    def __init__(self, symbol: str, timeframe: str, settings: Any):
        self.symbol = symbol.upper().strip()
        self.timeframe = timeframe
        self.settings = settings
        self.rest_client = BinancePublicClient(settings.binance_public_base_url)
        self.history_candles: list[Candle] = []
        self.latest_ticker: dict[str, Any] = {}
        self.latest_order_book: dict[str, Any] = {"bids": [], "asks": []}
        self.websocket_url = (
            f"{settings.binance_stream_base_url}/stream?streams="
            f"{self.symbol.lower()}@kline_{self.timeframe}/"
            f"{self.symbol.lower()}@ticker/"
            f"{self.symbol.lower()}@depth20@1000ms"
        )
        self.min_emit_seconds = 2.0

    async def initialize(self) -> None:
        try:
            logger.info(f"Initializing rest data bootstrap for {self.symbol}...")
            self.history_candles = await self.rest_client.klines(self.symbol, self.timeframe, limit=200)
            self.latest_ticker = await self.rest_client.ticker_24hr(self.symbol)
            self.latest_order_book = await self.rest_client.order_book(self.symbol, limit=100)
            
            # Switch to Futures stream if client initialized in Futures mode
            if getattr(self.rest_client, "is_futures_mode", False):
                logger.info(f"Detected {self.symbol} as a Futures contract. Switching websocket stream URL.")
                self.websocket_url = (
                    f"wss://fstream.binance.com/stream?streams="
                    f"{self.symbol.lower()}@kline_{self.timeframe}/"
                    f"{self.symbol.lower()}@ticker/"
                    f"{self.symbol.lower()}@depth20@1000ms"
                )
            logger.info(f"Successfully bootstrapped initial REST data for {self.symbol}.")
        except Exception as e:
            logger.error(f"Failed to initialize WS REST bootstrap for {self.symbol}: {e}")
            raise

    async def start(self) -> AsyncGenerator[dict[str, Any], None]:
        await self.initialize()

        async for websocket in websockets.connect(self.websocket_url):
            try:
                # Send the initial bootstrap data to start the client view immediately
                yield {
                    "type": "init",
                    "candles": [c.to_dict() for c in self.history_candles],
                    "ticker": self.latest_ticker,
                    "order_book": self.latest_order_book,
                }

                last_emit_at = monotonic()
                while True:
                    message = await websocket.recv()
                    event_data = json.loads(message)
                    stream = event_data.get("stream", "")
                    data = event_data.get("data", {})

                    state_changed = False
                    new_candle_closed = False

                    if "@kline_" in stream:
                        k = data.get("k", {})
                        raw_kline = [
                            k.get("t"),
                            k.get("o"),
                            k.get("h"),
                            k.get("l"),
                            k.get("c"),
                            k.get("v"),
                            k.get("T"),
                            k.get("q"),
                            k.get("n"),
                            k.get("V"),
                            k.get("Q"),
                            "0",
                        ]
                        candle = parse_kline(raw_kline)

                        if self.history_candles:
                            if self.history_candles[-1].open_time == candle.open_time:
                                # Overwrite active candle with latest tick info
                                self.history_candles[-1] = candle
                            else:
                                # Prior candle is closed, this is a new tick on a new candle
                                new_candle_closed = True
                                self.history_candles.append(candle)
                                if len(self.history_candles) > 200:
                                    self.history_candles.pop(0)
                        else:
                            self.history_candles.append(candle)
                        state_changed = True

                    elif "@ticker" in stream:
                        self.latest_ticker = {
                            "symbol": data.get("s"),
                            "priceChange": float(data.get("p", 0.0)),
                            "priceChangePercent": float(data.get("P", 0.0)),
                            "weightedAvgPrice": float(data.get("w", 0.0)),
                            "prevClosePrice": float(data.get("x", 0.0)),
                            "lastPrice": float(data.get("c", 0.0)),
                            "lastQty": float(data.get("Q", 0.0)),
                            "bidPrice": float(data.get("b", 0.0)),
                            "bidQty": float(data.get("B", 0.0)),
                            "askPrice": float(data.get("a", 0.0)),
                            "askQty": float(data.get("A", 0.0)),
                            "openPrice": float(data.get("o", 0.0)),
                            "highPrice": float(data.get("h", 0.0)),
                            "lowPrice": float(data.get("l", 0.0)),
                            "volume": float(data.get("v", 0.0)),
                            "quoteVolume": float(data.get("q", 0.0)),
                            "openTime": int(data.get("O", 0)),
                            "closeTime": int(data.get("C", 0)),
                            "firstId": int(data.get("F", 0)),
                            "lastId": int(data.get("L", 0)),
                            "count": int(data.get("n", 0)),
                        }
                        state_changed = True

                    elif "@depth" in stream:
                        self.latest_order_book = {
                            "last_update_id": data.get("lastUpdateId") or data.get("u"),
                            "bids": [[float(p), float(q)] for p, q in data.get("bids", [])],
                            "asks": [[float(p), float(q)] for p, q in data.get("asks", [])],
                        }
                        state_changed = True

                    should_emit = (
                        state_changed
                        and (new_candle_closed or monotonic() - last_emit_at >= self.min_emit_seconds)
                    )

                    if should_emit:
                        last_emit_at = monotonic()
                        yield {
                            "type": "update",
                            "candles": [c.to_dict() for c in self.history_candles],
                            "ticker": self.latest_ticker,
                            "order_book": self.latest_order_book,
                            "new_candle_closed": new_candle_closed,
                        }

            except websockets.ConnectionClosed:
                logger.warning("Binance WS stream disconnected, reconnecting in 2s...")
                await asyncio.sleep(2)
                continue
            except Exception as e:
                logger.error(f"Error in Binance WS connection loop: {e}", exc_info=True)
                await asyncio.sleep(2)
                continue
