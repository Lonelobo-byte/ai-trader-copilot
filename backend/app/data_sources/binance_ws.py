from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from dataclasses import dataclass, field
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
        # The Research workspace, Radar, funding, OI and public flow all refer
        # to the perpetual contract. Bootstrap and stream the same venue so a
        # spot candle cannot be evaluated against futures positioning.
        self.rest_client = BinancePublicClient(
            settings.binance_futures_base_url,
            market="futures",
        )
        self.history_candles: list[Candle] = []
        self.latest_ticker: dict[str, Any] = {}
        self.latest_order_book: dict[str, Any] = {"bids": [], "asks": []}
        self.market_websocket_url = (
            f"{str(settings.binance_futures_market_ws_url).rstrip('/')}?streams="
            f"{self.symbol.lower()}@kline_{self.timeframe}/"
            f"{self.symbol.lower()}@ticker"
        )
        self.book_websocket_url = (
            f"{str(settings.binance_futures_book_ws_url).rstrip('/')}?streams="
            f"{self.symbol.lower()}@depth20@500ms"
        )
        # Kept as a compatibility alias for diagnostics and older tests.
        self.websocket_url = self.market_websocket_url
        self.min_emit_seconds = 2.0

    async def initialize(self) -> None:
        try:
            logger.info(f"Initializing rest data bootstrap for {self.symbol}...")
            self.history_candles = await self.rest_client.klines(self.symbol, self.timeframe, limit=200)
            self.latest_ticker = await self.rest_client.ticker_24hr(self.symbol)
            self.latest_order_book = await self.rest_client.order_book(self.symbol, limit=100)
            
            # Switch to Futures stream if client initialized in Futures mode
            if getattr(self.rest_client, "is_futures_mode", False):
                logger.info(f"Detected {self.symbol} as a Futures contract. Using split market/book websocket streams.")
            logger.info(f"Successfully bootstrapped initial REST data for {self.symbol}.")
        except Exception as e:
            logger.error(f"Failed to initialize WS REST bootstrap for {self.symbol}: {e}")
            raise

    async def _pump_websocket(
        self,
        *,
        feed: str,
        url: str,
        queue: asyncio.Queue[tuple[str, dict[str, Any]]],
    ) -> None:
        """Reconnect one Binance namespace and forward only fresh messages."""
        delay = 1.0
        while True:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                    max_queue=32,
                    max_size=2_000_000,
                ) as websocket:
                    logger.info("Binance %s stream connected for %s/%s.", feed, self.symbol, self.timeframe)
                    while True:
                        # A TCP/WebSocket handshake can succeed even when an
                        # obsolete namespace publishes nothing.  Treat silence
                        # as unhealthy so deployment failures self-recover.
                        message = await asyncio.wait_for(websocket.recv(), timeout=15.0)
                        event_data = json.loads(message)
                        if queue.full():
                            with suppress(asyncio.QueueEmpty):
                                queue.get_nowait()
                        with suppress(asyncio.QueueFull):
                            queue.put_nowait((feed, event_data))
                        delay = 1.0
                        await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Binance %s stream unavailable for %s (%s); reconnecting.",
                    feed,
                    self.symbol,
                    exc,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, 15.0)

    async def start(self) -> AsyncGenerator[dict[str, Any], None]:
        await self.initialize()

        # Bootstrap history is immediately usable; it must not wait for two
        # external WebSocket handshakes before the chart can render.
        yield {
            "type": "init",
            "candles": [c.to_dict() for c in self.history_candles],
            "ticker": self.latest_ticker,
            "order_book": self.latest_order_book,
        }

        queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=64)
        pumps = [
            asyncio.create_task(
                self._pump_websocket(feed="market", url=self.market_websocket_url, queue=queue),
                name=f"binance-market-{self.symbol}-{self.timeframe}",
            ),
            asyncio.create_task(
                self._pump_websocket(feed="book", url=self.book_websocket_url, queue=queue),
                name=f"binance-book-{self.symbol}-{self.timeframe}",
            ),
        ]
        last_emit_at = monotonic()
        try:
            while True:
                _feed, event_data = await queue.get()
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
                            # Overwrite active candle with latest tick info.
                            self.history_candles[-1] = candle
                        else:
                            # Prior candle is closed; append the new active one.
                            new_candle_closed = True
                            self.history_candles.append(candle)
                            if len(self.history_candles) > 200:
                                self.history_candles.pop(0)
                    else:
                        self.history_candles.append(candle)
                    # Klines publish more frequently than the rolling ticker.
                    # Promote their close into the live ticker so every chart
                    # emission carries the freshest futures trade price.
                    self.latest_ticker["lastPrice"] = candle.close
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
                        "bids": [[float(p), float(q)] for p, q in (data.get("bids") or data.get("b") or [])],
                        "asks": [[float(p), float(q)] for p, q in (data.get("asks") or data.get("a") or [])],
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
                await asyncio.sleep(0)
        finally:
            for task in pumps:
                task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)


