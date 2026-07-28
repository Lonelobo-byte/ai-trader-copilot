"""Shared, bounded public WebSocket market evidence from Bybit and Coinbase.

The hub owns one connection per venue for the whole application process. It
never opens connections per user or per analysis request. Raw updates are
reduced into bounded rolling evidence so a long-running server cannot grow
memory with market activity.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
from math import isfinite
from time import monotonic, time
from typing import Any

import websockets

from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class SequenceGapError(RuntimeError):
    """Raised when a venue sequence gap requires a clean reconnect/snapshot."""


class SubscriptionError(RuntimeError):
    """Raised when required public channels fail to become ready."""


class BookIntegrityError(RuntimeError):
    """Raised when an update produces an impossible local order book."""


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if isfinite(result) else default


def _symbol(value: str) -> str:
    return value.upper().replace("-", "").strip()


def _source_time_is_fresh(value: Any, max_age_seconds: float) -> bool:
    """Reject delayed/replayed exchange events before they enter live flow."""
    if value in (None, ""):
        return False
    try:
        if isinstance(value, (int, float)) or str(value).replace(".", "", 1).isdigit():
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp /= 1_000.0
        else:
            text = str(value).strip()
            if text.endswith("Z"):
                text = f"{text[:-1]}+00:00"
            timestamp = datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError, OverflowError):
        return False
    age = time() - timestamp
    return -5.0 <= age <= max(1.0, max_age_seconds)


class _InstrumentState:
    """Bounded mutable state for one venue/instrument."""

    def __init__(
        self,
        *,
        venue: str,
        symbol: str,
        max_levels: int,
        max_events: int,
        max_window_seconds: float,
        min_book_levels: int,
    ) -> None:
        self.venue = venue
        self.symbol = symbol
        self.max_levels = max(20, min(int(max_levels), 5_000))
        self.min_book_levels = max(1, min(int(min_book_levels), self.max_levels))
        event_limit = max(100, min(int(max_events), 5_000))
        bucket_limit = max(120, min(int(max_window_seconds) + 5, 1_805))
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.trades: deque[dict[str, Any]] = deque(maxlen=bucket_limit)
        self.liquidations: deque[dict[str, Any]] = deque(maxlen=bucket_limit)
        self.book_events: deque[dict[str, Any]] = deque(maxlen=bucket_limit)
        self.imbalance_samples: deque[tuple[float, float]] = deque(maxlen=bucket_limit)
        dedupe_size = event_limit
        self._trade_ids: deque[str] = deque(maxlen=dedupe_size)
        self._trade_id_set: set[str] = set()
        self._liquidation_ids: deque[str] = deque(maxlen=dedupe_size)
        self._liquidation_id_set: set[str] = set()
        self.connected = False
        self.book_ready = False
        self.liquidation_stream_ready = False
        self.health_reason = "DISCONNECTED"
        self.connected_at = 0.0
        self.last_message_at = 0.0
        self.last_transport_at = 0.0
        self.last_book_at = 0.0
        self.last_trade_at = 0.0
        self.last_liquidation_at = 0.0
        self.last_sample_at = 0.0
        self.last_label_at = 0.0
        self.last_update_id: int | None = None
        self.last_sequence: int | None = None
        self.last_trade_side: str | None = None
        self.received_at: str | None = None

    def disconnect(self, reason: str = "DISCONNECTED") -> None:
        self.connected = False
        self.book_ready = False
        self.liquidation_stream_ready = False
        self.health_reason = reason
        self.bids.clear()
        self.asks.clear()
        # Never combine observations from opposite sides of an unobserved gap.
        self.trades.clear()
        self.liquidations.clear()
        self.book_events.clear()
        self.imbalance_samples.clear()
        self._trade_ids.clear()
        self._trade_id_set.clear()
        self._liquidation_ids.clear()
        self._liquidation_id_set.clear()
        self.connected_at = 0.0
        self.last_message_at = 0.0
        self.last_transport_at = 0.0
        self.last_book_at = 0.0
        self.last_trade_at = 0.0
        self.last_liquidation_at = 0.0
        self.last_sample_at = 0.0
        self.last_label_at = 0.0
        self.last_update_id = None
        self.last_sequence = None
        self.last_trade_side = None
        self.received_at = None

    def mark_connected(self, now: float | None = None) -> None:
        observed = monotonic() if now is None else now
        self.connected = True
        self.book_ready = False
        self.health_reason = "SYNCING"
        self.connected_at = observed
        self.touch_connection(observed)

    def touch_connection(self, now: float | None = None) -> None:
        """Record transport health without making symbol data look fresh."""
        observed = monotonic() if now is None else now
        self.last_transport_at = observed
        self._refresh_received_label(observed)

    @staticmethod
    def _remember_identifier(identifier: str, queue: deque[str], identifiers: set[str]) -> bool:
        """Return False for a duplicate while keeping the dedupe index bounded."""
        if identifier in identifiers:
            return False
        if queue.maxlen and len(queue) >= queue.maxlen:
            identifiers.discard(queue[0])
        queue.append(identifier)
        identifiers.add(identifier)
        return True

    def _touch(self, now: float | None = None) -> float:
        observed = monotonic() if now is None else now
        self.last_message_at = observed
        self.last_transport_at = observed
        self._refresh_received_label(observed)
        return observed

    def _refresh_received_label(self, observed: float) -> None:
        if self.received_at is None or observed - self.last_label_at >= 1.0:
            self.last_label_at = observed
            self.received_at = datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _bucket(
        queue: deque[dict[str, Any]], observed: float, defaults: dict[str, Any]
    ) -> dict[str, Any]:
        second = int(observed)
        if queue and queue[-1]["second"] == second:
            queue[-1]["at"] = observed
            return queue[-1]
        bucket = {"second": second, "at": observed, **defaults}
        queue.append(bucket)
        return bucket

    def _prune_book(self) -> None:
        if len(self.bids) > self.max_levels:
            self.bids = dict(sorted(self.bids.items(), reverse=True)[: self.max_levels])
        if len(self.asks) > self.max_levels:
            self.asks = dict(sorted(self.asks.items())[: self.max_levels])

    def _apply_side(
        self,
        book: dict[float, float],
        rows: list[list[Any]],
        *,
        side: str,
        now: float,
        record_events: bool,
    ) -> None:
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            price, quantity = _number(row[0]), _number(row[1])
            if price <= 0:
                continue
            if quantity < 0:
                continue
            previous_quantity = book.get(price, 0.0)
            if quantity == previous_quantity:
                continue
            reduction = quantity < previous_quantity
            if quantity == 0:
                book.pop(price, None)
            else:
                book[price] = quantity
            if record_events:
                bucket = self._bucket(
                    self.book_events,
                    now,
                    {"addition_count": 0, "removal_count": 0, "bid_count": 0, "ask_count": 0},
                )
                bucket["removal_count" if reduction else "addition_count"] += 1
                bucket["bid_count" if side == "bid" else "ask_count"] += 1

    def apply_book(
        self,
        *,
        bids: list[list[Any]],
        asks: list[list[Any]],
        snapshot: bool,
        update_id: int | None = None,
        sequence: int | None = None,
        now: float | None = None,
    ) -> None:
        observed = self._touch(now)
        if sequence is not None and self.last_sequence is not None and sequence <= self.last_sequence and not snapshot:
            return
        if update_id is not None and self.last_update_id is not None and update_id <= self.last_update_id and not snapshot:
            return
        if snapshot:
            self.bids.clear()
            self.asks.clear()
        elif not self.book_ready:
            # Never apply a delta to an unknown book.
            return

        self._apply_side(self.bids, bids, side="bid", now=observed, record_events=not snapshot)
        self._apply_side(self.asks, asks, side="ask", now=observed, record_events=not snapshot)
        self._prune_book()
        self.book_ready = (
            len(self.bids) >= self.min_book_levels
            and len(self.asks) >= self.min_book_levels
        )
        if self.book_ready and max(self.bids) >= min(self.asks):
            self.book_ready = False
            self.health_reason = "CROSSED_BOOK"
            self.bids.clear()
            self.asks.clear()
            raise BookIntegrityError(f"{self.venue} {self.symbol} produced a crossed order book")
        if not self.book_ready:
            self.health_reason = "INSUFFICIENT_DEPTH"
            raise BookIntegrityError(
                f"{self.venue} {self.symbol} order-book depth is below "
                f"{self.min_book_levels} levels per side"
            )

        self.last_book_at = observed
        self.health_reason = "HEALTHY" if self.book_ready else "INSUFFICIENT_DEPTH"
        if update_id is not None:
            self.last_update_id = update_id
        if sequence is not None:
            self.last_sequence = sequence

        if self.book_ready and observed - self.last_sample_at >= 1.0:
            self.last_sample_at = observed
            bids_top = sorted(self.bids.items(), reverse=True)[:20]
            asks_top = sorted(self.asks.items())[:20]
            bid_notional = sum(price * size for price, size in bids_top)
            ask_notional = sum(price * size for price, size in asks_top)
            denominator = bid_notional + ask_notional
            imbalance = (bid_notional - ask_notional) / denominator if denominator else 0.0
            self.imbalance_samples.append((observed, imbalance))

    def record_trade(
        self,
        *,
        taker_side: str,
        price: Any,
        size: Any,
        event_id: str | None = None,
        now: float | None = None,
    ) -> None:
        observed = self._touch(now)
        price_value, size_value = _number(price), _number(size)
        if price_value <= 0 or size_value <= 0:
            return
        normalized_side = str(taker_side).upper()
        if normalized_side not in {"BUY", "SELL"}:
            return
        if event_id and not self._remember_identifier(event_id, self._trade_ids, self._trade_id_set):
            return

        bucket = self._bucket(
            self.trades, observed,
            {"buy_notional": 0.0, "sell_notional": 0.0, "trade_count": 0},
        )
        bucket["buy_notional" if normalized_side == "BUY" else "sell_notional"] += price_value * size_value
        bucket["trade_count"] += 1
        self.last_trade_side = normalized_side
        self.last_trade_at = observed

    def record_liquidation(
        self,
        *,
        position_side: str,
        price: Any,
        size: Any,
        event_id: str | None = None,
        now: float | None = None,
    ) -> None:
        observed = self._touch(now)
        price_value, size_value = _number(price), _number(size)
        if price_value <= 0 or size_value <= 0:
            return
        normalized_side = str(position_side).upper()
        if normalized_side not in {"LONG", "SHORT"}:
            return
        if event_id and not self._remember_identifier(
            event_id, self._liquidation_ids, self._liquidation_id_set
        ):
            return

        bucket = self._bucket(
            self.liquidations, observed,
            {"long_notional": 0.0, "short_notional": 0.0, "event_count": 0},
        )
        bucket["long_notional" if normalized_side == "LONG" else "short_notional"] += price_value * size_value
        bucket["event_count"] += 1
        self.last_liquidation_at = observed

    def snapshot(
        self,
        *,
        stale_seconds: float,
        trade_window_seconds: float,
        liquidation_window_seconds: float,
        flow_warmup_seconds: float,
        min_flow_trades: int,
        min_flow_notional: float,
        now: float | None = None,
    ) -> dict[str, Any]:
        observed = monotonic() if now is None else now
        data_age = observed - self.last_message_at if self.last_message_at else None
        book_age = observed - self.last_book_at if self.last_book_at else None
        trade_age = observed - self.last_trade_at if self.last_trade_at else None
        transport_age = observed - self.last_transport_at if self.last_transport_at else None
        uptime = max(0.0, observed - self.connected_at) if self.connected_at else 0.0
        if not self.connected:
            health = self.health_reason if self.health_reason != "HEALTHY" else "DISCONNECTED"
        elif not self.book_ready:
            health = self.health_reason if self.health_reason != "HEALTHY" else "SYNCING"
        elif book_age is None or book_age > stale_seconds:
            health = "STALE"
        else:
            health = "HEALTHY"
        fresh = health == "HEALTHY"
        flow_warmed = uptime >= flow_warmup_seconds
        bids = sorted(self.bids.items(), reverse=True)[:20]
        asks = sorted(self.asks.items())[:20]
        best_bid = bids[0][0] if bids else 0.0
        best_ask = asks[0][0] if asks else 0.0
        midpoint = (best_bid + best_ask) / 2.0 if best_bid and best_ask else 0.0
        bid_notional = sum(price * size for price, size in bids)
        ask_notional = sum(price * size for price, size in asks)
        depth_total = bid_notional + ask_notional

        trades = [row for row in self.trades if observed - row["at"] <= trade_window_seconds]
        buy_notional = sum(row["buy_notional"] for row in trades)
        sell_notional = sum(row["sell_notional"] for row in trades)
        trade_count = sum(row["trade_count"] for row in trades)
        trade_total = buy_notional + sell_notional
        signed_flow = (buy_notional - sell_notional) / trade_total if trade_total else 0.0
        flow_available = (
            fresh
            and trade_age is not None and trade_age <= stale_seconds
            and flow_warmed
            and trade_count >= min_flow_trades
            and trade_total >= min_flow_notional
        )

        book_events = [row for row in self.book_events if observed - row["at"] <= trade_window_seconds]
        additions = sum(row["addition_count"] for row in book_events)
        removals = sum(row["removal_count"] for row in book_events)
        book_event_count = additions + removals
        samples = [value for at, value in self.imbalance_samples if observed - at <= trade_window_seconds]
        mean_imbalance = sum(samples) / len(samples) if samples else 0.0
        same_sign = (
            sum((value >= 0) == (mean_imbalance >= 0) for value in samples) / len(samples)
            if samples else 0.0
        )

        liquidations = [
            row for row in self.liquidations
            if observed - row["at"] <= liquidation_window_seconds
        ]
        long_liquidated = sum(row["long_notional"] for row in liquidations)
        short_liquidated = sum(row["short_notional"] for row in liquidations)
        liquidation_event_count = sum(row["event_count"] for row in liquidations)
        liquidation_available = bool(
            self.venue == "bybit"
            and self.connected
            and self.liquidation_stream_ready
            and transport_age is not None and transport_age <= stale_seconds
        )
        return {
            "venue": self.venue,
            "symbol": self.symbol,
            "available": fresh,
            "connected": self.connected,
            "fresh": fresh,
            "health": health,
            "age_seconds": round(book_age, 3) if book_age is not None else None,
            "data_age_seconds": round(data_age, 3) if data_age is not None else None,
            "transport_age_seconds": round(transport_age, 3) if transport_age is not None else None,
            "book_age_seconds": round(book_age, 3) if book_age is not None else None,
            "received_at": self.received_at,
            "order_book": {
                "ready": self.book_ready,
                "best_bid": best_bid or None,
                "best_ask": best_ask or None,
                "mid_price": round(midpoint, 8) if midpoint else None,
                "spread_bps": round((best_ask - best_bid) / midpoint * 10_000, 3) if midpoint else None,
                "depth_imbalance": round((bid_notional - ask_notional) / depth_total, 4) if depth_total else None,
                "persistent_imbalance": round(mean_imbalance, 4) if samples else None,
                "persistence_ratio": round(same_sign, 3) if samples else None,
                "sample_count": len(samples),
                "book_event_count": book_event_count,
                "removal_ratio": round(removals / book_event_count, 4) if book_event_count else None,
            },
            "trade_flow": {
                "available": flow_available,
                "age_seconds": round(trade_age, 3) if trade_age is not None else None,
                "window_seconds": trade_window_seconds,
                "trade_count": trade_count,
                "connection_uptime_seconds": round(uptime, 3),
                "warmup_complete": flow_warmed,
                "minimum_trade_count": min_flow_trades,
                "minimum_notional": min_flow_notional,
                "qualified_notional": round(trade_total, 2),
                "buy_notional": round(buy_notional, 2),
                "sell_notional": round(sell_notional, 2),
                "signed_flow": round(signed_flow, 4),
                "aggressive_buy_ratio": round(buy_notional / trade_total, 4) if trade_total else None,
            },
            "liquidations": {
                # A quiet window is a valid zero only while the shared Bybit
                # connection and liquidation subscription are both healthy.
                "available": liquidation_available,
                "observed": liquidation_available,
                "window_seconds": liquidation_window_seconds,
                "event_count": liquidation_event_count,
                "long_liquidated_notional": round(long_liquidated, 2),
                "short_liquidated_notional": round(short_liquidated, 2),
                "net_short_minus_long": round(short_liquidated - long_liquidated, 2),
            },
        }


class MultiVenueMarketDataHub:
    """One shared Bybit/Coinbase market-data hub per application process."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if self.settings.app_env.lower() not in {"local", "test", "development"}:
            endpoints = (self.settings.bybit_public_ws_url, self.settings.coinbase_public_ws_url)
            if any(not endpoint.lower().startswith("wss://") for endpoint in endpoints):
                raise ValueError("Public market-data WebSocket endpoints must use wss:// outside local development")

        self.stale_seconds = max(2.0, min(float(self.settings.multi_venue_stale_seconds), 120.0))
        self.trade_window_seconds = max(5.0, min(float(self.settings.multi_venue_trade_window_seconds), 300.0))
        self.liquidation_window_seconds = max(30.0, min(float(self.settings.multi_venue_liquidation_window_seconds), 1_800.0))
        self.flow_warmup_seconds = max(1.0, min(float(self.settings.multi_venue_flow_warmup_seconds), 300.0))
        self.min_flow_trades = max(1, min(int(self.settings.multi_venue_min_flow_trades), 10_000))
        self.min_flow_notional = max(0.0, min(float(self.settings.multi_venue_min_flow_notional_usd), 1_000_000_000.0))
        self.max_event_lag_seconds = max(1.0, min(float(self.settings.multi_venue_max_event_lag_seconds), 60.0))
        self.stable_connection_seconds = max(10.0, min(float(self.settings.multi_venue_stable_connection_seconds), 300.0))
        self.initial_sync_timeout_seconds = max(10.0, min(float(self.settings.multi_venue_initial_sync_timeout_seconds), 120.0))
        self.subscription_retry_seconds = max(60.0, min(float(self.settings.multi_venue_subscription_retry_seconds), 3_600.0))
        self.coinbase_resync_seconds = max(300.0, min(float(self.settings.coinbase_book_resync_seconds), 3_600.0))
        configured: list[str] = []
        for item in self.settings.multi_venue_symbols:
            normalized = _symbol(item)
            if normalized and normalized.endswith("USDT") and normalized not in configured:
                configured.append(normalized)
        max_symbols = max(1, min(int(self.settings.multi_venue_max_symbols), 12))
        self.symbols = configured[:max_symbols]
        if not self.symbols:
            logger.warning("No valid USDT symbols configured for the multi-venue public streams.")
        self.states: dict[tuple[str, str], _InstrumentState] = {}
        self._coinbase_sequence: int | None = None
        self._bybit_subscriptions: set[str] = set()
        self._bybit_rejected_symbols: dict[str, float] = {}
        self._coinbase_rejected_products: dict[str, float] = {}
        self.metrics: dict[str, int | float] = {
            "bybit_reconnects": 0,
            "coinbase_reconnects": 0,
            "sequence_gaps": 0,
            "subscription_errors": 0,
            "stale_events_dropped": 0,
        }
        self._configure_states()
        self._snapshot_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def _configure_states(self) -> None:
        max_window = max(self.trade_window_seconds, self.liquidation_window_seconds)
        for venue in ("bybit", "coinbase"):
            for symbol in self.symbols:
                self.states[(venue, symbol)] = _InstrumentState(
                    venue=venue,
                    symbol=symbol,
                    max_levels=(
                        self.settings.coinbase_multi_venue_book_levels
                        if venue == "coinbase"
                        else self.settings.multi_venue_book_levels
                    ),
                    max_events=self.settings.multi_venue_max_events,
                    max_window_seconds=max_window,
                    min_book_levels=self.settings.multi_venue_min_book_levels,
                )

    @staticmethod
    def coinbase_product(symbol: str) -> str:
        normalized = _symbol(symbol)
        base = normalized[:-4] if normalized.endswith("USDT") else normalized[:-3] if normalized.endswith("USD") else normalized
        return f"{base}-USD"

    @staticmethod
    def coinbase_symbol(product_id: str) -> str:
        normalized = _symbol(product_id)
        return f"{normalized[:-3]}USDT" if normalized.endswith("USD") else normalized

    @property
    def quarantined_subscriptions(self) -> dict[str, list[str]]:
        """Return rejected public instruments for operational visibility."""
        return {
            "bybit_symbols": sorted(self._bybit_rejected_symbols),
            "coinbase_products": sorted(self._coinbase_rejected_products),
        }

    @staticmethod
    def _release_expired_quarantine(rejected: dict[str, float]) -> None:
        observed = monotonic()
        for instrument, retry_at in tuple(rejected.items()):
            if retry_at <= observed:
                rejected.pop(instrument, None)

    def _active_bybit_symbols(self) -> list[str]:
        self._release_expired_quarantine(self._bybit_rejected_symbols)
        return [symbol for symbol in self.symbols if symbol not in self._bybit_rejected_symbols]

    def _active_coinbase_products(self, products: list[str]) -> list[str]:
        self._release_expired_quarantine(self._coinbase_rejected_products)
        return [product for product in products if product not in self._coinbase_rejected_products]

    def _state(self, venue: str, symbol: str) -> _InstrumentState | None:
        return self.states.get((venue, _symbol(symbol)))

    def _set_connected(
        self, venue: str, connected: bool, *, reason: str = "DISCONNECTED", now: float | None = None
    ) -> None:
        self._snapshot_cache.clear()
        for (state_venue, _), state in self.states.items():
            if state_venue != venue:
                continue
            if connected:
                state.mark_connected(now)
            else:
                state.disconnect(reason)

    def _touch_venue(self, venue: str, now: float | None = None) -> None:
        for (state_venue, _), state in self.states.items():
            if state_venue == venue:
                state.touch_connection(now)

    def _assert_stream_readiness(
        self, venue: str, active_symbols: list[str], connected_at: float
    ) -> None:
        """Reconnect a transport that never syncs or silently stops L2 data."""
        if not active_symbols:
            return
        observed = monotonic()
        states = [self.states[(venue, symbol)] for symbol in active_symbols]
        if observed - connected_at >= self.initial_sync_timeout_seconds:
            not_ready = [state.symbol for state in states if not state.book_ready]
            if not_ready:
                raise SubscriptionError(
                    f"{venue} initial order-book sync timed out for {not_ready}"
                )
        stale = [
            state.symbol
            for state in states
            if state.book_ready and observed - state.last_book_at > self.stale_seconds
        ]
        if stale:
            raise BookIntegrityError(f"{venue} Level-2 stream became stale for {stale}")

    def _mark_bybit_subscription(self, message: dict[str, Any]) -> bool:
        if str(message.get("op", "")) != "subscribe":
            return False
        request_id = str(message.get("req_id", ""))
        requested_symbol = request_id.removeprefix("mv:") if request_id.startswith("mv:") else ""
        targets = [requested_symbol] if requested_symbol in self.symbols else list(self.symbols)
        if message.get("success") is not True:
            self.metrics["subscription_errors"] = int(self.metrics["subscription_errors"]) + 1
            for symbol in targets:
                self._bybit_rejected_symbols[symbol] = monotonic() + self.subscription_retry_seconds
                state = self._state("bybit", symbol)
                if state:
                    state.disconnect("SUBSCRIPTION_REJECTED")
            logger.error("Bybit rejected public subscription %s: %s", request_id, message.get("ret_msg"))
            return True
        for symbol in targets:
            self._bybit_rejected_symbols.pop(symbol, None)
            self._bybit_subscriptions.add(symbol)
            state = self._state("bybit", symbol)
            if state:
                state.liquidation_stream_ready = True
        return True

    def _coinbase_message_is_new(self, message: dict[str, Any]) -> bool:
        """Track Coinbase's connection-wide top-level message sequence."""
        sequence_raw = message.get("sequence_num")
        if sequence_raw is None:
            return True
        sequence = int(sequence_raw)
        previous = self._coinbase_sequence
        if previous is None:
            self._coinbase_sequence = sequence
            return True
        if sequence > previous + 1:
            self.metrics["sequence_gaps"] = int(self.metrics["sequence_gaps"]) + 1
            self._set_connected("coinbase", False, reason="SEQUENCE_GAP")
            raise SequenceGapError(
                f"Coinbase connection sequence gap: {previous} -> {sequence}"
            )
        if sequence <= previous:
            return False
        self._coinbase_sequence = sequence
        return True

    def _source_event_is_fresh(self, value: Any) -> bool:
        fresh = _source_time_is_fresh(value, self.max_event_lag_seconds)
        if not fresh:
            self.metrics["stale_events_dropped"] = int(self.metrics["stale_events_dropped"]) + 1
        return fresh

    def process_bybit_message(self, message: dict[str, Any], *, now: float | None = None) -> None:
        topic = str(message.get("topic", ""))
        data = message.get("data")
        if self._mark_bybit_subscription(message):
            self._touch_venue("bybit", now)
            return
        if not topic:
            self._touch_venue("bybit", now)
            return
        if topic.startswith("orderbook.") and isinstance(data, dict):
            source_time = message.get("cts") or message.get("ts")
            if not self._source_event_is_fresh(source_time):
                raise BookIntegrityError("Delayed Bybit order-book frame requires a clean snapshot")
            state = self._state("bybit", str(data.get("s", "")))
            if state:
                state.apply_book(
                    bids=data.get("b", []) or [],
                    asks=data.get("a", []) or [],
                    snapshot=message.get("type") == "snapshot" or _number(data.get("u")) == 1,
                    update_id=int(data["u"]) if data.get("u") is not None else None,
                    sequence=int(data["seq"]) if data.get("seq") is not None else None,
                    now=now,
                )
                self._bybit_subscriptions.add(state.symbol)
                state.liquidation_stream_ready = True
            return
        if topic.startswith("publicTrade.") and isinstance(data, list):
            for trade in data:
                if not self._source_event_is_fresh(trade.get("T")):
                    continue
                state = self._state("bybit", str(trade.get("s", "")))
                if state:
                    state.record_trade(
                        taker_side=str(trade.get("S", "")),
                        price=trade.get("p"),
                        size=trade.get("v"),
                        event_id=str(trade.get("i", "")) or None,
                        now=now,
                    )
            return
        if topic.startswith("allLiquidation."):
            events = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
            for event_index, event in enumerate(events):
                state = self._state("bybit", str(event.get("s", "")))
                if not state:
                    continue
                state.liquidation_stream_ready = True
                side = str(event.get("S", "")).upper()
                if not self._source_event_is_fresh(event.get("T")):
                    continue
                if side not in {"BUY", "SELL"}:
                    continue
                # Bybit documents S=Buy as a liquidated long position.
                position_side = "LONG" if side == "BUY" else "SHORT"
                state.record_liquidation(
                    position_side=position_side,
                    price=event.get("p"),
                    size=event.get("v"),
                    event_id=f"{message.get('ts')}:{event.get('T')}:{event_index}:{event.get('s')}:{side}:{event.get('v')}:{event.get('p')}",
                    now=now,
                )
            return

    def process_coinbase_message(self, message: dict[str, Any], *, now: float | None = None) -> None:
        channel = str(message.get("channel", ""))
        if not self._coinbase_message_is_new(message):
            return
        if message.get("type") == "error" or channel == "error":
            self.metrics["subscription_errors"] = int(self.metrics["subscription_errors"]) + 1
            error_text = str(message.get("message", "unknown error"))
            for symbol in self.symbols:
                product = self.coinbase_product(symbol)
                if product in error_text:
                    self._coinbase_rejected_products[product] = monotonic() + self.subscription_retry_seconds
                    state = self._state("coinbase", symbol)
                    if state:
                        state.disconnect("SUBSCRIPTION_REJECTED")
            logger.error("Coinbase rejected a public subscription: %s", error_text)
            raise SubscriptionError(f"Coinbase public subscription rejected: {error_text}")
        if channel == "subscriptions":
            self._touch_venue("coinbase", now)
            return

        if channel == "heartbeats":
            self._touch_venue("coinbase", now)
            return
        sequence_raw = message.get("sequence_num")
        message_time = message.get("timestamp")
        if channel in {"l2_data", "level2"} and not self._source_event_is_fresh(message_time):
            raise BookIntegrityError("Delayed Coinbase Level-2 frame requires a clean snapshot")
        for event in message.get("events", []) or []:
            if channel in {"l2_data", "level2"}:
                product = str(event.get("product_id", ""))
                state = self._state("coinbase", self.coinbase_symbol(product))
                if not state:
                    continue
                event_is_snapshot = event.get("type") == "snapshot"
                bids: list[list[Any]] = []
                asks: list[list[Any]] = []
                for update in event.get("updates", []) or []:
                    side = str(update.get("side", "")).lower()
                    if side not in {"bid", "offer", "ask"}:
                        continue
                    row = [update.get("price_level"), update.get("new_quantity")]
                    (bids if side == "bid" else asks).append(row)
                state.apply_book(
                    bids=bids,
                    asks=asks,
                    snapshot=event_is_snapshot,
                    sequence=int(sequence_raw) if sequence_raw is not None else None,
                    now=now,
                )
            elif channel == "market_trades":
                grouped: dict[str, list[dict[str, Any]]] = {}
                for trade in event.get("trades", []) or []:
                    product = str(trade.get("product_id", ""))
                    if product:
                        grouped.setdefault(product, []).append(trade)
                event_is_snapshot = event.get("type") == "snapshot"
                for product, trades in grouped.items():
                    state = self._state("coinbase", self.coinbase_symbol(product))
                    if not state:
                        continue
                    for trade in trades:
                        if not self._source_event_is_fresh(trade.get("time")):
                            continue
                        maker_side = str(trade.get("side", "")).upper()
                        if maker_side not in {"BUY", "SELL"}:
                            continue
                        # Coinbase reports maker side; aggression is the inverse.
                        taker_side = "SELL" if maker_side == "BUY" else "BUY"
                        state.record_trade(
                            taker_side=taker_side,
                            price=trade.get("price"),
                            size=trade.get("size"),
                            event_id=str(trade.get("trade_id", "")) or None,
                            now=now,
                        )

    @staticmethod
    async def _bybit_heartbeat(websocket: Any) -> None:
        while True:
            await asyncio.sleep(20)
            await websocket.send(json.dumps({"op": "ping"}))

    async def run_bybit(self) -> None:
        url = self.settings.bybit_public_ws_url
        delay = 1.0
        while True:
            failure_reason = "CONNECTION_CLOSED"
            heartbeat_task: asyncio.Task | None = None
            try:
                self._set_connected("bybit", False)
                self._bybit_subscriptions.clear()
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                    max_queue=8,
                    max_size=1_000_000,
                ) as websocket:
                    self._set_connected("bybit", True)
                    connected_at = monotonic()
                    active_symbols = self._active_bybit_symbols()
                    for symbol in self._bybit_rejected_symbols:
                        state = self._state("bybit", symbol)
                        if state:
                            state.disconnect("SUBSCRIPTION_REJECTED")
                    for symbol in active_symbols:
                        args = [
                            f"orderbook.50.{symbol}",
                            f"publicTrade.{symbol}",
                            f"allLiquidation.{symbol}",
                        ]
                        await websocket.send(json.dumps({"op": "subscribe", "req_id": f"mv:{symbol}", "args": args}))
                    heartbeat_task = asyncio.create_task(
                        self._bybit_heartbeat(websocket), name="bybit-application-heartbeat"
                    )
                    logger.info("Bybit public market-data stream connected for %s.", self.symbols)
                    async for raw in websocket:
                        self.process_bybit_message(json.loads(raw))
                        active_symbols = self._active_bybit_symbols()
                        self._assert_stream_readiness("bybit", active_symbols, connected_at)
                        if (
                            monotonic() - connected_at >= self.stable_connection_seconds
                            and active_symbols
                            and all(self.states[("bybit", symbol)].book_ready for symbol in active_symbols)
                        ):
                            delay = 1.0
            except asyncio.CancelledError:
                raise
            except SubscriptionError as exc:
                failure_reason = "SUBSCRIPTION_ERROR"
                logger.warning("Bybit public subscription did not become ready: %s", exc)
            except BookIntegrityError as exc:
                failure_reason = "BOOK_INTEGRITY_ERROR"
                logger.warning("Bybit book integrity failure; rebuilding from snapshot: %s", exc)
            except Exception as exc:
                logger.warning("Bybit public stream reconnecting after error: %s", exc)
            finally:
                if heartbeat_task:
                    heartbeat_task.cancel()
                    await asyncio.gather(heartbeat_task, return_exceptions=True)
                self._set_connected("bybit", False, reason=failure_reason)
            self.metrics["bybit_reconnects"] = int(self.metrics["bybit_reconnects"]) + 1
            await asyncio.sleep(delay + random.uniform(0.0, min(delay, 1.0)))
            delay = min(delay * 2.0, 30.0)

    async def run_coinbase(self) -> None:
        url = self.settings.coinbase_public_ws_url
        products = [self.coinbase_product(symbol) for symbol in self.symbols]
        delay = 1.0
        while True:
            failure_reason = "CONNECTION_CLOSED"
            periodic_resync = False
            try:
                self._set_connected("coinbase", False)
                self._coinbase_sequence = None
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                    max_queue=4,
                    max_size=8_000_000,
                ) as websocket:
                    self._set_connected("coinbase", True)
                    connected_at = monotonic()
                    active_products = self._active_coinbase_products(products)
                    active_symbols = [
                        symbol for symbol in self.symbols
                        if self.coinbase_product(symbol) in active_products
                    ]
                    for product in self._coinbase_rejected_products:
                        state = self._state("coinbase", self.coinbase_symbol(product))
                        if state:
                            state.disconnect("SUBSCRIPTION_REJECTED")
                    await websocket.send(json.dumps({"type": "subscribe", "channel": "heartbeats"}))
                    if active_products:
                        for channel in ("level2", "market_trades"):
                            await websocket.send(json.dumps({
                                "type": "subscribe",
                                "channel": channel,
                                "product_ids": active_products,
                            }))
                    logger.info("Coinbase public market-data stream connected for %s.", active_products)
                    async for raw in websocket:
                        self.process_coinbase_message(json.loads(raw))
                        active_products = self._active_coinbase_products(products)
                        active_symbols = [
                            symbol for symbol in self.symbols
                            if self.coinbase_product(symbol) in active_products
                        ]
                        self._assert_stream_readiness("coinbase", active_symbols, connected_at)
                        uptime = monotonic() - connected_at
                        if (
                            uptime >= self.stable_connection_seconds
                            and active_symbols
                            and all(self.states[("coinbase", symbol)].book_ready for symbol in active_symbols)
                        ):
                            delay = 1.0
                        if uptime >= self.coinbase_resync_seconds:
                            failure_reason = "PERIODIC_BOOK_RESYNC"
                            periodic_resync = True
                            break
            except asyncio.CancelledError:
                raise
            except SubscriptionError as exc:
                failure_reason = "SUBSCRIPTION_ERROR"
                logger.warning("Coinbase public subscription did not become ready: %s", exc)
            except SequenceGapError as exc:
                failure_reason = "SEQUENCE_GAP"
                logger.warning("Coinbase sequence gap; rebuilding from snapshot: %s", exc)
            except BookIntegrityError as exc:
                failure_reason = "BOOK_INTEGRITY_ERROR"
                logger.warning("Coinbase book integrity failure; rebuilding from snapshot: %s", exc)
            except Exception as exc:
                logger.warning("Coinbase public stream reconnecting after error: %s", exc)
            finally:
                self._set_connected("coinbase", False, reason=failure_reason)
            self.metrics["coinbase_reconnects"] = int(self.metrics["coinbase_reconnects"]) + 1
            if periodic_resync:
                delay = 1.0
            await asyncio.sleep(delay + random.uniform(0.0, min(delay, 1.0)))
            delay = min(delay * 2.0, 30.0)

    def snapshot(self, symbol: str, *, now: float | None = None) -> dict[str, Any]:
        normalized = _symbol(symbol)
        if normalized not in self.symbols:
            return {
                "available": False,
                "symbol": normalized,
                "reason": "symbol_not_subscribed_to_shared_multi_venue_stream",
                "venues": {},
            }
        cache_at = monotonic() if now is None else None
        if cache_at is not None:
            cached = self._snapshot_cache.get(normalized)
            if cached and cache_at - cached[0] <= 0.5:
                return deepcopy(cached[1])
            now = cache_at

        venue_snapshots = {
            venue: self.states[(venue, normalized)].snapshot(
                stale_seconds=self.stale_seconds,
                trade_window_seconds=self.trade_window_seconds,
                liquidation_window_seconds=self.liquidation_window_seconds,
                flow_warmup_seconds=self.flow_warmup_seconds,
                min_flow_trades=self.min_flow_trades,
                min_flow_notional=self.min_flow_notional,
                now=now,
            )
            for venue in ("bybit", "coinbase")
        }
        fresh = [item for item in venue_snapshots.values() if item["available"]]
        flow_values = {
            str(item["venue"]): _number((item.get("trade_flow") or {}).get("signed_flow"))
            for item in fresh
            if (item.get("trade_flow") or {}).get("available")
        }
        flows = list(flow_values.values())
        depth = [
            _number((item.get("order_book") or {}).get("persistent_imbalance"))
            for item in fresh
            if (item.get("order_book") or {}).get("persistent_imbalance") is not None
        ]
        flow_score = sum(flows) / len(flows) if flows else 0.0
        depth_score = sum(depth) / len(depth) if depth else 0.0
        flow_biases = {
            venue: "BULLISH" if value >= 0.05 else "BEARISH" if value <= -0.05 else "NEUTRAL"
            for venue, value in flow_values.items()
        }
        if len(flow_biases) >= 2:
            unique_biases = set(flow_biases.values())
            flow_consensus = next(iter(unique_biases)) if len(unique_biases) == 1 else "MIXED"
        else:
            # One venue is evidence, but it is not cross-venue consensus.
            flow_consensus = "UNAVAILABLE"
        single_venue_flow_bias = next(iter(flow_biases.values())) if len(flow_biases) == 1 else None
        mid_prices = [
            _number((item.get("order_book") or {}).get("mid_price"))
            for item in fresh
            if _number((item.get("order_book") or {}).get("mid_price")) > 0
        ]
        mean_price = sum(mid_prices) / len(mid_prices) if mid_prices else 0.0
        dispersion_bps = (
            (max(mid_prices) - min(mid_prices)) / mean_price * 10_000
            if len(mid_prices) >= 2 and mean_price else None
        )
        bybit_liquidations = (venue_snapshots.get("bybit") or {}).get("liquidations", {})
        result = {
            "available": bool(fresh),
            "symbol": normalized,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "operational_metrics": dict(self.metrics),
            "status": "HEALTHY" if len(fresh) >= 2 else "DEGRADED" if fresh else "UNAVAILABLE",
            "fresh_venue_count": len(fresh),
            "required_venue_count": 2,
            "cross_venue_confirmed": len(fresh) >= 2,
            "flow_venue_count": len(flow_values),
            "flow_confirmed": len(flow_values) >= 2,
            "venue_flow_biases": flow_biases,
            "single_venue_flow_bias": single_venue_flow_bias,
            "flow_score": round(flow_score, 4),
            "flow_consensus": flow_consensus,
            "depth_score": round(depth_score, 4),
            "price_dispersion_bps": round(dispersion_bps, 3) if dispersion_bps is not None else None,
            "observed_liquidations": bybit_liquidations,
            "venues": venue_snapshots,
            "limitations": [
                "Bybit evidence is perpetual-futures activity while Coinbase evidence is spot activity.",
                "Public books expose displayed liquidity, not hidden orders or guaranteed executable size.",
            ],
        }
        if cache_at is not None:
            self._snapshot_cache[normalized] = (cache_at, result)
            return deepcopy(result)
        return result



