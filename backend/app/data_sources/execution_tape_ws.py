"""Shared public execution-tape intelligence from Binance and Bybit.

The application owns four bounded process-wide collectors:

* Binance USDT spot
* Binance USD-M perpetual
* Bybit USDT spot
* Bybit USDT perpetual

Every user reads the same normalized rolling tape.  No connection is opened
per browser, account, Radar request, or research request.
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


class SubscriptionError(RuntimeError):
    """Raised when a public stream rejects or fails a subscription."""


class BookIntegrityError(RuntimeError):
    """Raised when an incremental stream produces an impossible order book."""


SOURCE_SPECS: dict[str, dict[str, str]] = {
    "binance_perp": {
        "exchange": "BINANCE",
        "market": "PERPETUAL",
        "url_setting": "binance_perp_public_ws_url",
    },
    "bybit_perp": {
        "exchange": "BYBIT",
        "market": "PERPETUAL",
        "url_setting": "bybit_perp_public_ws_url",
    },
    "binance_spot": {
        "exchange": "BINANCE",
        "market": "SPOT",
        "url_setting": "binance_spot_public_ws_url",
    },
    "bybit_spot": {
        "exchange": "BYBIT",
        "market": "SPOT",
        "url_setting": "bybit_spot_public_ws_url",
    },
}
PERPETUAL_SOURCES = ("binance_perp", "bybit_perp")
SPOT_SOURCES = ("binance_spot", "bybit_spot")
MIN_PUBLICATION_SOURCES = 2
MIN_PUBLICATION_VENUES = 2


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if isfinite(result) else default


def _symbol(value: str) -> str:
    return str(value or "").upper().replace("-", "").strip()


def _source_time_is_fresh(value: Any, max_age_seconds: float) -> bool:
    if value in (None, ""):
        return False
    try:
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1_000.0
    except (TypeError, ValueError, OverflowError):
        return False
    age = time() - timestamp
    return -5.0 <= age <= max(1.0, max_age_seconds)


def _flow_bias(signed_flow: float) -> str:
    if signed_flow >= 0.08:
        return "BULLISH"
    if signed_flow <= -0.08:
        return "BEARISH"
    return "NEUTRAL"


def _classify_pressure(
    signed_flow: float,
    price_change_bps: float | None,
    exhaustion: str,
) -> tuple[str, str, str]:
    """Return verdict, aggressor and price-response labels."""
    price_move = price_change_bps if price_change_bps is not None else 0.0
    if signed_flow >= 0.12 and price_move <= 1.0:
        return "BUYERS_ABSORBED", "BUYERS", "NO_UPWARD_PROGRESS"
    if signed_flow <= -0.12 and price_move >= -1.0:
        return "SELLERS_ABSORBED", "SELLERS", "NO_DOWNWARD_PROGRESS"
    if exhaustion != "NONE":
        return exhaustion, "BUYERS" if exhaustion == "BUYER_EXHAUSTION" else "SELLERS", "FADING"
    if signed_flow >= 0.08 and price_move > 1.0:
        return "BUYING_CONFIRMED", "BUYERS", "ACCEPTING_HIGHER"
    if signed_flow <= -0.08 and price_move < -1.0:
        return "SELLING_CONFIRMED", "SELLERS", "ACCEPTING_LOWER"
    if signed_flow >= 0.08:
        return "BUYING_NO_PROGRESS", "BUYERS", "STALLED"
    if signed_flow <= -0.08:
        return "SELLING_NO_PROGRESS", "SELLERS", "STALLED"
    return "BALANCED", "BALANCED", "RANGE_BOUND"


class _TapeInstrumentState:
    """Bounded rolling trade and displayed-book state for one market source."""

    def __init__(
        self,
        *,
        source: str,
        symbol: str,
        max_levels: int,
        max_events: int,
        max_window_seconds: float,
        min_book_levels: int,
        large_trade_notional: float,
    ) -> None:
        self.source = source
        self.symbol = symbol
        self.exchange = SOURCE_SPECS[source]["exchange"]
        self.market = SOURCE_SPECS[source]["market"]
        self.max_levels = max(20, min(int(max_levels), 1_000))
        self.min_book_levels = max(1, min(int(min_book_levels), self.max_levels))
        bucket_limit = max(120, min(int(max_window_seconds) + 5, 1_805))
        dedupe_size = max(100, min(int(max_events), 10_000))
        self.large_trade_notional = max(1_000.0, float(large_trade_notional))
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.trades: deque[dict[str, Any]] = deque(maxlen=bucket_limit)
        self.liquidations: deque[dict[str, Any]] = deque(maxlen=bucket_limit)
        self.book_events: deque[dict[str, Any]] = deque(maxlen=bucket_limit)
        self.imbalance_samples: deque[tuple[float, float]] = deque(maxlen=bucket_limit)
        self._trade_ids: deque[str] = deque(maxlen=dedupe_size)
        self._trade_id_set: set[str] = set()
        self._liquidation_ids: deque[str] = deque(maxlen=dedupe_size)
        self._liquidation_id_set: set[str] = set()
        self.connected = False
        self.book_ready = False
        self.liquidation_stream_ready = False
        self.health_reason = "DISCONNECTED"
        self.connected_at = 0.0
        self.last_transport_at = 0.0
        self.last_book_at = 0.0
        self.last_trade_at = 0.0
        self.last_liquidation_at = 0.0
        self.last_sample_at = 0.0
        self.last_update_id: int | None = None
        self.last_sequence: int | None = None
        self.received_at: str | None = None

    def disconnect(self, reason: str = "DISCONNECTED") -> None:
        self.connected = False
        self.book_ready = False
        self.liquidation_stream_ready = False
        self.health_reason = reason
        self.bids.clear()
        self.asks.clear()
        self.trades.clear()
        self.liquidations.clear()
        self.book_events.clear()
        self.imbalance_samples.clear()
        self._trade_ids.clear()
        self._trade_id_set.clear()
        self._liquidation_ids.clear()
        self._liquidation_id_set.clear()
        self.connected_at = 0.0
        self.last_transport_at = 0.0
        self.last_book_at = 0.0
        self.last_trade_at = 0.0
        self.last_liquidation_at = 0.0
        self.last_sample_at = 0.0
        self.last_update_id = None
        self.last_sequence = None
        self.received_at = None

    def mark_connected(self, now: float | None = None) -> None:
        observed = monotonic() if now is None else now
        self.connected = True
        self.book_ready = False
        self.health_reason = "SYNCING"
        self.connected_at = observed
        self.touch_transport(observed)

    def touch_transport(self, now: float | None = None) -> None:
        observed = monotonic() if now is None else now
        self.last_transport_at = observed
        self.received_at = datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _remember(identifier: str, queue: deque[str], seen: set[str]) -> bool:
        if identifier in seen:
            return False
        if queue.maxlen and len(queue) >= queue.maxlen:
            seen.discard(queue[0])
        queue.append(identifier)
        seen.add(identifier)
        return True

    @staticmethod
    def _bucket(
        queue: deque[dict[str, Any]],
        observed: float,
        defaults: dict[str, Any],
    ) -> dict[str, Any]:
        second = int(observed)
        if queue and queue[-1]["second"] == second:
            queue[-1]["at"] = observed
            return queue[-1]
        row = {"second": second, "at": observed, **defaults}
        queue.append(row)
        return row

    def _touch(self, now: float | None = None) -> float:
        observed = monotonic() if now is None else now
        self.touch_transport(observed)
        return observed

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
            if price <= 0 or quantity < 0:
                continue
            previous = book.get(price, 0.0)
            if quantity == previous:
                continue
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
                bucket["removal_count" if quantity < previous else "addition_count"] += 1
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
            return
        self._apply_side(self.bids, bids, side="bid", now=observed, record_events=not snapshot)
        self._apply_side(self.asks, asks, side="ask", now=observed, record_events=not snapshot)
        self.bids = dict(sorted(self.bids.items(), reverse=True)[: self.max_levels])
        self.asks = dict(sorted(self.asks.items())[: self.max_levels])
        if self.bids and self.asks and max(self.bids) >= min(self.asks):
            self.bids.clear()
            self.asks.clear()
            self.book_ready = False
            self.health_reason = "CROSSED_BOOK"
            raise BookIntegrityError(f"{self.source} {self.symbol} produced a crossed order book")
        self.book_ready = (
            len(self.bids) >= self.min_book_levels
            and len(self.asks) >= self.min_book_levels
        )
        self.health_reason = "HEALTHY" if self.book_ready else "SHALLOW_BOOK"
        self.last_book_at = observed
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
            total = bid_notional + ask_notional
            self.imbalance_samples.append((
                observed,
                (bid_notional - ask_notional) / total if total else 0.0,
            ))

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
        side = str(taker_side).upper()
        if price_value <= 0 or size_value <= 0 or side not in {"BUY", "SELL"}:
            return
        if event_id and not self._remember(event_id, self._trade_ids, self._trade_id_set):
            return
        notional = price_value * size_value
        bucket = self._bucket(
            self.trades,
            observed,
            {
                "buy_notional": 0.0,
                "sell_notional": 0.0,
                "buy_count": 0,
                "sell_count": 0,
                "trade_count": 0,
                "first_price": None,
                "last_price": None,
                "largest_trade_notional": 0.0,
                "large_buy_count": 0,
                "large_sell_count": 0,
            },
        )
        bucket["buy_notional" if side == "BUY" else "sell_notional"] += notional
        bucket["buy_count" if side == "BUY" else "sell_count"] += 1
        bucket["trade_count"] += 1
        bucket["first_price"] = bucket["first_price"] or price_value
        bucket["last_price"] = price_value
        bucket["largest_trade_notional"] = max(bucket["largest_trade_notional"], notional)
        if notional >= self.large_trade_notional:
            bucket["large_buy_count" if side == "BUY" else "large_sell_count"] += 1
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
        side = str(position_side).upper()
        if price_value <= 0 or size_value <= 0 or side not in {"LONG", "SHORT"}:
            return
        if event_id and not self._remember(
            event_id, self._liquidation_ids, self._liquidation_id_set
        ):
            return
        bucket = self._bucket(
            self.liquidations,
            observed,
            {"long_notional": 0.0, "short_notional": 0.0, "event_count": 0},
        )
        bucket["long_notional" if side == "LONG" else "short_notional"] += price_value * size_value
        bucket["event_count"] += 1
        self.last_liquidation_at = observed

    @staticmethod
    def _summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
        buy = sum(row["buy_notional"] for row in rows)
        sell = sum(row["sell_notional"] for row in rows)
        total = buy + sell
        count = sum(row["trade_count"] for row in rows)
        first_price = next(
            (_number(row.get("first_price")) for row in rows if _number(row.get("first_price")) > 0),
            0.0,
        )
        last_price = next(
            (_number(row.get("last_price")) for row in reversed(rows) if _number(row.get("last_price")) > 0),
            0.0,
        )
        return {
            "buy_notional": buy,
            "sell_notional": sell,
            "total_notional": total,
            "net_delta": buy - sell,
            "trade_count": count,
            "signed_flow": (buy - sell) / total if total else 0.0,
            "first_price": first_price,
            "last_price": last_price,
            "price_change_bps": (
                (last_price - first_price) / first_price * 10_000
                if first_price and last_price else None
            ),
            "large_buy_count": sum(row["large_buy_count"] for row in rows),
            "large_sell_count": sum(row["large_sell_count"] for row in rows),
            "largest_trade_notional": max(
                (row["largest_trade_notional"] for row in rows),
                default=0.0,
            ),
        }

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
        transport_age = observed - self.last_transport_at if self.last_transport_at else None
        book_age = observed - self.last_book_at if self.last_book_at else None
        trade_age = observed - self.last_trade_at if self.last_trade_at else None
        uptime = max(0.0, observed - self.connected_at) if self.connected_at else 0.0
        book_fresh = bool(
            self.connected and self.book_ready
            and book_age is not None and book_age <= stale_seconds
        )
        rows = [row for row in self.trades if observed - row["at"] <= trade_window_seconds]
        summary = self._summarize_rows(rows)
        flow_available = bool(
            self.connected
            and trade_age is not None and trade_age <= stale_seconds
            and uptime >= flow_warmup_seconds
            and summary["trade_count"] >= min_flow_trades
            and summary["total_notional"] >= min_flow_notional
        )
        half_window = trade_window_seconds / 2.0
        recent_rows = [row for row in rows if observed - row["at"] <= half_window]
        prior_rows = [row for row in rows if half_window < observed - row["at"] <= trade_window_seconds]
        recent = self._summarize_rows(recent_rows)
        prior = self._summarize_rows(prior_rows)
        exhaustion = "NONE"
        if (
            prior["signed_flow"] >= 0.12
            and prior["total_notional"] > 0
            and recent["signed_flow"] <= 0.03
            and recent["total_notional"] <= prior["total_notional"] * 0.75
        ):
            exhaustion = "BUYER_EXHAUSTION"
        elif (
            prior["signed_flow"] <= -0.12
            and prior["total_notional"] > 0
            and recent["signed_flow"] >= -0.03
            and recent["total_notional"] <= prior["total_notional"] * 0.75
        ):
            exhaustion = "SELLER_EXHAUSTION"
        active_buckets = [
            row for row in rows
            if abs(row["buy_notional"] - row["sell_notional"]) > 0
        ]
        flow_sign = 1 if summary["signed_flow"] > 0 else -1 if summary["signed_flow"] < 0 else 0
        persistence = (
            sum(
                (row["buy_notional"] - row["sell_notional"] > 0) == (flow_sign > 0)
                for row in active_buckets
            ) / len(active_buckets)
            if active_buckets and flow_sign else 0.0
        )
        verdict, aggressor, price_response = _classify_pressure(
            summary["signed_flow"],
            summary["price_change_bps"],
            exhaustion,
        )

        bids = sorted(self.bids.items(), reverse=True)[:20]
        asks = sorted(self.asks.items())[:20]
        best_bid = bids[0][0] if bids else 0.0
        best_ask = asks[0][0] if asks else 0.0
        midpoint = (best_bid + best_ask) / 2.0 if best_bid and best_ask else 0.0
        bid_notional = sum(price * size for price, size in bids)
        ask_notional = sum(price * size for price, size in asks)
        depth_total = bid_notional + ask_notional
        book_rows = [row for row in self.book_events if observed - row["at"] <= trade_window_seconds]
        additions = sum(row["addition_count"] for row in book_rows)
        removals = sum(row["removal_count"] for row in book_rows)
        book_event_count = additions + removals
        imbalance_samples = [
            value for at, value in self.imbalance_samples
            if observed - at <= trade_window_seconds
        ]
        mean_imbalance = (
            sum(imbalance_samples) / len(imbalance_samples)
            if imbalance_samples else 0.0
        )
        persistence_ratio = (
            sum((value >= 0) == (mean_imbalance >= 0) for value in imbalance_samples)
            / len(imbalance_samples)
            if imbalance_samples else 0.0
        )
        removal_ratio = removals / book_event_count if book_event_count else None
        stability_qualified = book_fresh and book_event_count >= 20 and len(imbalance_samples) >= 5
        stability_status = (
            "ELEVATED"
            if stability_qualified and removal_ratio is not None
            and removal_ratio >= 0.80 and persistence_ratio < 0.55
            else "STABLE"
            if stability_qualified
            else "UNAVAILABLE"
        )

        liquidation_rows = [
            row for row in self.liquidations
            if observed - row["at"] <= liquidation_window_seconds
        ]
        long_liquidated = sum(row["long_notional"] for row in liquidation_rows)
        short_liquidated = sum(row["short_notional"] for row in liquidation_rows)
        liquidation_events = sum(row["event_count"] for row in liquidation_rows)
        liquidation_available = bool(
            self.market == "PERPETUAL"
            and self.connected
            and self.liquidation_stream_ready
            and transport_age is not None
            and transport_age <= stale_seconds
        )
        health = (
            "DISCONNECTED"
            if not self.connected
            else "HEALTHY"
            if flow_available and book_fresh
            else "FLOW_ONLY"
            if flow_available
            else "BOOK_ONLY"
            if book_fresh
            else self.health_reason
        )
        return {
            "source": self.source,
            "exchange": self.exchange,
            "market": self.market,
            "symbol": self.symbol,
            "available": flow_available or book_fresh,
            "connected": self.connected,
            "health": health,
            "transport_age_seconds": round(transport_age, 3) if transport_age is not None else None,
            "book_age_seconds": round(book_age, 3) if book_age is not None else None,
            "trade_age_seconds": round(trade_age, 3) if trade_age is not None else None,
            "received_at": self.received_at,
            "order_book": {
                "ready": book_fresh,
                "best_bid": best_bid or None,
                "best_ask": best_ask or None,
                "mid_price": round(midpoint, 8) if midpoint else None,
                "spread_bps": (
                    round((best_ask - best_bid) / midpoint * 10_000, 3)
                    if midpoint else None
                ),
                "depth_imbalance": (
                    round((bid_notional - ask_notional) / depth_total, 4)
                    if depth_total else None
                ),
                "persistent_imbalance": (
                    round(mean_imbalance, 4) if imbalance_samples else None
                ),
                "book_event_count": book_event_count,
                "displayed_liquidity_stability": {
                    "qualified": stability_qualified,
                    "status": stability_status,
                    "removal_ratio": (
                        round(removal_ratio, 4)
                        if removal_ratio is not None else None
                    ),
                    "limitation": (
                        "Displayed-quote instability is a cancellation-risk proxy, "
                        "not proof of spoofing."
                    ),
                },
            },
            "trade_flow": {
                "available": flow_available,
                "window_seconds": trade_window_seconds,
                "age_seconds": round(trade_age, 3) if trade_age is not None else None,
                "warmup_complete": uptime >= flow_warmup_seconds,
                "trade_count": summary["trade_count"],
                "buy_notional": round(summary["buy_notional"], 2),
                "sell_notional": round(summary["sell_notional"], 2),
                "qualified_notional": round(summary["total_notional"], 2),
                "net_delta_usd": round(summary["net_delta"], 2),
                "signed_flow": round(summary["signed_flow"], 4),
                "aggressive_buy_ratio": (
                    round(summary["buy_notional"] / summary["total_notional"], 4)
                    if summary["total_notional"] else None
                ),
                "cvd_trend": (
                    "RISING" if summary["signed_flow"] >= 0.03
                    else "FALLING" if summary["signed_flow"] <= -0.03
                    else "FLAT"
                ),
                "persistence_ratio": round(persistence, 3),
                "price_change_bps": (
                    round(summary["price_change_bps"], 3)
                    if summary["price_change_bps"] is not None else None
                ),
                "active_aggressor": aggressor,
                "price_response": price_response,
                "verdict": verdict,
                "bias": _flow_bias(summary["signed_flow"]),
                "absorption": (
                    verdict if verdict in {"BUYERS_ABSORBED", "SELLERS_ABSORBED"}
                    else "NOT_DETECTED"
                ),
                "exhaustion": exhaustion,
                "large_buy_count": summary["large_buy_count"],
                "large_sell_count": summary["large_sell_count"],
                "largest_trade_notional": round(summary["largest_trade_notional"], 2),
            },
            "liquidations": {
                "available": liquidation_available,
                "observed": liquidation_events > 0,
                "window_seconds": liquidation_window_seconds,
                "event_count": liquidation_events,
                "long_liquidated_notional": round(long_liquidated, 2),
                "short_liquidated_notional": round(short_liquidated, 2),
                "net_short_minus_long": round(short_liquidated - long_liquidated, 2),
            },
        }


def _aggregate_flow(rows: list[dict[str, Any]], market: str) -> dict[str, Any]:
    qualified = [
        row for row in rows
        if (row.get("trade_flow") or {}).get("available") is True
    ]
    buy = sum(_number((row["trade_flow"]).get("buy_notional")) for row in qualified)
    sell = sum(_number((row["trade_flow"]).get("sell_notional")) for row in qualified)
    total = buy + sell
    signed = (buy - sell) / total if total else 0.0
    price_rows = [
        (
            _number((row["trade_flow"]).get("price_change_bps")),
            _number((row["trade_flow"]).get("qualified_notional")),
        )
        for row in qualified
        if row["trade_flow"].get("price_change_bps") is not None
    ]
    price_weight = sum(weight for _, weight in price_rows)
    price_change = (
        sum(value * weight for value, weight in price_rows) / price_weight
        if price_weight else None
    )
    exhaustion_rows = [
        str(row["trade_flow"].get("exhaustion", "NONE"))
        for row in qualified
        if str(row["trade_flow"].get("exhaustion", "NONE")) != "NONE"
    ]
    exhaustion = exhaustion_rows[0] if exhaustion_rows else "NONE"
    verdict, aggressor, price_response = _classify_pressure(signed, price_change, exhaustion)
    source_biases = {
        str(row["source"]): str(row["trade_flow"].get("bias", "NEUTRAL"))
        for row in qualified
    }
    venues = sorted({str(row.get("exchange") or "").upper() for row in qualified if row.get("exchange")})
    venue_biases: dict[str, str] = {}
    for venue in venues:
        venue_rows = [row for row in qualified if str(row.get("exchange") or "").upper() == venue]
        venue_buy = sum(_number(row["trade_flow"].get("buy_notional")) for row in venue_rows)
        venue_sell = sum(_number(row["trade_flow"].get("sell_notional")) for row in venue_rows)
        venue_total = venue_buy + venue_sell
        venue_biases[venue] = _flow_bias(
            (venue_buy - venue_sell) / venue_total if venue_total else 0.0
        )
    directional_venue_biases = {
        value for value in venue_biases.values() if value in {"BULLISH", "BEARISH"}
    }
    cross_venue_alignment = (
        "UNAVAILABLE"
        if len(venues) < MIN_PUBLICATION_VENUES
        else "DIVERGENT"
        if len(directional_venue_biases) > 1
        else "ALIGNED"
        if len(directional_venue_biases) == 1
        else "NEUTRAL"
    )
    return {
        "available": bool(qualified),
        "market": market,
        "qualified_source_count": len(qualified),
        "qualified_venue_count": len(venues),
        "sources": [row["source"] for row in qualified],
        "venues": venues,
        "trade_count": sum(int(row["trade_flow"].get("trade_count") or 0) for row in qualified),
        "buy_notional": round(buy, 2),
        "sell_notional": round(sell, 2),
        "total_notional": round(total, 2),
        "net_delta_usd": round(buy - sell, 2),
        "signed_flow": round(signed, 4),
        "aggressive_buy_ratio": round(buy / total, 4) if total else None,
        "bias": _flow_bias(signed),
        "active_aggressor": aggressor,
        "price_change_bps": round(price_change, 3) if price_change is not None else None,
        "price_response": price_response,
        "verdict": verdict,
        "cvd_trend": "RISING" if signed >= 0.03 else "FALLING" if signed <= -0.03 else "FLAT",
        "absorption": (
            verdict if verdict in {"BUYERS_ABSORBED", "SELLERS_ABSORBED"}
            else "NOT_DETECTED"
        ),
        "exhaustion": exhaustion,
        "large_buy_count": sum(int(row["trade_flow"].get("large_buy_count") or 0) for row in qualified),
        "large_sell_count": sum(int(row["trade_flow"].get("large_sell_count") or 0) for row in qualified),
        "largest_trade_notional": round(max(
            (_number(row["trade_flow"].get("largest_trade_notional")) for row in qualified),
            default=0.0,
        ), 2),
        "persistence_ratio": round(
            sum(_number(row["trade_flow"].get("persistence_ratio")) for row in qualified)
            / len(qualified),
            3,
        ) if qualified else 0.0,
        "source_biases": source_biases,
        "venue_biases": venue_biases,
        "cross_venue_alignment": cross_venue_alignment,
    }


def publication_flow_is_qualified(execution_tape: dict[str, Any] | None) -> bool:
    """Require fresh actual flow from two exchanges without venue disagreement."""
    tape = execution_tape if isinstance(execution_tape, dict) else {}
    actual_flow = tape.get("actual_flow") or {}
    return bool(
        actual_flow.get("available")
        and int(actual_flow.get("qualified_source_count") or 0) >= MIN_PUBLICATION_SOURCES
        and int(actual_flow.get("qualified_venue_count") or 0) >= MIN_PUBLICATION_VENUES
        and str(actual_flow.get("cross_venue_alignment") or "UNAVAILABLE").upper()
        != "DIVERGENT"
    )


class ExecutionTapeHub:
    """One shared four-source execution tape per application process."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        endpoints = [
            str(getattr(self.settings, spec["url_setting"]))
            for spec in SOURCE_SPECS.values()
        ]
        if self.settings.app_env.lower() not in {"local", "test", "development"}:
            if any(not endpoint.lower().startswith("wss://") for endpoint in endpoints):
                raise ValueError("Public execution-tape endpoints must use wss:// outside development")
        self.stale_seconds = max(2.0, min(float(self.settings.multi_venue_stale_seconds), 120.0))
        self.trade_window_seconds = max(5.0, min(float(self.settings.multi_venue_trade_window_seconds), 300.0))
        self.liquidation_window_seconds = max(30.0, min(float(self.settings.multi_venue_liquidation_window_seconds), 1_800.0))
        self.flow_warmup_seconds = max(1.0, min(float(self.settings.multi_venue_flow_warmup_seconds), 300.0))
        self.min_flow_trades = max(1, min(int(self.settings.multi_venue_min_flow_trades), 10_000))
        self.min_flow_notional = max(0.0, float(self.settings.multi_venue_min_flow_notional_usd))
        self.max_event_lag_seconds = max(1.0, min(float(self.settings.multi_venue_max_event_lag_seconds), 60.0))
        self.subscription_retry_seconds = max(60.0, min(float(self.settings.multi_venue_subscription_retry_seconds), 3_600.0))
        self.max_symbols = max(1, min(int(self.settings.multi_venue_max_symbols), 12))
        self.symbol_idle_seconds = max(0.0, min(float(self.settings.multi_venue_symbol_idle_seconds), 600.0))
        configured: list[str] = []
        for item in self.settings.multi_venue_symbols:
            normalized = _symbol(item)
            if normalized.endswith("USDT") and normalized not in configured:
                configured.append(normalized)
        self.symbols = configured[: self.max_symbols]
        self.states: dict[tuple[str, str], _TapeInstrumentState] = {}
        observed = monotonic() - self.symbol_idle_seconds - 1.0
        self._symbol_last_requested = {
            symbol: observed + index * 1e-6
            for index, symbol in enumerate(self.symbols)
        }
        self._subscription_generation = 0
        self._rejected: dict[str, dict[str, float]] = {
            source: {} for source in SOURCE_SPECS
        }
        self._pending_binance: dict[str, dict[int, str]] = {
            source: {} for source in SOURCE_SPECS if source.startswith("binance_")
        }
        self.metrics: dict[str, int] = {
            **{f"{source}_reconnects": 0 for source in SOURCE_SPECS},
            "subscription_errors": 0,
            "stale_events_dropped": 0,
            "dynamic_symbol_registrations": 0,
            "dynamic_symbol_evictions": 0,
            "subscription_refreshes": 0,
        }
        self._snapshot_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._configure_states()

    def _new_state(self, source: str, symbol: str) -> _TapeInstrumentState:
        state = _TapeInstrumentState(
            source=source,
            symbol=symbol,
            max_levels=self.settings.multi_venue_book_levels,
            max_events=self.settings.multi_venue_max_events,
            max_window_seconds=max(
                self.trade_window_seconds,
                self.liquidation_window_seconds,
            ),
            min_book_levels=self.settings.multi_venue_min_book_levels,
            large_trade_notional=self.settings.execution_tape_large_trade_notional_usd,
        )
        state.health_reason = "SUBSCRIBING"
        return state

    def _configure_states(self) -> None:
        for source in SOURCE_SPECS:
            for symbol in self.symbols:
                self.states[(source, symbol)] = self._new_state(source, symbol)

    def ensure_symbol(self, symbol: str) -> dict[str, Any]:
        normalized = _symbol(symbol)
        if (
            not normalized.endswith("USDT")
            or len(normalized) <= 4
            or len(normalized) > 24
            or not normalized.isalnum()
        ):
            return {
                "registered": False,
                "symbol": normalized,
                "reason": "invalid_usdt_symbol",
                "evicted_symbol": None,
            }
        observed = monotonic()
        if normalized in self.symbols:
            self._symbol_last_requested[normalized] = observed
            return {
                "registered": True,
                "symbol": normalized,
                "reason": "already_registered",
                "evicted_symbol": None,
            }
        evicted: str | None = None
        if len(self.symbols) >= self.max_symbols:
            oldest = min(
                self.symbols,
                key=lambda item: self._symbol_last_requested.get(item, 0.0),
            )
            if observed - self._symbol_last_requested.get(oldest, 0.0) < self.symbol_idle_seconds:
                return {
                    "registered": False,
                    "symbol": normalized,
                    "reason": "active_symbol_capacity_reached",
                    "evicted_symbol": None,
                }
            evicted = oldest
            self.symbols.remove(evicted)
            self._symbol_last_requested.pop(evicted, None)
            self._snapshot_cache.pop(evicted, None)
            for source in SOURCE_SPECS:
                self._rejected[source].pop(evicted, None)
                state = self.states.pop((source, evicted), None)
                if state:
                    state.disconnect("DYNAMICALLY_EVICTED")
            self.metrics["dynamic_symbol_evictions"] += 1
        self.symbols.append(normalized)
        self._symbol_last_requested[normalized] = observed
        for source in SOURCE_SPECS:
            self.states[(source, normalized)] = self._new_state(source, normalized)
        self._subscription_generation += 1
        self.metrics["dynamic_symbol_registrations"] += 1
        self._snapshot_cache.clear()
        logger.info(
            "Registered %s in the shared execution tape%s.",
            normalized,
            f"; evicted {evicted}" if evicted else "",
        )
        return {
            "registered": True,
            "symbol": normalized,
            "reason": "dynamic_registration_started",
            "evicted_symbol": evicted,
        }

    @property
    def quarantined_subscriptions(self) -> dict[str, list[str]]:
        return {
            source: sorted(symbols)
            for source, symbols in self._rejected.items()
        }

    def _active_symbols(self, source: str) -> list[str]:
        observed = monotonic()
        for symbol, retry_at in tuple(self._rejected[source].items()):
            if retry_at <= observed:
                self._rejected[source].pop(symbol, None)
        return [
            symbol for symbol in self.symbols
            if symbol not in self._rejected[source]
        ]

    def _state(self, source: str, symbol: str) -> _TapeInstrumentState | None:
        return self.states.get((source, _symbol(symbol)))

    def _set_connected(
        self,
        source: str,
        connected: bool,
        *,
        reason: str = "DISCONNECTED",
        now: float | None = None,
    ) -> None:
        self._snapshot_cache.clear()
        for (state_source, _), state in self.states.items():
            if state_source != source:
                continue
            if connected:
                state.mark_connected(now)
            else:
                state.disconnect(reason)

    def _touch_source(self, source: str, now: float | None = None) -> None:
        for (state_source, _), state in self.states.items():
            if state_source == source:
                state.touch_transport(now)

    def _source_event_is_fresh(self, value: Any) -> bool:
        fresh = _source_time_is_fresh(value, self.max_event_lag_seconds)
        if not fresh:
            self.metrics["stale_events_dropped"] += 1
        return fresh

    def _quarantine(self, source: str, symbol: str, reason: str) -> None:
        normalized = _symbol(symbol)
        if normalized not in self.symbols:
            return
        self._rejected[source][normalized] = monotonic() + self.subscription_retry_seconds
        state = self._state(source, normalized)
        if state:
            state.disconnect(reason)

    def process_binance_message(
        self,
        source: str,
        message: dict[str, Any],
        *,
        now: float | None = None,
    ) -> None:
        if source not in {"binance_spot", "binance_perp"}:
            return
        request_id = message.get("id")
        if request_id is not None and ("result" in message or "code" in message):
            symbol = self._pending_binance[source].pop(int(request_id), "")
            if "code" in message:
                self.metrics["subscription_errors"] += 1
                self._quarantine(source, symbol, "SUBSCRIPTION_REJECTED")
            else:
                self._touch_source(source, now)
            return
        stream = str(message.get("stream", ""))
        payload = message.get("data") if isinstance(message.get("data"), dict) else message
        event = str(payload.get("e", ""))
        stream_symbol = stream.split("@", 1)[0] if "@" in stream else ""
        symbol = _symbol(payload.get("s") or stream_symbol)
        state = self._state(source, symbol)
        if not state:
            return
        if "depth" in stream.lower() or event == "depthUpdate":
            source_time = payload.get("E") or payload.get("T")
            if source_time is not None and not self._source_event_is_fresh(source_time):
                return
            state.apply_book(
                bids=payload.get("bids") or payload.get("b") or [],
                asks=payload.get("asks") or payload.get("a") or [],
                snapshot="depth20" in stream.lower() or "lastUpdateId" in payload,
                update_id=int(
                    payload.get("lastUpdateId")
                    or payload.get("u")
                    or 0
                ) or None,
                now=now,
            )
            return
        if event == "aggTrade" or "aggtrade" in stream.lower():
            if not self._source_event_is_fresh(payload.get("T") or payload.get("E")):
                return
            state.record_trade(
                # m=True means the buyer rested as maker, so the seller was
                # the market-order aggressor.
                taker_side="SELL" if payload.get("m") is True else "BUY",
                price=payload.get("p"),
                size=payload.get("q"),
                event_id=str(payload.get("a", "")) or None,
                now=now,
            )
            return
        if event == "forceOrder" and source == "binance_perp":
            order = payload.get("o") or {}
            if not self._source_event_is_fresh(order.get("T") or payload.get("E")):
                return
            side = str(order.get("S", "")).upper()
            if side not in {"BUY", "SELL"}:
                return
            state.liquidation_stream_ready = True
            # Binance reports the forced order side: a forced sell closes a
            # long, and a forced buy closes a short.
            state.record_liquidation(
                position_side="LONG" if side == "SELL" else "SHORT",
                price=order.get("ap") or order.get("p"),
                size=order.get("z") or order.get("q"),
                event_id=f"{symbol}:{order.get('T')}:{side}:{order.get('z')}:{order.get('ap')}",
                now=now,
            )

    def process_bybit_message(
        self,
        source: str,
        message: dict[str, Any],
        *,
        now: float | None = None,
    ) -> None:
        if source not in {"bybit_spot", "bybit_perp"}:
            return
        if str(message.get("op", "")) == "subscribe":
            request_id = str(message.get("req_id", ""))
            symbol = request_id.rsplit(":", 1)[-1]
            if message.get("success") is not True:
                self.metrics["subscription_errors"] += 1
                self._quarantine(source, symbol, "SUBSCRIPTION_REJECTED")
            else:
                self._touch_source(source, now)
                state = self._state(source, symbol)
                if state and source == "bybit_perp":
                    state.liquidation_stream_ready = True
            return
        topic = str(message.get("topic", ""))
        data = message.get("data")
        if not topic:
            self._touch_source(source, now)
            return
        if topic.startswith("orderbook.") and isinstance(data, dict):
            source_time = message.get("cts") or message.get("ts")
            if not self._source_event_is_fresh(source_time):
                return
            state = self._state(source, str(data.get("s", "")))
            if state:
                state.apply_book(
                    bids=data.get("b", []) or [],
                    asks=data.get("a", []) or [],
                    snapshot=message.get("type") == "snapshot" or _number(data.get("u")) == 1,
                    update_id=int(data["u"]) if data.get("u") is not None else None,
                    sequence=int(data["seq"]) if data.get("seq") is not None else None,
                    now=now,
                )
            return
        if topic.startswith("publicTrade.") and isinstance(data, list):
            for trade in data:
                if not self._source_event_is_fresh(trade.get("T")):
                    continue
                state = self._state(source, str(trade.get("s", "")))
                if state:
                    state.record_trade(
                        taker_side=str(trade.get("S", "")),
                        price=trade.get("p"),
                        size=trade.get("v"),
                        event_id=str(trade.get("i", "")) or None,
                        now=now,
                    )
            return
        if topic.startswith("allLiquidation.") and source == "bybit_perp":
            events = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
            for index, event in enumerate(events):
                if not self._source_event_is_fresh(event.get("T")):
                    continue
                state = self._state(source, str(event.get("s", "")))
                if not state:
                    continue
                side = str(event.get("S", "")).upper()
                if side not in {"BUY", "SELL"}:
                    continue
                state.liquidation_stream_ready = True
                # Bybit documents Buy as a liquidated long position.
                state.record_liquidation(
                    position_side="LONG" if side == "BUY" else "SHORT",
                    price=event.get("p"),
                    size=event.get("v"),
                    event_id=f"{message.get('ts')}:{index}:{event.get('T')}:{event.get('s')}:{side}",
                    now=now,
                )

    @staticmethod
    async def _bybit_heartbeat(websocket: Any) -> None:
        while True:
            await asyncio.sleep(20)
            await websocket.send(json.dumps({"op": "ping"}))

    async def run_binance(self, source: str) -> None:
        url = str(getattr(self.settings, SOURCE_SPECS[source]["url_setting"]))
        delay = 1.0
        while True:
            failure_reason = "CONNECTION_CLOSED"
            subscription_refresh = False
            try:
                self._set_connected(source, False)
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                    max_queue=32,
                    max_size=2_000_000,
                ) as websocket:
                    self._set_connected(source, True)
                    generation = self._subscription_generation
                    active_symbols = self._active_symbols(source)
                    self._pending_binance[source].clear()
                    for index, symbol in enumerate(active_symbols, start=1):
                        streams = [
                            f"{symbol.lower()}@aggTrade",
                            f"{symbol.lower()}@depth20@100ms",
                        ]
                        if source == "binance_perp":
                            streams.append(f"{symbol.lower()}@forceOrder")
                            state = self._state(source, symbol)
                            if state:
                                state.liquidation_stream_ready = True
                        self._pending_binance[source][index] = symbol
                        await websocket.send(json.dumps({
                            "method": "SUBSCRIBE",
                            "params": streams,
                            "id": index,
                        }))
                    logger.info("%s execution tape connected for %s.", source, active_symbols)
                    while True:
                        if generation != self._subscription_generation:
                            failure_reason = "SUBSCRIPTION_REFRESH"
                            subscription_refresh = True
                            self.metrics["subscription_refreshes"] += 1
                            break
                        try:
                            raw = await asyncio.wait_for(websocket.recv(), timeout=2.0)
                        except asyncio.TimeoutError:
                            continue
                        self.process_binance_message(source, json.loads(raw))
                        delay = 1.0
            except asyncio.CancelledError:
                raise
            except (SubscriptionError, BookIntegrityError) as exc:
                failure_reason = type(exc).__name__.upper()
                logger.warning("%s rebuilding after validation error: %s", source, exc)
            except Exception as exc:
                logger.warning("%s public tape reconnecting after error: %s", source, exc)
            finally:
                self._set_connected(source, False, reason=failure_reason)
            self.metrics[f"{source}_reconnects"] += 1
            delay = 0.0 if subscription_refresh else min(delay * 2.0, 30.0)
            await asyncio.sleep(delay + random.uniform(0.0, min(delay, 1.0)))

    async def run_bybit(self, source: str) -> None:
        url = str(getattr(self.settings, SOURCE_SPECS[source]["url_setting"]))
        delay = 1.0
        while True:
            failure_reason = "CONNECTION_CLOSED"
            subscription_refresh = False
            heartbeat: asyncio.Task | None = None
            try:
                self._set_connected(source, False)
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                    max_queue=32,
                    max_size=2_000_000,
                ) as websocket:
                    self._set_connected(source, True)
                    generation = self._subscription_generation
                    active_symbols = self._active_symbols(source)
                    for symbol in active_symbols:
                        topics = [
                            f"orderbook.50.{symbol}",
                            f"publicTrade.{symbol}",
                        ]
                        if source == "bybit_perp":
                            topics.append(f"allLiquidation.{symbol}")
                        await websocket.send(json.dumps({
                            "op": "subscribe",
                            "req_id": f"tape:{source}:{symbol}",
                            "args": topics,
                        }))
                    heartbeat = asyncio.create_task(
                        self._bybit_heartbeat(websocket),
                        name=f"{source}-heartbeat",
                    )
                    logger.info("%s execution tape connected for %s.", source, active_symbols)
                    while True:
                        if generation != self._subscription_generation:
                            failure_reason = "SUBSCRIPTION_REFRESH"
                            subscription_refresh = True
                            self.metrics["subscription_refreshes"] += 1
                            break
                        try:
                            raw = await asyncio.wait_for(websocket.recv(), timeout=2.0)
                        except asyncio.TimeoutError:
                            continue
                        self.process_bybit_message(source, json.loads(raw))
                        delay = 1.0
            except asyncio.CancelledError:
                raise
            except (SubscriptionError, BookIntegrityError) as exc:
                failure_reason = type(exc).__name__.upper()
                logger.warning("%s rebuilding after validation error: %s", source, exc)
            except Exception as exc:
                logger.warning("%s public tape reconnecting after error: %s", source, exc)
            finally:
                if heartbeat:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
                self._set_connected(source, False, reason=failure_reason)
            self.metrics[f"{source}_reconnects"] += 1
            delay = 0.0 if subscription_refresh else min(delay * 2.0, 30.0)
            await asyncio.sleep(delay + random.uniform(0.0, min(delay, 1.0)))

    def snapshot(
        self,
        symbol: str,
        *,
        now: float | None = None,
        register: bool = True,
        touch: bool = True,
    ) -> dict[str, Any]:
        normalized = _symbol(symbol)
        registration = (
            self.ensure_symbol(normalized)
            if register
            else {
                "registered": normalized in self.symbols,
                "symbol": normalized,
                "reason": "registration_not_requested",
                "evicted_symbol": None,
            }
        )
        if normalized not in self.symbols:
            return {
                "available": False,
                "status": "UNAVAILABLE",
                "symbol": normalized,
                "reason": registration.get("reason", "symbol_not_registered"),
                "registration": registration,
                "sources": {},
                "actual_flow": {"available": False, "status": "UNAVAILABLE"},
            }
        if touch and not register:
            self._symbol_last_requested[normalized] = monotonic()
        cache_at = monotonic() if now is None else None
        if cache_at is not None:
            cached = self._snapshot_cache.get(normalized)
            if cached and cache_at - cached[0] <= 0.5:
                return deepcopy(cached[1])
            now = cache_at

        sources = {
            source: self.states[(source, normalized)].snapshot(
                stale_seconds=self.stale_seconds,
                trade_window_seconds=self.trade_window_seconds,
                liquidation_window_seconds=self.liquidation_window_seconds,
                flow_warmup_seconds=self.flow_warmup_seconds,
                min_flow_trades=self.min_flow_trades,
                min_flow_notional=self.min_flow_notional,
                now=now,
            )
            for source in SOURCE_SPECS
        }
        perp = _aggregate_flow([sources[key] for key in PERPETUAL_SOURCES], "PERPETUAL")
        spot = _aggregate_flow([sources[key] for key in SPOT_SOURCES], "SPOT")
        combined = _aggregate_flow(list(sources.values()), "COMBINED")
        if perp["available"] and spot["available"]:
            if perp["bias"] == spot["bias"] and perp["bias"] != "NEUTRAL":
                cross_market_alignment = "ALIGNED"
            elif (
                perp["bias"] != "NEUTRAL"
                and spot["bias"] != "NEUTRAL"
                and perp["bias"] != spot["bias"]
            ):
                cross_market_alignment = "DIVERGENT"
            else:
                cross_market_alignment = "MIXED"
        elif perp["available"]:
            cross_market_alignment = "PERPETUAL_ONLY"
        elif spot["available"]:
            cross_market_alignment = "SPOT_ONLY"
        else:
            cross_market_alignment = "UNAVAILABLE"
        qualified_count = combined["qualified_source_count"]
        qualified_venue_count = combined["qualified_venue_count"]
        publication_qualified = bool(
            combined["available"]
            and qualified_count >= MIN_PUBLICATION_SOURCES
            and qualified_venue_count >= MIN_PUBLICATION_VENUES
            and combined["cross_venue_alignment"] != "DIVERGENT"
        )
        confidence = (
            "HIGH"
            if publication_qualified and qualified_count >= 3 and cross_market_alignment == "ALIGNED"
            else "MEDIUM"
            if publication_qualified
            else "LOW"
            if qualified_count >= 1
            else "UNAVAILABLE"
        )
        actual_flow = {
            **combined,
            "status": combined["verdict"] if combined["available"] else "UNAVAILABLE",
            "confidence": confidence,
            "cross_market_alignment": cross_market_alignment,
            "perpetual": perp,
            "spot": spot,
            "method": "public_taker_trade_tape_v1",
            "production_qualified": publication_qualified,
            "limitations": (
                "Taker side identifies the aggressor, not participant identity or future intent."
            ),
        }
        liquidation_rows = [
            sources[source]["liquidations"]
            for source in PERPETUAL_SOURCES
            if sources[source]["liquidations"].get("available")
        ]
        liquidations = {
            "available": bool(liquidation_rows),
            "observed": any(bool(row.get("observed")) for row in liquidation_rows),
            "window_seconds": self.liquidation_window_seconds,
            "event_count": sum(int(row.get("event_count") or 0) for row in liquidation_rows),
            "long_liquidated_notional": round(sum(
                _number(row.get("long_liquidated_notional"))
                for row in liquidation_rows
            ), 2),
            "short_liquidated_notional": round(sum(
                _number(row.get("short_liquidated_notional"))
                for row in liquidation_rows
            ), 2),
        }
        liquidations["net_short_minus_long"] = round(
            liquidations["short_liquidated_notional"]
            - liquidations["long_liquidated_notional"],
            2,
        )
        stability_rows = [
            source["order_book"]["displayed_liquidity_stability"]
            for source in sources.values()
            if source["order_book"]["displayed_liquidity_stability"].get("qualified")
        ]
        elevated = [row for row in stability_rows if row.get("status") == "ELEVATED"]
        stability = {
            "status": (
                "ELEVATED" if len(elevated) >= 2
                else "WATCH" if elevated
                else "STABLE" if stability_rows
                else "UNAVAILABLE"
            ),
            "qualified_source_count": len(stability_rows),
            "elevated_source_count": len(elevated),
            # Quote behavior is context only. It never overrides measured
            # aggressive flow by itself.
            "publication_veto": False,
        }
        result = {
            "schema_version": "execution_tape.v1",
            "available": actual_flow["available"],
            "symbol": normalized,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "status": (
                "HEALTHY" if publication_qualified
                else "PARTIAL" if qualified_count >= 1
                else "SUBSCRIBING"
                if any(source["connected"] for source in sources.values())
                else "UNAVAILABLE"
            ),
            "registration": registration,
            "operational_metrics": dict(self.metrics),
            "actual_flow": actual_flow,
            "flow_confirmed": actual_flow["available"],
            "publication_flow_confirmed": publication_qualified,
            "flow_consensus": actual_flow["bias"],
            "flow_score": actual_flow["signed_flow"],
            "flow_source_count": qualified_count,
            "source_flow_biases": actual_flow["source_biases"],
            "fresh_source_count": sum(bool(source["available"]) for source in sources.values()),
            "required_source_count": MIN_PUBLICATION_SOURCES,
            "required_venue_count": MIN_PUBLICATION_VENUES,
            "observed_liquidations": liquidations,
            "displayed_liquidity_stability": stability,
            "sources": sources,
            "limitations": [
                "The verdict measures market-order aggression and price response; every execution still has both a buyer and seller.",
                "Partial or single-venue flow remains observable, but publication requires qualified Binance and Bybit evidence without cross-venue disagreement.",
                "Displayed books cannot reveal hidden liquidity or participant identity.",
            ],
        }
        if cache_at is not None:
            self._snapshot_cache[normalized] = (cache_at, result)
            return deepcopy(result)
        return result