class SharedStreamCapacityError(RuntimeError):
    """Raised when every bounded upstream market stream is actively in use."""


@dataclass
class _SharedStream:
    queues: set[asyncio.Queue[dict[str, Any]]] = field(default_factory=set)
    latest: dict[str, Any] | None = None
    task: asyncio.Task[None] | None = None
    idle_task: asyncio.Task[None] | None = None


class SharedBinanceStreamHub:
    """Fan one Binance upstream connection out to every local Research client.

    A browser tab receives its own one-item queue, so a slow tab drops obsolete
    intermediate ticks instead of adding memory or back-pressure to the public
    exchange feed.  Streams with no listeners are closed after a short grace
    period, which also makes rapid page navigation inexpensive.
    """

    def __init__(self, settings: Any):
        self.settings = settings
        self.max_pairs = max(1, int(settings.analysis_stream_max_pairs))
        self.idle_seconds = max(0.0, float(settings.analysis_stream_idle_seconds))
        self._streams: dict[tuple[str, str], _SharedStream] = {}
        self._lock = asyncio.Lock()

    async def events(self, symbol: str, timeframe: str) -> AsyncGenerator[dict[str, Any], None]:
        key = (symbol.upper().strip(), timeframe)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)
        async with self._lock:
            state = self._streams.get(key)
            if state is None:
                self._remove_finished_streams()
                if len(self._streams) >= self.max_pairs:
                    raise SharedStreamCapacityError(
                        "Live market-stream capacity is busy. Close an unused research pair and retry shortly."
                    )
                state = _SharedStream()
                self._streams[key] = state
                state.task = asyncio.create_task(
                    self._produce(key, state),
                    name=f"shared-binance-{key[0]}-{key[1]}",
                )
            if state.idle_task is not None:
                state.idle_task.cancel()
                state.idle_task = None
            state.queues.add(queue)
            if state.latest is not None:
                initial = dict(state.latest)
                initial["type"] = "init"
                initial["new_candle_closed"] = False
                queue.put_nowait(initial)

        try:
            while True:
                yield await queue.get()
        finally:
            async with self._lock:
                current = self._streams.get(key)
                if current is state:
                    current.queues.discard(queue)
                    if not current.queues and current.idle_task is None:
                        current.idle_task = asyncio.create_task(
                            self._close_when_idle(key, current),
                            name=f"shared-binance-idle-{key[0]}-{key[1]}",
                        )

    def _remove_finished_streams(self) -> None:
        for key, state in list(self._streams.items()):
            if not state.queues and state.task is not None and state.task.done():
                self._streams.pop(key, None)

    async def _produce(self, key: tuple[str, str], state: _SharedStream) -> None:
        symbol, timeframe = key
        while True:
            try:
                subscriber = BinanceWSSubscriber(symbol, timeframe, self.settings)
                async for event in subscriber.start():
                    state.latest = event
                    for queue in tuple(state.queues):
                        if queue.full():
                            with suppress(asyncio.QueueEmpty):
                                queue.get_nowait()
                        with suppress(asyncio.QueueFull):
                            queue.put_nowait(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Shared Binance stream failed; reconnecting.", extra={"symbol": symbol, "timeframe": timeframe})
                await asyncio.sleep(2)

    async def _close_when_idle(self, key: tuple[str, str], state: _SharedStream) -> None:
        try:
            if self.idle_seconds:
                await asyncio.sleep(self.idle_seconds)
            async with self._lock:
                if self._streams.get(key) is not state or state.queues:
                    return
                self._streams.pop(key, None)
                task = state.task
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        except asyncio.CancelledError:
            raise


_SHARED_HUB: SharedBinanceStreamHub | None = None


def get_shared_binance_stream_hub(settings: Any) -> SharedBinanceStreamHub:
    global _SHARED_HUB
    if _SHARED_HUB is None:
        _SHARED_HUB = SharedBinanceStreamHub(settings)
    return _SHARED_HUB
