"""Snapshot microstructure features from public depth and aggregated trade flow."""
from __future__ import annotations

from typing import Any


def _levels_notional(levels: list[list[float]], depth: int) -> float:
    return sum(float(price) * float(size) for price, size in levels[:depth])


def analyze_microstructure(order_book: dict[str, Any], candles: list[Any], depth: int = 20) -> dict[str, Any]:
    """Create execution-quality features without inferring a deterministic side.

    Binance kline taker volumes are an aggregated trade-flow proxy.  True queue
    position/absorption requires incremental order-book and trade streams; the
    response explicitly labels this proxy so it is not overstated.
    """
    bids = order_book.get("bids", [])[:depth]
    asks = order_book.get("asks", [])[:depth]
    if not bids or not asks:
        return {"available": False, "reason": "Bid/ask depth unavailable."}

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
        "liquidity_quality": "thin" if spread_bps > 15 else "normal" if total_depth else "unavailable",
        "limitations": [
            "Order-book values are a snapshot; queue changes require incremental depth capture.",
            "Absorption is an aggregated taker-flow proxy, not exchange-level order attribution.",
        ],
    }