_HUB: ExecutionTapeHub | None = None


def get_execution_tape_hub(settings: Settings | None = None) -> ExecutionTapeHub:
    global _HUB
    if _HUB is None:
        _HUB = ExecutionTapeHub(settings)
    return _HUB


def get_execution_tape_snapshot(
    symbol: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    active_settings = settings or get_settings()
    if not active_settings.multi_venue_ws_enabled:
        return {
            "schema_version": "execution_tape.v1",
            "available": False,
            "status": "DISABLED",
            "symbol": _symbol(symbol),
            "reason": "execution_tape_ws_disabled",
            "sources": {},
            "actual_flow": {"available": False, "status": "UNAVAILABLE"},
        }
    return get_execution_tape_hub(active_settings).snapshot(symbol)


async def execution_tape_market_data_loop() -> None:
    settings = get_settings()
    if not settings.multi_venue_ws_enabled:
        logger.info("Public execution-tape collection is disabled.")
        return
    hub = get_execution_tape_hub(settings)
    if not hub.symbols:
        logger.warning("Execution-tape collection has no configured USDT symbols.")
        return
    while True:
        tasks = [
            asyncio.create_task(hub.run_binance("binance_perp"), name="binance-perp-tape"),
            asyncio.create_task(hub.run_bybit("bybit_perp"), name="bybit-perp-tape"),
            asyncio.create_task(hub.run_binance("binance_spot"), name="binance-spot-tape"),
            asyncio.create_task(hub.run_bybit("bybit_spot"), name="bybit-spot-tape"),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            completed = next(iter(done))
            if completed.cancelled():
                raise RuntimeError(f"Collector {completed.get_name()} was cancelled unexpectedly")
            error = completed.exception()
            if error:
                raise error
            raise RuntimeError(f"Collector {completed.get_name()} exited unexpectedly")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Execution-tape collector exited; restarting all shared streams.")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(1.0)
