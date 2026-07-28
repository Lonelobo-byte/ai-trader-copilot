"""Central data aggregator — fetches ALL market intelligence concurrently.

This is the single entry-point for the AI Council.  It returns a unified
``MarketIntelligence`` dict containing every piece of data the AI agents
might need for their analysis.  Each sub-source handles its own failures
gracefully — a single source going down never crashes the aggregator.

Design principles:
  • Every external call runs concurrently via ``asyncio.gather``
  • Per-source caching with configurable TTL prevents redundant fetches
  • Shape is consistent even when individual sources fail (``available: False``)
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from copy import deepcopy
import logging
from time import monotonic
from typing import Any

from app.data_sources.binance_public import BinancePublicClient, Candle
from app.data_sources.binance_futures import BinanceFuturesClient
from app.data_sources.coinglass import fetch_derivatives_intelligence
from app.data_sources.gdelt import fetch_gdelt_news, fetch_global_news
from app.data_sources.macro import fetch_macro_data
from app.data_sources.sentiment import fetch_sentiment_snapshot
from app.data_sources.calendar import fetch_economic_events
from app.data_sources.global_liquidity import fetch_global_liquidity_index
from app.data_sources.multi_venue_ws import get_multi_venue_snapshot
from app.settings import Settings

logger = logging.getLogger(__name__)

# ── In-memory cache ──────────────────────────────────────────────────────────

_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_MAX_ENTRIES = 512

# A complete intelligence snapshot is much more expensive than the individual
# slow-source caches below: it opens several market-data requests at once.  A
# short TTL is enough because the websocket supplies the current ticker/order
# book independently.  The cache is bounded so a busy public deployment cannot
# grow process memory simply by receiving many unique symbol requests.
_SNAPSHOT_CACHE: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
_SNAPSHOT_INFLIGHT: dict[str, asyncio.Task[dict[str, Any]]] = {}
_SNAPSHOT_LOCK = asyncio.Lock()
_FETCH_GATES: dict[int, asyncio.Semaphore] = {}

DEFAULT_TTL = 30.0          # seconds for price-sensitive data
SLOW_TTL = 120.0            # seconds for slow-moving data (macro, sentiment)
NEWS_TTL = 300.0             # 5 minutes for news
NEWS_TIMEOUT_SECONDS = 3.0
SLOW_SOURCE_TIMEOUT_SECONDS = 5.0
DERIVATIVES_TIMEOUT_SECONDS = 5.0


def _cached(key: str, ttl: float) -> Any | None:
    """Return cached value if still fresh, else None."""
    entry = _CACHE.get(key)
    if entry and (monotonic() - entry[0]) <= ttl:
        return entry[1]
    return None


def _store(key: str, value: Any) -> None:
    if len(_CACHE) >= _CACHE_MAX_ENTRIES and key not in _CACHE:
        # Slow-source keys are naturally TTL-based; discard the oldest entry
        # when a public symbol spray would otherwise grow this cache forever.
        oldest = min(_CACHE, key=lambda item: _CACHE[item][0])
        _CACHE.pop(oldest, None)
    _CACHE[key] = (monotonic(), value)


def _snapshot_key(symbol: str, timeframe: str, candle_limit: int) -> str:
    return f"{symbol.upper().strip()}:{timeframe}:{max(60, min(int(candle_limit), 1000))}"


def _fetch_gate(limit: int) -> asyncio.Semaphore:
    """Return a process-local gate that bounds expensive fan-out fetches."""
    bounded_limit = max(1, min(int(limit), 32))
    gate = _FETCH_GATES.get(bounded_limit)
    if gate is None:
        gate = asyncio.Semaphore(bounded_limit)
        _FETCH_GATES[bounded_limit] = gate
    return gate

def attach_live_multi_venue_snapshot(
    intelligence: dict[str, Any], symbol: str, settings: Settings
) -> dict[str, Any]:
    """Attach the latest process-local public-feed evidence without I/O.

    Cached REST/context snapshots may be several seconds old. The shared hub
    is read in O(1) here so every analysis tick gets current feed health and
    evidence without opening another upstream connection.
    """
    try:
        snapshot = get_multi_venue_snapshot(symbol, settings)
    except Exception as exc:
        logger.warning("Multi-venue snapshot unavailable for %s: %s", symbol, exc)
        snapshot = {
            "available": False,
            "status": "UNAVAILABLE",
            "symbol": symbol.upper().strip(),
            "reason": "multi_venue_snapshot_error",
            "venues": {},
        }
    intelligence["multi_venue"] = snapshot
    meta = intelligence.setdefault("meta", {})
    available = [item for item in meta.get("sources_available", []) if item != "multi_venue_ws"]
    failed = [item for item in meta.get("sources_failed", []) if item != "multi_venue_ws"]
    (available if snapshot.get("available") else failed).append("multi_venue_ws")
    meta["sources_available"] = available
    meta["sources_failed"] = failed
    meta["total_sources"] = len(available) + len(failed)
    return intelligence


async def fetch_market_intelligence_cached(
    symbol: str,
    timeframe: str,
    settings: Settings,
    candle_limit: int = 200,
) -> dict[str, Any]:
    """Return an isolated, short-lived intelligence snapshot.

    Identical concurrent requests share one upstream fetch (single-flight).
    Callers receive a deep copy because the analysis pipeline intentionally
    replaces the snapshot's live candle/ticker/order-book values.
    """
    ttl = max(0.0, float(settings.market_snapshot_cache_seconds))
    max_entries = max(8, min(int(settings.market_snapshot_cache_max_entries), 512))
    key = _snapshot_key(symbol, timeframe, candle_limit)
    now = monotonic()

    async with _SNAPSHOT_LOCK:
        cached = _SNAPSHOT_CACHE.get(key)
        if cached and ttl and now - cached[0] <= ttl:
            _SNAPSHOT_CACHE.move_to_end(key)
            return attach_live_multi_venue_snapshot(deepcopy(cached[1]), symbol, settings)
        if cached:
            _SNAPSHOT_CACHE.pop(key, None)

        task = _SNAPSHOT_INFLIGHT.get(key)
        if task is None:
            gate = _fetch_gate(settings.market_intelligence_max_concurrency)

            async def build_snapshot() -> dict[str, Any]:
                async with gate:
                    result = await fetch_market_intelligence(symbol, timeframe, settings, candle_limit)
                async with _SNAPSHOT_LOCK:
                    _SNAPSHOT_CACHE[key] = (monotonic(), result)
                    _SNAPSHOT_CACHE.move_to_end(key)
                    while len(_SNAPSHOT_CACHE) > max_entries:
                        _SNAPSHOT_CACHE.popitem(last=False)
                return result

            task = asyncio.create_task(build_snapshot())
            _SNAPSHOT_INFLIGHT[key] = task

    try:
        result = await asyncio.shield(task)
        return attach_live_multi_venue_snapshot(deepcopy(result), symbol, settings)
    finally:
        if task.done():
            async with _SNAPSHOT_LOCK:
                if _SNAPSHOT_INFLIGHT.get(key) is task:
                    _SNAPSHOT_INFLIGHT.pop(key, None)


# ── Multi-timeframe candle map ───────────────────────────────────────────────

TIMEFRAME_HIERARCHY = {
    "1m":  ["5m", "15m", "1h"],
    "5m":  ["15m", "1h", "4h"],
    "15m": ["1h", "4h", "1d"],
    "1h":  ["4h", "1d"],
    "4h":  ["1d", "1w"],
    "1d":  ["1w"],
}


# ── Aggregator ───────────────────────────────────────────────────────────────


async def fetch_market_intelligence(
    symbol: str,
    timeframe: str,
    settings: Settings,
    candle_limit: int = 200,
) -> dict[str, Any]:
    """Fetch everything the AI Council needs.  Single async call.

    Returns a ``MarketIntelligence`` dict with guaranteed shape:
    {
        "symbol", "timeframe",
        "candles", "ticker", "order_book", "recent_trades",
        "multi_tf_candles": { "5m": [...], "1h": [...], ... },
        "funding", "open_interest", "options",
        "derivatives": { long_short_ratio, top_trader, taker_vol, oi_hist },
        "news", "macro", "sentiment", "calendar",
        "meta": { "fetch_time_ms", "sources_available", "sources_failed" },
    }
    """
    spot = BinancePublicClient(settings.binance_public_base_url)
    futures = BinanceFuturesClient(settings.binance_futures_base_url)

    # ── Identify higher timeframes for multi-TF analysis ─────────────────
    higher_tfs = TIMEFRAME_HIERARCHY.get(timeframe, ["1h", "4h"])

    # ── Schedule all fetches concurrently ────────────────────────────────
    start = monotonic()

    # Core price data (always fresh)
    safe_candle_limit = max(60, min(int(candle_limit), 1000))
    candles_task = spot.klines(symbol, timeframe, limit=safe_candle_limit)
    ticker_task = spot.ticker_24hr(symbol)
    order_book_task = spot.order_book(symbol, limit=100)
    trades_task = spot.recent_trades(symbol, limit=200)

    # Multi-timeframe candles
    mtf_tasks = {
        tf: asyncio.wait_for(
            spot.klines(symbol, tf, limit=200), timeout=DERIVATIVES_TIMEOUT_SECONDS
        )
        for tf in higher_tfs
    }

    # Derivatives data
    funding_task = asyncio.wait_for(
        futures.get_funding_rate(symbol), timeout=DERIVATIVES_TIMEOUT_SECONDS
    )
    oi_task = asyncio.wait_for(
        futures.get_open_interest(symbol), timeout=DERIVATIVES_TIMEOUT_SECONDS
    )
    derivatives_task = asyncio.wait_for(
        fetch_derivatives_intelligence(symbol), timeout=DERIVATIVES_TIMEOUT_SECONDS
    )

    # Slower data (use cache if fresh)
    cached_news = _cached(f"news:{symbol}", NEWS_TTL)
    cached_global = _cached("news_global", NEWS_TTL)
    cached_macro = _cached("macro", SLOW_TTL)
    cached_sentiment = _cached("sentiment", SLOW_TTL)
    cached_calendar = _cached("calendar", SLOW_TTL)
    cached_liquidity = _cached("global_liquidity", SLOW_TTL)

    # Optional context must never hold the core candle/order-book path hostage.
    # A timeout is recorded as a failed source and the signal continues with
    # explicit data-quality metadata.
    news_task = None if cached_news else asyncio.wait_for(
        fetch_gdelt_news(symbol, settings.gdelt_doc_api), timeout=NEWS_TIMEOUT_SECONDS
    )
    global_news_task = None if cached_global else asyncio.wait_for(
        fetch_global_news(settings.gdelt_doc_api), timeout=NEWS_TIMEOUT_SECONDS
    )
    macro_task = None if cached_macro else asyncio.wait_for(
        fetch_macro_data(), timeout=SLOW_SOURCE_TIMEOUT_SECONDS
    )
    sentiment_task = None if cached_sentiment else asyncio.wait_for(
        fetch_sentiment_snapshot(), timeout=SLOW_SOURCE_TIMEOUT_SECONDS
    )
    calendar_task = None if cached_calendar else asyncio.wait_for(
        fetch_economic_events(settings.app_env), timeout=SLOW_SOURCE_TIMEOUT_SECONDS
    )
    liquidity_task = None if cached_liquidity else asyncio.wait_for(
        fetch_global_liquidity_index(), timeout=SLOW_SOURCE_TIMEOUT_SECONDS
    )

    # Gather core data
    core_results = await asyncio.gather(
        candles_task, ticker_task, order_book_task, trades_task,
        funding_task, oi_task, derivatives_task,
        return_exceptions=True,
    )

    # Gather MTF candles
    mtf_keys = list(mtf_tasks.keys())
    mtf_results = await asyncio.gather(*mtf_tasks.values(), return_exceptions=True)

    # Gather slow data (only the ones not cached)
    slow_tasks = []
    slow_keys = []
    if news_task:
        slow_tasks.append(news_task)
        slow_keys.append("news")
    if global_news_task:
        slow_tasks.append(global_news_task)
        slow_keys.append("global_news")
    if macro_task:
        slow_tasks.append(macro_task)
        slow_keys.append("macro")
    if sentiment_task:
        slow_tasks.append(sentiment_task)
        slow_keys.append("sentiment")
    if calendar_task:
        slow_tasks.append(calendar_task)
        slow_keys.append("calendar")
    if liquidity_task:
        slow_tasks.append(liquidity_task)
        slow_keys.append("global_liquidity")

    slow_results = {}
    if slow_tasks:
        raw_slow = await asyncio.gather(*slow_tasks, return_exceptions=True)
        for key, res in zip(slow_keys, raw_slow):
            slow_results[key] = res

    # ── Unpack results with safe defaults ────────────────────────────────

    def safe(res: Any, default: Any) -> Any:
        return res if not isinstance(res, (Exception, BaseException)) else default

    sources_available = []
    sources_failed = []

    candles = safe(core_results[0], [])
    if candles:
        sources_available.append("candles")
    else:
        sources_failed.append("candles")

    ticker = safe(core_results[1], {})
    if ticker:
        sources_available.append("ticker")
    else:
        sources_failed.append("ticker")

    order_book = safe(core_results[2], {"bids": [], "asks": []})
    if order_book.get("bids"):
        sources_available.append("order_book")
    else:
        sources_failed.append("order_book")

    recent_trades = safe(core_results[3], [])
    if recent_trades:
        sources_available.append("recent_trades")
    else:
        sources_failed.append("recent_trades")

    funding = safe(core_results[4], {"funding_rate": 0.0})
    if "error" not in funding:
        sources_available.append("funding")
    else:
        sources_failed.append("funding")

    open_interest = safe(core_results[5], {"open_interest": 0.0})
    if "error" not in open_interest:
        sources_available.append("open_interest")
    else:
        sources_failed.append("open_interest")

    derivatives = safe(core_results[6], {})
    if derivatives:
        sources_available.append("derivatives")
    else:
        sources_failed.append("derivatives")

    # Multi-TF candles
    multi_tf_candles: dict[str, list[Candle]] = {}
    for tf_key, res in zip(mtf_keys, mtf_results):
        mtf_candles = safe(res, [])
        multi_tf_candles[tf_key] = mtf_candles
        if mtf_candles:
            sources_available.append(f"candles_{tf_key}")
        else:
            sources_failed.append(f"candles_{tf_key}")

    # Slow data — use cache or fresh result
    def resolve_slow(key: str, cached_val: Any, default: Any) -> Any:
        if cached_val is not None:
            return cached_val
        result = slow_results.get(key)
        if result is None:
            return default
        val = safe(result, default)
        
        # Determine cache storing paths
        if key == "news":
            cache_key = f"news:{symbol}"
        elif key == "global_news":
            cache_key = "news_global"
        else:
            cache_key = key
            
        _store(cache_key, val)
        return val

    news = resolve_slow("news", cached_news, [])
    if news:
        sources_available.append("news")
    else:
        sources_failed.append("news")

    global_news = resolve_slow("global_news", cached_global, [])
    if global_news:
        sources_available.append("global_news")
    else:
        sources_failed.append("global_news")

    macro = resolve_slow("macro", cached_macro, {})
    if macro and "error" not in macro:
        sources_available.append("macro")
    else:
        sources_failed.append("macro")

    sentiment = resolve_slow("sentiment", cached_sentiment, {"fear_greed": {"value": 50, "available": False}})
    if sentiment.get("fear_greed", {}).get("available"):
        sources_available.append("sentiment")
    else:
        sources_failed.append("sentiment")

    calendar = resolve_slow("calendar", cached_calendar, [])
    if calendar:
        sources_available.append("calendar")
    else:
        sources_failed.append("calendar")

    global_liquidity = resolve_slow(
        "global_liquidity",
        cached_liquidity,
        {"available": False, "risk_appetite_score": 50, "risk_appetite_status": "RISK_APPETITE_NEUTRAL"},
    )
    if global_liquidity.get("available"):
        sources_available.append("global_liquidity")
    else:
        sources_failed.append("global_liquidity")

    # Provider slots are explicit even before credentials/vendors are added.
    # This prevents downstream engines or an LLM from treating absent options
    # volatility-surface evidence as a neutral observation.
    options = {
        "available": False,
        "reason": "No options volatility-surface provider is configured.",
        "required_fields": ["atm_iv", "put_call_skew", "term_structure", "gamma_exposure", "dealer_positioning"],
    }
    sources_failed.append("options")

    elapsed_ms = round((monotonic() - start) * 1000, 1)

    intelligence: dict[str, Any] = {
        "symbol": symbol,
        "timeframe": timeframe,
        # Core price data
        "candles": candles,
        "ticker": ticker,
        "order_book": order_book,
        "recent_trades": recent_trades,
        # Multi-timeframe
        "multi_tf_candles": multi_tf_candles,
        # Derivatives
        "funding": funding,
        "open_interest": open_interest,
        "derivatives": derivatives,
        "options": options,
        # Context
        "news": news,
        "global_news": global_news,
        "macro": macro,
        "sentiment": sentiment,
        "calendar": calendar,
        "global_liquidity": global_liquidity,
        # Meta
        "meta": {
            "fetch_time_ms": elapsed_ms,
            "sources_available": sources_available,
            "sources_failed": sources_failed,
            "total_sources": len(sources_available) + len(sources_failed),
        },
    }

    logger.info(
        f"MarketIntelligence for {symbol}/{timeframe}: "
        f"{len(sources_available)}/{len(sources_available) + len(sources_failed)} sources OK "
        f"in {elapsed_ms}ms"
    )

    return attach_live_multi_venue_snapshot(intelligence, symbol, settings)


async def fetch_pair_discovery_data(settings: Settings) -> dict[str, Any]:
    """Fetch data needed for AI-driven pair discovery.

    Returns top volume/change pairs from Binance plus trending coins,
    so the AI can decide which pairs are worth scanning.
    """
    try:
        # Fetch all tickers to find high-volume/volatile pairs
        async with __import__("httpx").AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{settings.binance_public_base_url}/api/v3/ticker/24hr")
            resp.raise_for_status()
            all_tickers = resp.json()

        # Filter to USDT pairs only (most liquid)
        usdt_pairs = [
            t for t in all_tickers
            if t.get("symbol", "").endswith("USDT")
            and float(t.get("quoteVolume", 0)) > 1_000_000  # >$1M daily volume
        ]

        # Sort by quote volume descending
        usdt_pairs.sort(key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)

        # Top movers by absolute price change
        top_movers = sorted(
            usdt_pairs[:100],
            key=lambda x: abs(float(x.get("priceChangePercent", 0))),
            reverse=True,
        )[:20]

        # Top volume
        top_volume = usdt_pairs[:20]

        # Get trending from sentiment
        sentiment = await fetch_sentiment_snapshot()

        return {
            "top_volume_pairs": [
                {
                    "symbol": t["symbol"],
                    "volume_usd": round(float(t.get("quoteVolume", 0)), 0),
                    "change_pct": round(float(t.get("priceChangePercent", 0)), 2),
                    "last_price": float(t.get("lastPrice", 0)),
                }
                for t in top_volume
            ],
            "top_movers": [
                {
                    "symbol": t["symbol"],
                    "change_pct": round(float(t.get("priceChangePercent", 0)), 2),
                    "volume_usd": round(float(t.get("quoteVolume", 0)), 0),
                }
                for t in top_movers
            ],
            "trending_coins": sentiment.get("trending_coins", []),
            "fear_greed": sentiment.get("fear_greed", {}),
        }
    except Exception as exc:
        logger.error(f"Pair discovery data fetch failed: {exc}")
        return {
            "top_volume_pairs": [],
            "top_movers": [],
            "trending_coins": [],
            "fear_greed": {},
            "error": str(exc),
        }
