"""Snapshot microstructure features from public depth and aggregated trade flow."""
from __future__ import annotations

from typing import Any


def _levels_notional(levels: list[list[float]], depth: int) -> float:
    return sum(float(price) * float(size) for price, size in levels[:depth])


def _execution_tape_summary(execution_tape: dict[str, Any] | None) -> dict[str, Any]:
    data = execution_tape if isinstance(execution_tape, dict) else {}
    sources: dict[str, Any] = {}
    for source, payload in (data.get("sources") or {}).items():
        order_book = payload.get("order_book") or {}
        trade_flow = payload.get("trade_flow") or {}
        sources[source] = {
            "available": bool(payload.get("available")),
            "exchange": payload.get("exchange"),
            "market": payload.get("market"),
            "health": payload.get("health", "UNAVAILABLE"),
            "transport_age_seconds": payload.get("transport_age_seconds"),
            "spread_bps": order_book.get("spread_bps"),
            "depth_imbalance": order_book.get("depth_imbalance"),
            "persistent_imbalance": order_book.get("persistent_imbalance"),
            "book_event_count": order_book.get("book_event_count"),
            "displayed_liquidity_stability": order_book.get("displayed_liquidity_stability") or {},
            "trade_flow_available": bool(trade_flow.get("available")),
            "trade_flow_age_seconds": trade_flow.get("age_seconds"),
            "trade_count": trade_flow.get("trade_count"),
            "qualified_notional": trade_flow.get("qualified_notional"),
            "net_delta_usd": trade_flow.get("net_delta_usd"),
            "signed_flow": trade_flow.get("signed_flow"),
            "aggressive_buy_ratio": trade_flow.get("aggressive_buy_ratio"),
            "active_aggressor": trade_flow.get("active_aggressor"),
            "price_response": trade_flow.get("price_response"),
            "verdict": trade_flow.get("verdict"),
        }
    actual_flow = data.get("actual_flow", {}) or {}
    return {
        "available": bool(data.get("available")),
        "status": data.get("status", "UNAVAILABLE"),
        "actual_flow": actual_flow,
        "flow_confirmed": bool(actual_flow.get("available")),
        "flow_consensus": actual_flow.get("bias", "UNAVAILABLE"),
        "flow_score": actual_flow.get("signed_flow"),
        "flow_source_count": int(actual_flow.get("qualified_source_count") or 0),
        "observed_liquidations": data.get("observed_liquidations") or {
            "available": False,
            "observed": False,
        },
        "displayed_liquidity_stability": data.get("displayed_liquidity_stability") or {
            "status": "UNAVAILABLE",
            "qualified_source_count": 0,
            "elevated_source_count": 0,
            "publication_veto": False,
        },
        "limitations": list(data.get("limitations") or []),
        "sources": sources,
    }


def analyze_microstructure(
    order_book: dict[str, Any], candles: list[Any], depth: int = 20,
    multi_venue: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create execution-quality features with normalized live taker evidence.

    Completed Binance candles remain a historical fallback. The shared
    Binance/Bybit spot and perpetual tape supplies the current aggressor,
    delta, absorption, exhaustion, and price-response verdict.
    """
    execution_tape = _execution_tape_summary(multi_venue)
    bids = order_book.get("bids", [])[:depth]
    asks = order_book.get("asks", [])[:depth]
    if not bids or not asks:
        return {
            "available": False,
            "reason": "Bid/ask depth unavailable.",
            "execution_tape": execution_tape,
            "incremental_public_feeds": execution_tape,
        }

    best_bid, best_ask = float(bids[0][0]), float(asks[0][0])
    mid = (best_bid + best_ask) / 2 if best_bid + best_ask else 0.0
    bid_notional = _levels_notional(bids, depth)
    ask_notional = _levels_notional(asks, depth)
    total_depth = bid_notional + ask_notional
    imbalance = (bid_notional - ask_notional) / total_depth if total_depth else 0.0
    spread_bps = ((best_ask - best_bid) / mid * 10_000) if mid else 0.0

    recent = candles[-20:]
    buy_notional = sum(float(c.taker_buy_quote_volume) for c in recent)
    gross_notional = sum(float(c.quote_volume) for c in recent)
    sell_notional = max(gross_notional - buy_notional, 0.0)
    signed_flow = (buy_notional - sell_notional) / gross_notional if gross_notional else 0.0
    last = candles[-1] if candles else None
    return_change = ((last.close - last.open) / last.open) if last and last.open else 0.0
    # Large signed flow with muted price change is a cautious absorption proxy.
    absorption = abs(signed_flow) * max(0.0, 1.0 - min(abs(return_change) * 250, 1.0))
    absorption_state = (
        "PASSIVE_SELLER_ABSORPTION"
        if absorption >= 0.20 and signed_flow > 0
        else "PASSIVE_BUYER_ABSORPTION"
        if absorption >= 0.20 and signed_flow < 0
        else "NOT_DETECTED"
    )
    near_bid = _levels_notional(bids, min(5, len(bids)))
    near_ask = _levels_notional(asks, min(5, len(asks)))

    return {
        "available": True,
        "mid_price": round(mid, 8),
        "spread_bps": round(spread_bps, 3),
        "bid_depth_notional": round(bid_notional, 2),
        "ask_depth_notional": round(ask_notional, 2),
        "depth_imbalance": round(imbalance, 4),
        "near_touch_imbalance": round((near_bid - near_ask) / (near_bid + near_ask), 4) if near_bid + near_ask else 0.0,
        "aggressive_buy_ratio": round(buy_notional / gross_notional, 4) if gross_notional else 0.5,
        "signed_trade_flow": round(signed_flow, 4),
        "absorption_proxy": round(absorption, 4),
        "absorption_state": absorption_state,
        "liquidity_quality": "thin" if spread_bps > 15 else "normal" if total_depth else "unavailable",
        "execution_tape": execution_tape,
        # Compatibility for committee snapshots produced before this schema.
        "incremental_public_feeds": execution_tape,
        "limitations": [
            "Primary REST depth is a snapshot; live source books are contextual and cannot reveal hidden liquidity.",
            "Taker aggression identifies which side crossed the spread, not participant identity or future intent.",
        ],
    }