_HUB: MultiVenueMarketDataHub | None = None


def get_multi_venue_hub(settings: Settings | None = None) -> MultiVenueMarketDataHub:
    global _HUB
    if _HUB is None:
        _HUB = MultiVenueMarketDataHub(settings)
    return _HUB


def get_multi_venue_snapshot(symbol: str, settings: Settings | None = None) -> dict[str, Any]:
    active_settings = settings or get_settings()
    if not active_settings.multi_venue_ws_enabled:
        return {
            "available": False,
            "status": "DISABLED",
            "symbol": _symbol(symbol),
            "reason": "multi_venue_ws_disabled",
            "venues": {},
        }
    return get_multi_venue_hub(active_settings).snapshot(symbol)


async def multi_venue_market_data_loop() -> None:
    """Run both free public collectors until application shutdown."""
    settings = get_settings()
    if not settings.multi_venue_ws_enabled:
        logger.info("Multi-venue public WebSocket collection is disabled.")
        return
    hub = get_multi_venue_hub(settings)
    if not hub.symbols:
        logger.warning("Multi-venue public WebSocket collection has no valid configured symbols.")
        return
    while True:
        tasks = [
            asyncio.create_task(hub.run_bybit(), name="bybit-public-market-data"),
            asyncio.create_task(hub.run_coinbase(), name="coinbase-public-market-data"),
        ]
        try:
            done, _ = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            completed = next(iter(done))
            if completed.cancelled():
                raise RuntimeError(f"Collector task {completed.get_name()} was cancelled unexpectedly")
            error = completed.exception()
            if error is not None:
                raise error
            raise RuntimeError(f"Collector task {completed.get_name()} exited unexpectedly")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Public market-data collector exited; restarting both venues.")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(1.0)
