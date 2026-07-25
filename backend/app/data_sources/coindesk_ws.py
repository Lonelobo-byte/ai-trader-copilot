"""CoinDesk/CCData WebSocket Subscriber for L2 Order Book depth streaming."""
from __future__ import annotations

import asyncio
import json
import logging
from time import monotonic
from typing import Any, AsyncGenerator, Dict, List

import websockets

from app.data_sources.binance_public import BinancePublicClient, Candle, parse_kline

logger = logging.getLogger(__name__)


class CoindeskWSSubscriber:
    def __init__(self, symbol: str, timeframe: str, settings: Any):
        self.symbol = symbol.upper().strip()
        self.timeframe = timeframe
        self.settings = settings
        self.rest_client = BinancePublicClient(settings.binance_public_base_url)
        self.history_candles: list[Candle] = []
        self.latest_ticker: dict[str, Any] = {}
        
        # Aggregated L2 book
        self.bids_cache: Dict[float, float] = {}
        self.asks_cache: Dict[float, float] = {}
        self.latest_order_book: dict[str, Any] = {"bids": [], "asks": []}
        
        # Format connection URL
        api_key = settings.coindesk_api_key
        if api_key:
            self.websocket_url = f"{settings.coindesk_stream_base_url}/?api_key={api_key}"
        else:
            # Fallback to public cryptocompare endpoint
            self.websocket_url = "wss://streamer.cryptocompare.com/v2"

        self.min_emit_seconds = 2.0

    async def initialize(self) -> None:
        try:
            logger.info(f"Initializing rest data bootstrap for {self.symbol} via CoinDesk subscriber...")
            self.history_candles = await self.rest_client.klines(self.symbol, self.timeframe, limit=200)
            self.latest_ticker = await self.rest_client.ticker_24hr(self.symbol)
            self.latest_order_book = await self.rest_client.order_book(self.symbol, limit=100)
            
            # Seed our L2 cache with initial REST order book
            for p, q in self.latest_order_book["bids"]:
                self.bids_cache[float(p)] = float(q)
            for p, q in self.latest_order_book["asks"]:
                self.asks_cache[float(p)] = float(q)
                
            logger.info("Successfully bootstrapped initial order book cache.")
        except Exception as e:
            logger.error(f"Failed to bootstrap data for CoinDesk WS: {e}")
            raise

    async def start(self) -> AsyncGenerator[dict[str, Any], None]:
        await self.initialize()

        # Parse symbol to Base/Quote currencies
        # Assume standard USDT pairs
        if self.symbol.endswith("USDT"):
            base = self.symbol[:-4]
            quote = "USDT"
        elif self.symbol.endswith("BTC"):
            base = self.symbol[:-3]
            quote = "BTC"
        else:
            base = self.symbol
            quote = "USD"

        # Subscription message for CCData order book channel (TYPE 2)
        # Channel format: 2~Binance~{base}~{quote}
        sub_msg = {
            "action": "SubAdd",
            "subs": [f"2~Binance~{base}~{quote}"]
        }

        async for websocket in websockets.connect(self.websocket_url):
            try:
                # Send subscription payload
                await websocket.send(json.dumps(sub_msg))
                logger.info(f"Subscribed to CoinDesk order book stream for {base}-{quote}")

                # Send initial state
                yield {
                    "type": "init",
                    "candles": [c.to_dict() for c in self.history_candles],
                    "ticker": self.latest_ticker,
                    "order_book": self.latest_order_book,
                }

                last_emit_at = monotonic()
                while True:
                    message = await websocket.recv()
                    data = json.loads(message)
                    event_type = str(data.get("TYPE", ""))
                    
                    state_changed = False
                    new_candle_closed = False

                    # Check CCData order book updates (TYPE 2)
                    if event_type == "2":
                        price = float(data.get("P", 0.0))
                        side = int(data.get("SIDE", 0))  # 0=Bid, 1=Ask
                        action = int(data.get("ACTION", 0))  # 0=Add/Update, 1=Delete
                        volume = float(data.get("VOLUME", 0.0))

                        # Apply updates to cache
                        cache = self.bids_cache if side == 0 else self.asks_cache
                        if action == 1 or volume <= 0:
                            cache.pop(price, None)
                        else:
                            cache[price] = volume

                        # Update last price in ticker for real-time updates
                        if price > 0:
                            self.latest_ticker["lastPrice"] = price
                        
                        state_changed = True

                        # Rebuild L2 order book representation
                        sorted_bids = sorted(self.bids_cache.items(), key=lambda x: x[0], reverse=True)[:50]
                        sorted_asks = sorted(self.asks_cache.items(), key=lambda x: x[0])[:50]
                        
                        self.latest_order_book = {
                            "bids": [[float(p), float(q)] for p, q in sorted_bids],
                            "asks": [[float(p), float(q)] for p, q in sorted_asks],
                        }

                    # Fallback kline updating via periodic REST queries (since CoinDesk lacks raw kline streams)
                    # We query once every 10 seconds to keep candles fresh
                    now = monotonic()
                    if now - last_emit_at >= 10.0:
                        try:
                            fresh_candles = await self.rest_client.klines(self.symbol, self.timeframe, limit=5)
                            if fresh_candles:
                                # Merge candles
                                for fc in fresh_candles:
                                    # If exists, update
                                    found = False
                                    for idx, hc in enumerate(self.history_candles):
                                        if hc.open_time == fc.open_time:
                                            self.history_candles[idx] = fc
                                            found = True
                                            break
                                    if not found:
                                        self.history_candles.append(fc)
                                        new_candle_closed = True
                                if len(self.history_candles) > 200:
                                    self.history_candles = self.history_candles[-200:]
                                state_changed = True
                        except Exception as e:
                            logger.warning(f"Failed to fetch tick candles in CoinDesk stream: {e}")

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
                logger.warning("CoinDesk WS stream disconnected, reconnecting in 2s...")
                await asyncio.sleep(2)
                continue
            except Exception as e:
                logger.error(f"Error in CoinDesk WS connection loop: {e}", exc_info=True)
                await asyncio.sleep(2)
                continue
