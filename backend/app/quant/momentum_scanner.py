import asyncio
import logging
import time
import httpx
from typing import List, Dict, Any, Tuple, Set

from app.data_sources.binance_public import Candle
from app.data_sources.execution_tape_ws import (
    get_execution_tape_snapshot,
    publication_flow_is_qualified,
)
from app.indicators.market_story import (
    build_market_story,
    evaluate_story_playbook,
    observable_liquidity_sweep,
    observable_structure_events,
)
from app.indicators.structure import (
    classify_market_phase,
    find_fair_value_gaps,
    find_order_blocks,
)
from app.quant.live_confirmation import apply_live_confirmation as _apply_live_confirmation
from app.quant.market_context import (
    build_liquidity_map,
    build_volume_profile,
    build_volatility_context,
    build_vwap_context,
    classify_positioning,
    score_market_context,
)

logger = logging.getLogger(__name__)

_RADAR_INTERVALS = {"5m", "15m", "1h", "4h", "1d"}
_KLINE_CONCURRENCY = 8
_ADVANCED_CONFIRMATION_MAX_CANDIDATES = 6
_ADVANCED_CONFIRMATION_CONCURRENCY = 2
_CAUSAL_HISTORY_CANDLES = 200


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _candles_from_klines(klines: List[List[Any]]) -> list[Candle]:
    """Adapt Futures kline rows to the shared deterministic structure tools."""
    return [
        Candle(
            open_time=int(row[0]),
            open=_float(row[1]),
            high=_float(row[2]),
            low=_float(row[3]),
            close=_float(row[4]),
            volume=_float(row[5]),
            close_time=int(row[6]),
            quote_volume=_float(row[7]),
            trade_count=int(row[8]),
            taker_buy_base_volume=_float(row[9]),
            taker_buy_quote_volume=_float(row[10]),
        )
        for row in klines
    ]


def analyze_radar_structure_confluence(candles: list[Candle], direction: str) -> Dict[str, Any]:
    """Add SMC-style context as a *soft* Radar confluence layer.

    Supply/demand order blocks, FVGs, sweep/stop-hunt patterns and operating
    phase are useful context, but none is reliable enough to be a standalone
    signal or a mandatory rule. The result is capped so price structure and
    live execution evidence remain the primary gates.
    """
    normalized = direction.lower()
    if normalized not in {"bullish", "bearish"} or len(candles) < 30:
        return {"score_adjustment": 0, "phase": "RANGING", "risk_flags": [], "supporting_factors": []}

    price = candles[-1].close
    story = build_market_story(candles)
    events = observable_structure_events(story)
    bos = events["bos"]
    choch = events["choch"]
    sweep = observable_liquidity_sweep(story)
    order_blocks = find_order_blocks(candles)
    fvgs = find_fair_value_gaps(candles)
    phase = classify_market_phase(candles)
    expected_ob = "bullish_demand" if normalized == "bullish" else "bearish_supply"
    expected_fvg = "bullish" if normalized == "bullish" else "bearish"
    opposite_ob = "bearish_supply" if normalized == "bullish" else "bullish_demand"
    opposite_fvg = "bearish" if normalized == "bullish" else "bullish"

    def nearby(zone: Dict[str, Any]) -> bool:
        return zone["low"] * 0.99 <= price <= zone["high"] * 1.01

    supporting_ob = any(block["type"] == expected_ob and nearby(block) for block in order_blocks)
    supporting_fvg = any(gap["type"] == expected_fvg and nearby(gap) for gap in fvgs)
    opposing_zone = any(block["type"] == opposite_ob and nearby(block) for block in order_blocks) or any(
        gap["type"] == opposite_fvg and nearby(gap) for gap in fvgs
    )
    bos_aligned = bos.get("detected") and bos.get("direction") == normalized
    choch_opposes = choch.get("detected") and choch.get("direction") != normalized
    sweep_direction = "bullish" if normalized == "bullish" else "bearish"
    sweep_aligned = sweep.get("detected") and sweep.get("direction", "").startswith(sweep_direction)
    sweep_opposes = sweep.get("detected") and not sweep_aligned
    phase_aligned = phase in ({"MARKUP", "ACCUMULATION"} if normalized == "bullish" else {"MARKDOWN", "DISTRIBUTION"})

    adjustment = 0
    factors: list[str] = []
    risks: list[str] = []
    if bos_aligned:
        adjustment += 3
        factors.append("Break of structure aligns with the Radar direction.")
    if supporting_ob:
        adjustment += 2
        factors.append("Price is near a same-direction supply/demand order block.")
    if supporting_fvg:
        adjustment += 2
        factors.append("Price is near a same-direction unmitigated fair-value gap.")
    if sweep_aligned:
        adjustment += 3
        factors.append("Recent liquidity sweep/stop-hunt reversal aligns with the setup.")
    if phase_aligned:
        adjustment += 2
        factors.append(f"Operating phase ({phase.lower()}) supports the direction.")
    if opposing_zone:
        adjustment -= 3
        risks.append("An opposing supply/demand zone or fair-value gap is near current price.")
    if choch_opposes:
        adjustment -= 4
        risks.append("A change-of-character opposes the proposed direction.")
    if sweep_opposes:
        adjustment -= 3
        risks.append("The latest liquidity sweep/stop-hunt pattern opposes the proposed direction.")

    return {
        "score_adjustment": max(-8, min(12, adjustment)),
        "phase": phase,
        "bos": bos,
        "choch": choch,
        "liquidity_sweep": sweep,
        "supporting_order_block": supporting_ob,
        "supporting_fvg": supporting_fvg,
        "opposing_zone_nearby": opposing_zone,
        "supporting_factors": factors,
        "risk_flags": risks,
        "limitations": "SMC labels are confluence context, not standalone proof of institutional intent.",
    }


async def _radar_json(
    client: httpx.AsyncClient,
    path: str,
    params: Dict[str, Any],
) -> Any:
    """Fetch one bounded live-confirmation input without failing the full scan."""
    try:
        response = await client.get(f"https://fapi.binance.com{path}", params=params, timeout=5.0)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        logger.warning("Radar live-confirmation fetch failed for %s: %s", path, exc)
        return None


async def _fetch_live_confirmation(client: httpx.AsyncClient, symbol: str) -> Dict[str, Any]:
    """Collect execution and positioning evidence for a *small* set of structures.

    This intentionally runs only after the candle-based structural screen. A
    depth snapshot and derivative positioning are confirmation/veto inputs,
    never a claim that a trade will win.
    """
    depth, premium, oi_history, taker = await asyncio.gather(
        _radar_json(client, "/fapi/v1/depth", {"symbol": symbol, "limit": 20}),
        _radar_json(client, "/fapi/v1/premiumIndex", {"symbol": symbol}),
        _radar_json(client, "/futures/data/openInterestHist", {"symbol": symbol, "period": "5m", "limit": 12}),
        _radar_json(client, "/futures/data/takerlongshortRatio", {"symbol": symbol, "period": "5m", "limit": 12}),
    )

    bids = (depth or {}).get("bids", [])
    asks = (depth or {}).get("asks", [])
    bid_notional = sum(_float(price) * _float(size) for price, size in bids[:20])
    ask_notional = sum(_float(price) * _float(size) for price, size in asks[:20])
    total_depth = bid_notional + ask_notional
    best_bid = _float(bids[0][0]) if bids else 0.0
    best_ask = _float(asks[0][0]) if asks else 0.0
    midpoint = (best_bid + best_ask) / 2 if best_bid and best_ask else 0.0
    spread_bps = ((best_ask - best_bid) / midpoint * 10_000) if midpoint else None

    oi_values = [_float(item.get("sumOpenInterest")) for item in (oi_history or [])]
    oi_change_pct = (
        (oi_values[-1] - oi_values[0]) / oi_values[0] * 100
        if len(oi_values) >= 2 and oi_values[0] > 0
        else None
    )
    taker_latest = taker[-1] if isinstance(taker, list) and taker else {}

    execution_tape = get_execution_tape_snapshot(symbol)
    actual_flow = execution_tape.get("actual_flow") or {}
    tape_flow_ready = publication_flow_is_qualified(execution_tape)
    return {
        "data_complete": bool(
            bids and asks and premium and len(oi_values) >= 2
            and tape_flow_ready
        ),
        "depth_imbalance": round((bid_notional - ask_notional) / total_depth, 4) if total_depth else None,
        "spread_bps": round(spread_bps, 3) if spread_bps is not None else None,
        "current_price": round(midpoint, 12) if midpoint else None,
        "bid_depth_notional": round(bid_notional, 2),
        "ask_depth_notional": round(ask_notional, 2),
        "funding_rate": _float((premium or {}).get("lastFundingRate")),
        "oi_change_pct": round(oi_change_pct, 3) if oi_change_pct is not None else None,
        "taker_buy_sell_ratio": _float(taker_latest.get("buySellRatio"), 1.0) if taker_latest else None,
        "execution_tape": execution_tape,
    }


async def _enrich_live_confirmations(
    client: httpx.AsyncClient,
    candidates: List[Dict[str, Any]],
) -> None:
    """Confirm only the strongest completed structures to stay rate-safe."""
    eligible = [
        candidate for candidate in candidates
        if candidate.get("advanced_confirmation", {}).get("state") == "PENDING"
    ][:_ADVANCED_CONFIRMATION_MAX_CANDIDATES]
    semaphore = asyncio.Semaphore(_ADVANCED_CONFIRMATION_CONCURRENCY)

    async def enrich(candidate: Dict[str, Any]) -> None:
        async with semaphore:
            live = await _fetch_live_confirmation(client, candidate["symbol"])
            await asyncio.to_thread(_refresh_causal_context_from_live, candidate, live)
            if (
                candidate.get("direction") == "NEUTRAL"
                or (candidate.get("market_context") or {}).get("status") != "SETUP_CANDIDATE"
                or not (candidate.get("structure_confirmation") or {}).get("passed")
            ):
                candidate["review_status"] = "WATCH_ONLY"
                candidate["status"] = "CAUSAL_CONTEXT_WAIT"
                candidate.setdefault("risk_flags", []).append(
                    "Live evidence or market-story timing is not actionable; waiting for a fresh causal context."
                )
                return
            _apply_live_confirmation(candidate, live)

    await asyncio.gather(*(enrich(candidate) for candidate in eligible))

def calculate_ema(prices: List[float], period: int) -> List[float]:
    """Calculate Exponential Moving Average (EMA) for a prices series."""
    if len(prices) < period:
        return [0.0] * len(prices)
    
    alpha = 2 / (period + 1)
    ema = []
    # Start with SMA for first element
    sma = sum(prices[:period]) / period
    for _ in range(period - 1):
        ema.append(0.0)
    ema.append(sma)
    
    for price in prices[period:]:
        next_ema = (price * alpha) + (ema[-1] * (1 - alpha))
        ema.append(next_ema)
    return ema

def calculate_rsi(prices: List[float], period: int = 14) -> List[float]:
    """Calculate Relative Strength Index (RSI) for a prices series."""
    if len(prices) < period + 1:
        return [50.0] * len(prices)
        
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    rsi = [50.0] * (period)
    
    # Calculate first average gain and loss
    gains = [d if d > 0 else 0.0 for d in deltas[:period]]
    losses = [-d if d < 0 else 0.0 for d in deltas[:period]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    
    if avg_gain == 0 and avg_loss == 0:
        rsi.append(50.0)
    elif avg_loss == 0:
        rsi.append(100.0)
    else:
        rs = avg_gain / avg_loss
        rsi.append(100 - (100 / (1 + rs)))
        
    for i in range(period, len(deltas)):
        delta = deltas[i]
        gain = delta if delta > 0 else 0.0
        loss = -delta if delta < 0 else 0.0
        
        # Smoothed RS
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        
        if avg_gain == 0 and avg_loss == 0:
            rsi.append(50.0)
        elif avg_loss == 0:
            rsi.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi.append(100 - (100 / (1 + rs)))
            
    return rsi

def calculate_atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> List[float]:
    """Calculate Average True Range (ATR) for a series of candles."""
    if len(closes) < 2:
        return [0.0] * len(closes)
        
    # True Range list
    tr = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        val1 = highs[i] - lows[i]
        val2 = abs(highs[i] - closes[i-1])
        val3 = abs(lows[i] - closes[i-1])
        tr.append(max(val1, val2, val3))
        
    if len(tr) < period:
        return tr
        
    # Simple Moving Average of True Range
    atr = []
    sma = sum(tr[:period]) / period
    for _ in range(period - 1):
        atr.append(0.0)
    atr.append(sma)
    
    for val in tr[period:]:
        next_atr = (atr[-1] * (period - 1) + val) / period
        atr.append(next_atr)
        
    return atr

def calculate_keltner_channels(closes: List[float], highs: List[float], lows: List[float], period: int = 20, multiplier: float = 2.2) -> Tuple[List[float], List[float], List[float]]:
    """
    Calculate Keltner Channels.
    Basis: 20-period EMA
    Bands: Basis +/- (multiplier * ATR(20))
    """
    basis = calculate_ema(closes, period)
    atr = calculate_atr(highs, lows, closes, period)
    
    upper = []
    lower = []
    for b, a in zip(basis, atr):
        if b == 0.0 or a == 0.0:
            upper.append(0.0)
            lower.append(0.0)
        else:
            upper.append(b + (multiplier * a))
            lower.append(b - (multiplier * a))
            
    return basis, upper, lower

async def fetch_klines(client: httpx.AsyncClient, symbol: str, timeframe: str) -> List[List[Any]]:
    """Fetch recent klines for a symbol and interval from Binance Futures."""
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {
        "symbol": symbol,
        "interval": timeframe,
        "limit": _CAUSAL_HISTORY_CANDLES,
    }
    try:
        response = await client.get(url, params=params, timeout=5.0)
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        logger.error(f"Error fetching {timeframe} klines for {symbol}: {e}")
    return []

async def fetch_monitoring_symbols(client: httpx.AsyncClient) -> Set[str]:
    """Retrieve all symbols marked with Binance Spot Monitoring/Delisting Risk Tag."""
    url = "https://api.binance.com/api/v3/exchangeInfo"
    try:
        resp = await client.get(url, timeout=5.0)
        if resp.status_code == 200:
            data = resp.json()
            monitoring_symbols = set()
            for s in data.get("symbols", []):
                # Binance labels monitored tags in the "tags" array
                tags = [t.lower() for t in s.get("tags", [])]
                if "monitoring" in tags or "seed" in tags:
                    monitoring_symbols.add(s["symbol"])
            return monitoring_symbols
    except Exception as e:
        logger.error(f"Failed to fetch Binance Spot tags mapping: {e}")
    return set()


async def fetch_crypto_perpetual_metadata(client: httpx.AsyncClient) -> Dict[str, Dict[str, Any]]:
    """Return Binance Futures metadata used to keep Radar crypto-only."""
    try:
        response = await client.get("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=5.0)
        if response.status_code == 200:
            return {
                item["symbol"]: item
                for item in response.json().get("symbols", [])
                if item.get("symbol")
            }
    except Exception as exc:
        logger.warning("Could not load Futures contract metadata for Radar: %s", exc)
    return {}

async def _legacy_get_breakout_candidates(ltf: str = "5m", htf: str = "1h", use_ai: bool = False) -> List[Dict[str, Any]]:
    """Deterministically triage liquid perpetual contracts for manual review.

    Radar candidates are not trade signals. They are ranked from completed
    candles, liquidity, volatility, and higher-timeframe alignment only.
    """
    raise RuntimeError(
        "Legacy RSI/EMA Radar scoring is retired. Use "
        "get_breakout_candidates(), which enforces the Bare Eye contract."
    )
    if ltf not in _RADAR_INTERVALS or htf not in _RADAR_INTERVALS:
        raise ValueError("Radar supports 5m, 15m, 1h, 4h, and 1d timeframes.")
    if ltf == htf:
        raise ValueError("Radar lower and higher timeframes must be different.")
    if use_ai:
        logger.info("Radar AI scoring is disabled; deterministic evidence ranking is used instead.")
    url_ticker = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    url_book = "https://fapi.binance.com/fapi/v1/ticker/bookTicker"
    
    async with httpx.AsyncClient() as client:
        # Fetch delisting surveillance tag lists, raw tickers, and book tickers in parallel
        try:
            tickers_task = client.get(url_ticker, timeout=5.0)
            book_task = client.get(url_book, timeout=5.0)
            monitoring_task = fetch_monitoring_symbols(client)
            futures_metadata_task = fetch_crypto_perpetual_metadata(client)
            
            resp, resp_book, monitoring_set, futures_metadata = await asyncio.gather(
                tickers_task, book_task, monitoring_task, futures_metadata_task,
            )
            
            if resp.status_code != 200 or resp_book.status_code != 200:
                logger.error("Failed to fetch Binance Futures ticker stats.")
                return []
            tickers = resp.json()
            books = resp_book.json()
        except Exception as e:
            logger.error(f"Error initializing radar scans: {e}")
            return []

        # Map symbol to book ticker details containing bidPrice and askPrice
        books_map = {b["symbol"]: b for b in books if "symbol" in b}

        # Filter active perpetual contracts ending with USDT with liquid volumes and spreads
        usdt_tickers = []
        for t in tickers:
            symbol = t["symbol"]
            if not symbol.endswith("USDT"):
                continue
            
            # Check 1: Exclude tokens flagged in Binance's delisting monitoring surveillance list
            if symbol in monitoring_set:
                logger.warning(f"Excluding symbol under delisting surveillance: {symbol}")
                continue

            # Binance Futures also lists non-crypto instruments. Require an
            # active crypto perpetual when metadata is available; if the
            # metadata call itself failed, retain the liquid-universe fallback
            # instead of declaring no Radar data.
            contract = futures_metadata.get(symbol) if futures_metadata else None
            if contract:
                underlying_type = str(contract.get("underlyingType", "")).upper()
                if (
                    contract.get("contractType") != "PERPETUAL"
                    or contract.get("status") != "TRADING"
                    or (underlying_type and underlying_type != "COIN")
                ):
                    continue
            
            # Retrieve bid and ask prices from books_map (as /fapi/v1/ticker/24hr does not return them)
            book = books_map.get(symbol)
            if not book:
                continue
            
            try:
                vol = float(t.get("quoteVolume", 0))
                bid = float(book.get("bidPrice", 0))
                ask = float(book.get("askPrice", 0))
                
                # Check 2: Enforce minimum 24h volume of $8,000,000 USDT
                if vol < 8000000:
                    continue
                
                # Check 3: Enforce maximum bid-ask spread of 0.15% to screen out illiquid crap coins
                if bid > 0:
                    spread_pct = ((ask - bid) / bid) * 100
                    if spread_pct > 0.15:
                        continue
                else:
                    continue
                
                usdt_tickers.append(t)
            except (ValueError, TypeError):
                continue
                
        usdt_tickers.sort(key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)
        
        # Pick top 40 most active contracts
        top_symbols = [t["symbol"] for t in usdt_tickers[:40]]

        # Bound kline concurrency. The former 80-request burst every refresh
        # was prone to exchange throttling and intermittent empty radars.
        semaphore = asyncio.Semaphore(_KLINE_CONCURRENCY)

        async def bounded_klines(symbol: str, interval: str) -> List[List[Any]]:
            async with semaphore:
                return await fetch_klines(client, symbol, interval)

        tasks_ltf = [bounded_klines(symbol, ltf) for symbol in top_symbols]
        tasks_htf = [bounded_klines(symbol, htf) for symbol in top_symbols]
        
        klines_ltf_list = await asyncio.gather(*tasks_ltf)
        klines_htf_list = await asyncio.gather(*tasks_htf)

        now_ms = int(time.time() * 1000)
        breakouts = []
        
        for symbol, raw_ltf, raw_htf in zip(top_symbols, klines_ltf_list, klines_htf_list):
            klines_ltf = [k for k in raw_ltf if int(k[6]) <= now_ms]
            klines_htf = [k for k in raw_htf if int(k[6]) <= now_ms]
            
            if len(klines_ltf) < 55 or len(klines_htf) < 55:
                continue
                
            try:
                # Extract columns
                closes_ltf = [float(k[4]) for k in klines_ltf]
                opens_ltf = [float(k[1]) for k in klines_ltf]
                highs_ltf = [float(k[2]) for k in klines_ltf]
                lows_ltf = [float(k[3]) for k in klines_ltf]
                volumes_ltf = [float(k[7]) for k in klines_ltf]
                
                # Check target candle: index -1 is the last completed candle
                target_volume = volumes_ltf[-1]
                target_close = closes_ltf[-1]
                target_open = opens_ltf[-1]
                target_high = highs_ltf[-1]
                target_low = lows_ltf[-1]
                
                # Check 4: Spot 'Dead Coin' warnings (programmatic)
                # If ATR is near zero relative to price (e.g. less than 0.05% relative volatility)
                # or if the price is flatlined, exclude it from active breakout signals
                atr_series = calculate_atr(highs_ltf, lows_ltf, closes_ltf, period=14)
                current_atr = atr_series[-1]
                
                relative_volatility_pct = (current_atr / target_close) * 100 if target_close > 0 else 0.0
                if relative_volatility_pct < 0.05:
                    logger.info(f"Skipping flatline Dead Coin setup: {symbol} (ATR ratio: {relative_volatility_pct:.4f}%)")
                    continue

                # Relative Volume (RVol) on LTF
                preceding_volumes = volumes_ltf[-21:-1]
                avg_volume = sum(preceding_volumes) / len(preceding_volumes)
                if avg_volume == 0:
                    continue
                rvol = target_volume / avg_volume
                
                # Calculate indicators
                rsi_series = calculate_rsi(closes_ltf, period=14)
                ema_20 = calculate_ema(closes_ltf, period=20)
                ema_50 = calculate_ema(closes_ltf, period=50)
                
                current_rsi = rsi_series[-1]
                current_ema20 = ema_20[-1]
                current_ema50 = ema_50[-1]
                
                # True range of the target completed LTF candle
                tr_val = max(target_high - target_low, abs(target_high - closes_ltf[-2]), abs(target_low - closes_ltf[-2]))
                atr_ratio = tr_val / current_atr if current_atr > 0 else 0.0
                
                # Score Calculation
                score = 0
                
                # 1. Volume Score (up to 40 points)
                if rvol >= 3.0:
                    score += 40
                elif rvol >= 2.0:
                    score += 30
                elif rvol >= 1.5:
                    score += 20
                elif rvol >= 1.0:
                    score += 10
                    
                # 2. Volatility Breakout Score (up to 30 points)
                if atr_ratio >= 2.0:
                    score += 30
                elif atr_ratio >= 1.5:
                    score += 20
                elif atr_ratio >= 1.0:
                    score += 10
                
                # Fetch Keltner Channels for MTF overextension analysis
                _, upper_kltf, lower_kltf = calculate_keltner_channels(closes_ltf, highs_ltf, lows_ltf, period=20, multiplier=2.2)
                
                closes_htf = [float(k[4]) for k in klines_htf]
                opens_htf = [float(k[1]) for k in klines_htf]
                highs_htf = [float(k[2]) for k in klines_htf]
                lows_htf = [float(k[3]) for k in klines_htf]
                _, upper_khtf, lower_khtf = calculate_keltner_channels(closes_htf, highs_htf, lows_htf, period=20, multiplier=2.2)
                
                # Establish overextended flags
                is_ltf_bull_exhausted = target_close > upper_kltf[-1]
                is_ltf_bear_exhausted = target_close < lower_kltf[-1]
                is_htf_bull_exhausted = target_close > upper_khtf[-1]
                is_htf_bear_exhausted = target_close < lower_khtf[-1]
                
                is_bullish_exhausted = is_ltf_bull_exhausted or is_htf_bull_exhausted
                is_bearish_exhausted = is_ltf_bear_exhausted or is_htf_bear_exhausted
                
                # Analyze Candle Geometry & Rejection Wicks
                body_size = abs(target_close - target_open)
                candle_range = target_high - target_low
                lower_wick = min(target_open, target_close) - target_low
                upper_wick = target_high - max(target_open, target_close)
                is_red_candle = target_close < target_open
                is_green_candle = target_close > target_open

                quality_badge = "🔍 HIGH PROBABILITY"

                # Re-classification Direction Engine with Falling Knife Protection:
                if is_bearish_exhausted:
                    # Check if candle is a pure red dumping candle without lower rejection
                    if is_red_candle and lower_wick <= (body_size * 0.6):
                        trend_direction = "BEARISH"  # Pure red dump -> Bearish Breakdown!
                        setup_status = "📉 BEARISH BREAKDOWN (SHORT)"
                        quality_badge = "⚠️ HIGH RISK DUMP"
                        score = max(15, score - 15)
                    else:
                        trend_direction = "BULLISH"  # Rejection wick or green candle -> Reversal Long!
                        setup_status = "⚠️ SELLING EXHAUSTED (REVERSAL LONG)"
                        quality_badge = "✅ REVERSAL VERIFIED"
                        if current_rsi <= 30:
                            score += 30
                        else:
                            score += 15

                elif is_bullish_exhausted:
                    # Check if candle is a pure green surge candle without upper rejection
                    if is_green_candle and upper_wick <= (body_size * 0.6):
                        trend_direction = "BULLISH"  # Pure green surge -> Bullish Breakout!
                        setup_status = "🚀 BULLISH BREAKOUT (LONG)"
                        quality_badge = "⚠️ HIGH RISK PUMP"
                        if current_rsi >= 65:
                            score += 30
                        else:
                            score += 15
                    else:
                        trend_direction = "BEARISH"  # Upper rejection wick -> Reversal Short!
                        setup_status = "⚠️ BUYING EXHAUSTED (REVERSAL SHORT)"
                        quality_badge = "✅ REVERSAL VERIFIED"
                        if current_rsi >= 70:
                            score += 30
                        else:
                            score += 15

                else:
                    # Healthy, under-extended trend continuations
                    trend_direction = "BULLISH" if target_close >= current_ema20 else "BEARISH"
                    
                    # EMA Trend Check (15 points)
                    if trend_direction == "BULLISH" and current_ema20 > current_ema50:
                        score += 15
                    elif trend_direction == "BEARISH" and current_ema20 < current_ema50:
                        score += 15
                        
                    # RSI Momentum Check (15 points)
                    if trend_direction == "BULLISH" and current_rsi >= 60:
                        score += 15
                    elif trend_direction == "BEARISH" and current_rsi <= 40:
                        score += 15
                        
                    if score >= 75:
                        setup_status = "🚨 EXPLOSIVE BREAKOUT"
                        quality_badge = "🔥 TOP TIER EDGE"
                    elif score >= 50:
                        setup_status = "⚠️ STRONG MOMENTUM"
                        quality_badge = "🔍 HIGH PROBABILITY"
                    elif score >= 30:
                        setup_status = "🔍 WATCHLIST SETUP"
                        quality_badge = "👀 MONITORING"
                    else:
                        setup_status = "➖ CONSOLIDATING"
                        quality_badge = "➖ NEUTRAL"
                        
                # A Radar direction is useful only when the higher timeframe
                # agrees. Previously HTF candles were fetched but not used for
                # directional confirmation.
                htf_ema20 = calculate_ema(closes_htf, period=20)[-1]
                htf_ema50 = calculate_ema(closes_htf, period=50)[-1]
                htf_direction = (
                    "BULLISH" if closes_htf[-1] >= htf_ema20 and htf_ema20 > htf_ema50
                    else "BEARISH" if closes_htf[-1] <= htf_ema20 and htf_ema20 < htf_ema50
                    else "NEUTRAL"
                )
                mtf_aligned = htf_direction == trend_direction
                risk_flags: list[str] = []
                if mtf_aligned:
                    score += 15
                else:
                    score = max(0, score - 20)
                    risk_flags.append(f"Higher timeframe is {htf_direction.lower()}, not aligned with the LTF direction.")
                if rvol < 1.2:
                    risk_flags.append("Relative volume is below 1.2x; move lacks strong participation.")

                review_candidate = (
                    score >= 55
                    and mtf_aligned
                    and rvol >= 1.2
                    and "HIGH RISK" not in quality_badge
                )
                review_status = "REVIEW_CANDIDATE" if review_candidate else "WATCH_ONLY"

                # Structure gate: candle colour or a Keltner touch cannot
                # define a breakout direction. A valid directional label needs
                # EMA structure and slope on both timeframes plus a completed
                # close through the preceding 20-candle swing level.
                ltf_ema20_series = calculate_ema(closes_ltf, period=20)
                ltf_ema50_series = calculate_ema(closes_ltf, period=50)
                htf_ema20_series = calculate_ema(closes_htf, period=20)
                htf_ema50_series = calculate_ema(closes_htf, period=50)
                ltf_ema_slope = ltf_ema20_series[-1] - ltf_ema20_series[-5]
                htf_ema_slope = htf_ema20_series[-1] - htf_ema20_series[-5]
                ltf_direction = (
                    "BULLISH" if target_close > ltf_ema20_series[-1] > ltf_ema50_series[-1] and ltf_ema_slope > 0
                    else "BEARISH" if target_close < ltf_ema20_series[-1] < ltf_ema50_series[-1] and ltf_ema_slope < 0
                    else "NEUTRAL"
                )
                htf_direction = (
                    "BULLISH" if closes_htf[-1] > htf_ema20_series[-1] > htf_ema50_series[-1] and htf_ema_slope > 0
                    else "BEARISH" if closes_htf[-1] < htf_ema20_series[-1] < htf_ema50_series[-1] and htf_ema_slope < 0
                    else "NEUTRAL"
                )
                prior_resistance = max(highs_ltf[-21:-1])
                prior_support = min(lows_ltf[-21:-1])
                broke_resistance = target_close > prior_resistance
                broke_support = target_close < prior_support
                mtf_aligned = ltf_direction != "NEUTRAL" and ltf_direction == htf_direction
                confirmed_bullish_breakout = mtf_aligned and ltf_direction == "BULLISH" and broke_resistance and rvol >= 1.5
                confirmed_bearish_breakdown = mtf_aligned and ltf_direction == "BEARISH" and broke_support and rvol >= 1.5
                confirmed_structure = confirmed_bullish_breakout or confirmed_bearish_breakdown
                smc_confluence = analyze_radar_structure_confluence(
                    _candles_from_klines(klines_ltf), ltf_direction,
                )

                # A close beyond a level with only a wick is not a dependable
                # breakout. It remains visible, but is not eligible for the
                # expensive live-confirmation stage.
                candle_range = max(target_high - target_low, 1e-12)
                body_ratio = abs(target_close - target_open) / candle_range
                close_location = (
                    (target_close - target_low) / candle_range
                    if ltf_direction == "BULLISH"
                    else (target_high - target_close) / candle_range
                )
                candle_quality_pass = body_ratio >= 0.45 and close_location >= 0.65

                risk_flags = []
                if not mtf_aligned:
                    risk_flags.append(f"Timeframe structure conflicts: LTF={ltf_direction.lower()}, HTF={htf_direction.lower()}.")
                if not (broke_resistance or broke_support):
                    risk_flags.append("Price has not closed through the preceding 20-candle support or resistance.")
                if rvol < 1.5:
                    risk_flags.append("Relative volume is below the 1.5x breakout-confirmation threshold.")
                if confirmed_structure and not candle_quality_pass:
                    risk_flags.append("Breakout candle closed with weak body or an adverse rejection wick.")
                risk_flags.extend(smc_confluence["risk_flags"])

                if confirmed_bullish_breakout:
                    trend_direction = "BULLISH"
                    setup_status = "STRUCTURE_CONFIRMED_PENDING_LIVE_CONFIRMATION"
                    quality_badge = "STRUCTURE_CONFIRMED"
                elif confirmed_bearish_breakdown:
                    trend_direction = "BEARISH"
                    setup_status = "STRUCTURE_CONFIRMED_PENDING_LIVE_CONFIRMATION"
                    quality_badge = "STRUCTURE_CONFIRMED"
                elif ltf_direction == "BEARISH":
                    trend_direction = "BEARISH"
                    setup_status = "BEARISH_TREND_WATCH"
                    quality_badge = "STRUCTURE_NOT_CONFIRMED"
                    score = min(score, 49)
                elif ltf_direction == "BULLISH":
                    trend_direction = "BULLISH"
                    setup_status = "BULLISH_TREND_WATCH"
                    quality_badge = "STRUCTURE_NOT_CONFIRMED"
                    score = min(score, 49)
                else:
                    trend_direction = "NEUTRAL"
                    setup_status = "NO_CONFIRMED_DIRECTION"
                    quality_badge = "STRUCTURE_NOT_CONFIRMED"
                    score = min(score, 35)

                # Do not carry the earlier Keltner/candle heuristic points into
                # the final ranking. This is a transparent evidence score:
                # completed structure, participation, and decisive close first;
                # live execution/derivatives points are added only below.
                if confirmed_structure:
                    structure_score = 45
                    structure_score += min(15, int(max(0.0, rvol - 1.5) * 10))
                    if candle_quality_pass:
                        structure_score += 10
                    # SMC factors influence ranking but cannot create a Radar
                    # setup without the objective structure gate above.
                    score = min(75, max(0, structure_score + smc_confluence["score_adjustment"]))
                elif ltf_direction != "NEUTRAL":
                    score = min(49, 20 + min(15, int(max(0.0, rvol - 1.0) * 10)))
                else:
                    score = min(35, 10 + min(10, int(max(0.0, rvol - 1.0) * 8)))

                live_eligible = confirmed_structure and candle_quality_pass
                review_status = "WATCH_ONLY"
                price_change_pct = ((target_close - target_open) / target_open) * 100
                
                breakouts.append({
                    "symbol": symbol,
                    "score": score,
                    "rvol": round(rvol, 2),
                    "atr_ratio": round(atr_ratio, 2),
                    "rsi": round(current_rsi, 1),
                    "price_change_pct": round(price_change_pct, 2),
                    "close_price": target_close,
                    "volume_usdt": round(target_volume, 2),
                    "direction": trend_direction,
                    "status": setup_status,
                    "quality_badge": quality_badge,
                    "htf_direction": htf_direction,
                    "mtf_aligned": mtf_aligned,
                    "review_status": review_status,
                    "risk_flags": risk_flags,
                    "structure": {
                        "prior_resistance": prior_resistance,
                        "prior_support": prior_support,
                        "broke_resistance": broke_resistance,
                        "broke_support": broke_support,
                        "confirmed": confirmed_structure,
                        "body_ratio": round(body_ratio, 3),
                        "close_location": round(close_location, 3),
                        "candle_quality_pass": candle_quality_pass,
                    },
                    "smc_confluence": smc_confluence,
                    "advanced_confirmation": {
                        "state": "PENDING" if live_eligible else "NOT_ELIGIBLE",
                        "reason": (
                            "Completed structure awaits live depth and derivatives confirmation."
                            if live_eligible
                            else "Requires confirmed structure with a decisive completed breakout candle."
                        ),
                    },
                    "evaluation_mode": "structure_then_live_execution_evidence",
                })
            except Exception as e:
                logger.error(f"Error analyzing metrics for {symbol}: {e}")
                continue

        # Sort before enrichment so only the strongest completed structures use
        # the limited live market-data budget.
        breakouts.sort(key=lambda x: (x["score"], x["rvol"]), reverse=True)

        await _enrich_live_confirmations(client, breakouts)
        breakouts.sort(key=lambda x: (x["review_status"] == "REVIEW_CANDIDATE", x["score"], x["rvol"]), reverse=True)

        # ── AI Scoring Layer ─────────────────────────────────────────────
        # Language-model scoring is intentionally not part of Radar ranking.
        # The model previously received only summary numbers, so labels such as
        # "AI VERIFIED" or "trap" were not evidence-backed.

        return breakouts


def _phase_bias(phase: str) -> str:
    if phase in {"MARKUP", "ACCUMULATION"}:
        return "BULLISH"
    if phase in {"MARKDOWN", "DISTRIBUTION"}:
        return "BEARISH"
    return "NEUTRAL"


def _candle_range_reference(candles: list[Candle]) -> float:
    """A sizing reference for liquidity clustering, never a trade signal."""
    rows = candles[-14:]
    return sum(max(0.0, candle.high - candle.low) for candle in rows) / len(rows) if rows else 0.0


def _observable_structure_events(candles: list[Candle]) -> dict[str, dict[str, Any]]:
    """Compatibility projection from the canonical completed-candle story."""
    return observable_structure_events(build_market_story(candles))


def _candidate_from_causal_context(
    *, symbol: str, candles: list[Candle], higher_candles: list[Candle], quote_volume_24h: float,
) -> dict[str, Any] | None:
    """Build a Radar discovery row without derived-indicator directional rules."""
    if len(candles) < 55 or len(higher_candles) < 55:
        return None

    phase = classify_market_phase(candles)
    higher_phase = classify_market_phase(higher_candles)
    higher_bias = _phase_bias(higher_phase)
    story = build_market_story(candles)
    higher_story = build_market_story(higher_candles)
    sweep = observable_liquidity_sweep(story)
    events = observable_structure_events(story)
    structure = {
        "phase": phase,
        "higher_timeframe_phase": higher_phase,
        "bos": events["bos"],
        "choch": events["choch"],
        "liquidity_sweep": sweep,
        "story_state": story.get("current_state"),
        "story_actionability": story.get("actionability", {}),
        "story_as_of_close_time": story.get("as_of_close_time"),
    }
    liquidity_map = build_liquidity_map(candles, _candle_range_reference(candles))
    features: dict[str, Any] = {
        "market_structure": structure,
        "market_story": story,
        "liquidity_map": liquidity_map,
        "sweep": sweep,
        "positioning": {"available": False, "state": "UNKNOWN"},
        # No depth history has been captured at this stage.  A candle's
        # taker volume is not mislabeled as an order-book imbalance.
        "microstructure": {"available": False},
        "trade_flow": {"available": False, "buy_ratio": None, "bias": "UNAVAILABLE"},
        "volatility_context": build_volatility_context(candles),
        "volume_profile": build_volume_profile(candles),
        "vwap_context": build_vwap_context(candles),
    }
    context = score_market_context(features)
    direction = {"LONG": "BULLISH", "SHORT": "BEARISH"}.get(context["direction"], "NEUTRAL")
    playbook_evaluation = evaluate_story_playbook(
        primary_story=story,
        higher_story=higher_story,
        direction=direction,
        primary_phase=phase,
        higher_phase=higher_phase,
        vwap_context=features["vwap_context"],
        volume_profile=features["volume_profile"],
    )
    story_view = playbook_evaluation["directional_view"]
    structure_ready = bool(playbook_evaluation["passed"])
    structure_playbook = playbook_evaluation["playbook"]
    event_quality_ready = bool(
        playbook_evaluation["checks"]["structure_event_quality_ready"]
    )
    risk_flags = list(context.get("contradictions", []))
    if direction != "NEUTRAL" and higher_bias not in {"NEUTRAL", direction}:
        risk_flags.append("higher_timeframe_regime_conflicts")
    if direction != "NEUTRAL" and not structure_ready:
        # An actionable/retesting event can still fail the *playbook* because
        # regime, range acceptance, or higher-timeframe structure is missing.
        # Calling that event itself a contradiction produced labels such as
        # "market story actionable now", which inverted the actual meaning.
        if not story_view.get("actionable"):
            risk_flags.append(
                f"market_story_{story_view.get('state', 'not_actionable').lower()}"
            )
        else:
            reason_code = str(
                playbook_evaluation.get("reason_code", "playbook_not_confirmed")
            ).lower()
            risk_flags.append(f"structure_playbook_{reason_code}")
    risk_flags = list(dict.fromkeys(risk_flags))

    completed = candles[-1]
    reference = candles[-6].close
    price_change_pct = ((completed.close - reference) / reference * 100.0) if reference else 0.0
    target_pool = liquidity_map.get("nearest_above" if direction == "BULLISH" else "nearest_below") if direction != "NEUTRAL" else None
    coverage = context.get("coverage", {})
    eligible = (
        direction != "NEUTRAL"
        and context.get("status") == "SETUP_CANDIDATE"
        and coverage.get("complete", False)
        and structure_ready
        and not risk_flags
    )
    components = context.get("components", {})
    evidence_tags = [
        name.replace("_", " ")
        for name, component in components.items()
        if component.get("available") and component.get("bias") in {direction, "NEUTRAL"}
    ]
    return {
        "symbol": symbol,
        "score": int(round(context.get("score", 0))),
        "direction": direction,
        "status": "CAUSAL_CONTEXT_PENDING_LIVE_CONFIRMATION" if eligible else "WAIT_FOR_ALIGNED_EVIDENCE",
        "quality_badge": "CAUSAL_CONTEXT" if eligible else "EVIDENCE_INCOMPLETE",
        "review_status": "WATCH_ONLY",
        "risk_flags": risk_flags,
        "contradictions": risk_flags,
        "market_context": context,
        "liquidity_map": liquidity_map,
        "positioning": features["positioning"],
        "volatility_context": features["volatility_context"],
        "volume_profile": features["volume_profile"],
        "vwap_context": features["vwap_context"],
        "market_structure": structure,
        "market_story": story,
        "higher_timeframe_story": higher_story,
        "structure_confirmation": {
            "passed": structure_ready,
            "playbook": structure_playbook,
            "story_state": story_view.get("state"),
            "actionable": bool(story_view.get("actionable")),
            "reason": playbook_evaluation.get("reason"),
            "event_quality_ready": event_quality_ready,
            "higher_timeframe_aligned": higher_bias == direction,
            "selected_event": playbook_evaluation.get("selected_event"),
            "checks": playbook_evaluation.get("checks", {}),
        },
        "target_pool": target_pool,
        "evidence_tags": evidence_tags,
        "coverage": coverage,
        "close_price": completed.close,
        "price_change_pct": round(price_change_pct, 2),
        "volume_usdt": round(quote_volume_24h, 2),
        # Compatibility telemetry only. These values are never scored or
        # rendered by the causal Radar surface.
        "rvol": None,
        "atr_ratio": None,
        "rsi": None,
        "htf_direction": higher_bias,
        "mtf_aligned": higher_bias in {"NEUTRAL", direction},
        "advanced_confirmation": {
            "state": "PENDING" if eligible else "NOT_ELIGIBLE",
            "reason": "Causal context awaits live depth, price×OI, funding and taker-flow confirmation." if eligible else "Waiting for aligned regime, liquidity and causal evidence.",
        },
        "causal_radar": True,
        "_causal_features": features,
        "_candles": candles,
        "evaluation_mode": "causal_market_discovery_then_live_confirmation",
    }


def _refresh_causal_context_from_live(candidate: dict[str, Any], live: dict[str, Any]) -> None:
    """Fold the bounded live snapshot into an already-ranked Radar context."""
    features = candidate.get("_causal_features")
    candles = candidate.get("_candles")
    if not isinstance(features, dict) or not isinstance(candles, list):
        return
    execution_tape = live.get("execution_tape")
    if isinstance(execution_tape, dict):
        # The live-confirmation fetch already reads the shared four-feed tape.
        # Attach that exact snapshot before rescoring so Radar ranking, its
        # evidence chips, and the final confirmation badge cannot disagree.
        features["execution_tape"] = execution_tape
        features["multi_venue"] = execution_tape
    ratio_raw = live.get("taker_buy_sell_ratio")
    ratio = _float(ratio_raw) if ratio_raw is not None else None
    ratio_value = ratio if ratio is not None else 1.0
    buy_ratio = ratio_value / (1.0 + ratio_value) if ratio is not None and ratio_value > 0 else None
    price_change = _float(candidate.get("price_change_pct"))
    derivatives = {
        "funding_rate": live.get("funding_rate"),
        "oi_history": {
            "available": live.get("oi_change_pct") is not None,
            "oi_change_pct": live.get("oi_change_pct"),
        },
        "taker_volume": {
            "cvd_trend": "UNAVAILABLE" if ratio is None else "CVD_BULLISH" if ratio >= 1.02 else "CVD_BEARISH" if ratio <= 0.98 else "CVD_NEUTRAL",
            "aggression": "UNAVAILABLE" if ratio is None else "BUYER_AGGRESSIVE" if ratio >= 1.02 else "SELLER_AGGRESSIVE" if ratio <= 0.98 else "NEUTRAL",
        },
    }
    features["positioning"] = classify_positioning(
        candles,
        derivatives,
        execution_tape=execution_tape if isinstance(execution_tape, dict) else None,
    )
    features["microstructure"] = {
        "available": bool(live.get("data_complete")),
        "depth_imbalance": live.get("depth_imbalance"),
        "spread_bps": live.get("spread_bps"),
    }
    features["trade_flow"] = {
        "available": buy_ratio is not None,
        "buy_ratio": buy_ratio,
        "bias": "UNAVAILABLE" if buy_ratio is None else "BUYING" if buy_ratio > 0.55 else "SELLING" if buy_ratio < 0.45 else "NEUTRAL",
    }
    context = score_market_context(features)
    direction = {"LONG": "BULLISH", "SHORT": "BEARISH"}.get(context["direction"], "NEUTRAL")
    higher_bias = candidate.get("htf_direction", "NEUTRAL")
    contradictions = list(context.get("contradictions", []))
    if direction != "NEUTRAL" and higher_bias not in {"NEUTRAL", direction}:
        contradictions.append("higher_timeframe_regime_conflicts")
    structure = features.get("market_structure") or {}
    playbook_evaluation = evaluate_story_playbook(
        primary_story=features.get("market_story") or {},
        higher_story=candidate.get("higher_timeframe_story") or {},
        direction=direction,
        primary_phase=str(structure.get("phase", "RANGING")),
        higher_phase=str(structure.get("higher_timeframe_phase", "UNAVAILABLE")),
        vwap_context=features.get("vwap_context") or {},
        volume_profile=features.get("volume_profile") or {},
    )
    if direction != "NEUTRAL" and not playbook_evaluation["passed"]:
        story_view = playbook_evaluation.get("directional_view") or {}
        if not story_view.get("actionable"):
            contradictions.append(
                f"market_story_{str(story_view.get('state', 'not_actionable')).lower()}"
            )
        else:
            reason_code = str(
                playbook_evaluation.get("reason_code", "playbook_not_confirmed")
            ).lower()
            contradictions.append(f"structure_playbook_{reason_code}")
    contradictions = list(dict.fromkeys(contradictions))
    directional_view = playbook_evaluation.get("directional_view") or {}
    structure_confirmation = {
        "passed": bool(playbook_evaluation["passed"]),
        "playbook": playbook_evaluation["playbook"],
        "story_state": directional_view.get("state"),
        "actionable": bool(directional_view.get("actionable")),
        "reason": playbook_evaluation.get("reason"),
        "reason_code": playbook_evaluation.get("reason_code"),
        "event_quality_ready": bool(
            (playbook_evaluation.get("checks") or {}).get("structure_event_quality_ready")
        ),
        "higher_timeframe_aligned": higher_bias == direction,
        "selected_event": playbook_evaluation.get("selected_event"),
        "checks": playbook_evaluation.get("checks", {}),
    }
    causal_ready = (
        direction != "NEUTRAL"
        and context.get("status") == "SETUP_CANDIDATE"
        and structure_confirmation["passed"]
        and not contradictions
    )
    candidate.update({
        "score": int(round(context.get("score", 0))),
        "direction": direction,
        "market_context": context,
        "positioning": features["positioning"],
        "contradictions": contradictions,
        # Rebuild all direction-dependent flags. Carrying the initial flags
        # forward could leave a bullish playbook attached after live flow
        # changed the candidate to bearish.
        "risk_flags": contradictions,
        "structure_confirmation": structure_confirmation,
        "status": (
            "CAUSAL_CONTEXT_PENDING_LIVE_CONFIRMATION"
            if causal_ready
            else "WAIT_FOR_ALIGNED_EVIDENCE"
        ),
        "quality_badge": "CAUSAL_CONTEXT" if causal_ready else "EVIDENCE_INCOMPLETE",
        "coverage": context.get("coverage", {}),
        "evidence_tags": [
            name.replace("_", " ")
            for name, component in context.get("components", {}).items()
            if component.get("available") and component.get("bias") in {direction, "NEUTRAL"}
        ],
    })
    live["price_change_pct"] = price_change


async def get_breakout_candidates(ltf: str = "5m", htf: str = "1h", use_ai: bool = False) -> List[Dict[str, Any]]:
    """Rank liquid perpetuals for causal manual review, never trade execution."""
    if ltf not in _RADAR_INTERVALS or htf not in _RADAR_INTERVALS or ltf == htf:
        raise ValueError("Radar requires two different supported timeframes: 5m, 15m, 1h, 4h, or 1d.")
    if use_ai:
        logger.info("Radar AI scoring is disabled; causal evidence ranking is used instead.")

    async with httpx.AsyncClient() as client:
        try:
            ticker_response, book_response, monitoring_set, futures_metadata = await asyncio.gather(
                client.get("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=5.0),
                client.get("https://fapi.binance.com/fapi/v1/ticker/bookTicker", timeout=5.0),
                fetch_monitoring_symbols(client),
                fetch_crypto_perpetual_metadata(client),
            )
            if ticker_response.status_code != 200 or book_response.status_code != 200:
                return []
        except Exception as exc:
            logger.warning("Radar universe fetch failed: %s", exc)
            return []

        books = {row.get("symbol"): row for row in book_response.json() if row.get("symbol")}
        universe: list[tuple[str, float]] = []
        for ticker in ticker_response.json():
            symbol = str(ticker.get("symbol", ""))
            contract = futures_metadata.get(symbol) if futures_metadata else None
            book = books.get(symbol, {})
            try:
                volume = _float(ticker.get("quoteVolume"))
                bid, ask = _float(book.get("bidPrice")), _float(book.get("askPrice"))
                spread_pct = ((ask - bid) / bid * 100.0) if bid else 999.0
            except (TypeError, ValueError):
                continue
            if (
                not symbol.endswith("USDT") or symbol in monitoring_set or volume < 8_000_000 or spread_pct > 0.15
                or (contract and (contract.get("contractType") != "PERPETUAL" or contract.get("status") != "TRADING"))
            ):
                continue
            universe.append((symbol, volume))

        universe.sort(key=lambda item: item[1], reverse=True)
        universe = universe[:40]
        semaphore = asyncio.Semaphore(_KLINE_CONCURRENCY)

        async def bounded(symbol: str, interval: str) -> list[list[Any]]:
            async with semaphore:
                return await fetch_klines(client, symbol, interval)

        ltf_rows, htf_rows = await asyncio.gather(
            asyncio.gather(*(bounded(symbol, ltf) for symbol, _ in universe)),
            asyncio.gather(*(bounded(symbol, htf) for symbol, _ in universe)),
        )
        now_ms = int(time.time() * 1000)

        def build_candidates() -> list[dict[str, Any]]:
            built: list[dict[str, Any]] = []
            for (symbol, volume), raw_ltf, raw_htf in zip(universe, ltf_rows, htf_rows):
                candles = [
                    candle for candle in _candles_from_klines(raw_ltf)
                    if candle.close_time <= now_ms
                ]
                higher = [
                    candle for candle in _candles_from_klines(raw_htf)
                    if candle.close_time <= now_ms
                ]
                candidate = _candidate_from_causal_context(
                    symbol=symbol,
                    candles=candles,
                    higher_candles=higher,
                    quote_volume_24h=volume,
                )
                if candidate:
                    built.append(candidate)
            return built

        # Market-story reconstruction across the liquid universe is CPU work.
        # Keep it off the sole ASGI event loop so Docker readiness and public
        # Radar reads remain responsive while a shared refresh is calculated.
        candidates = await asyncio.to_thread(build_candidates)

        candidates.sort(key=lambda item: (item["score"], item["coverage"].get("available_domains", 0), item["volume_usdt"]), reverse=True)
        await _enrich_live_confirmations(client, candidates)
        for candidate in candidates:
            candidate.pop("_causal_features", None)
            candidate.pop("_candles", None)
        candidates.sort(key=lambda item: (item["review_status"] == "REVIEW_CANDIDATE", item["score"], item["coverage"].get("available_domains", 0)), reverse=True)
        return candidates


async def _ai_score_candidates(
    candidates: List[Dict[str, Any]],
    max_ai_evals: int = 3,
) -> List[Dict[str, Any]]:
    """Run AI analysis on top breakout candidates to evaluate setup quality.

    Only candidates with a deterministic score >= 40 are sent to AI to avoid
    wasting API calls on low-quality setups. The final blended score combines
    deterministic analysis (40%) with AI conviction (60%).
    """
    raise RuntimeError(
        "Legacy blended Radar AI scoring is retired. Radar decisions use the "
        "auditable Bare Eye evidence contract."
    )
    from app.settings import get_settings
    from app.ai_client import build_async_ai_client, get_model_for_task, safe_async_chat_completion
    from app.brains.prompts.loader import load_prompt
    from app.utils.json_helper import loads_repaired

    settings = get_settings()
    client = build_async_ai_client(settings)
    if not client:
        logger.warning("AI client not configured — skipping AI scoring for radar.")
        for c in candidates:
            c["ai_score"] = None
            c["ai_verdict"] = "AI_UNAVAILABLE"
            c["ai_reasoning"] = "AI client not configured."
        return candidates

    model = get_model_for_task(settings, "scanner")
    system_prompt = load_prompt("radar_analyst")

    # Select top candidates worth AI evaluation (top max_ai_evals)
    ai_worthy = [c for c in candidates if c["score"] >= 20][:max_ai_evals]
    if not ai_worthy and candidates:
        ai_worthy = candidates[:max_ai_evals]
    ai_symbols = {c["symbol"] for c in ai_worthy}

    sem = asyncio.Semaphore(2)

    async def evaluate_single(candidate: Dict[str, Any], idx: int) -> Dict[str, Any]:
        """Call the AI radar analyst for a single candidate with concurrency lock."""
        async with sem:
            user_prompt = (
                f"Symbol: {candidate['symbol']}\n"
                f"Direction: {candidate['direction']}\n"
                f"Deterministic Score: {candidate['score']}/100\n"
                f"Setup Status: {candidate['status']}\n"
                f"Close Price: {candidate['close_price']}\n"
                f"RSI: {candidate['rsi']}\n"
                f"Relative Volume (RVol): {candidate['rvol']}x\n"
                f"ATR Ratio (candle range vs ATR): {candidate['atr_ratio']}x\n"
                f"Price Change %: {candidate['price_change_pct']}%\n"
                f"24h Volume USDT: ${candidate['volume_usdt']:,.0f}\n"
                f"Quality Badge: {candidate['quality_badge']}\n"
            )

            try:
                # Stagger requests to avoid 429 rate limit spikes on free-tier providers
                await asyncio.sleep(0.5 * idx)

                completion = await safe_async_chat_completion(
                    client=client,
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.15,
                )
                content = completion.choices[0].message.content or "{}"
                result = loads_repaired(content)

                ai_score = int(result.get("ai_score", 50))
                ai_score = max(0, min(100, ai_score))

                candidate["ai_score"] = ai_score
                candidate["ai_verdict"] = result.get("ai_verdict", "CAUTION")
                candidate["ai_reasoning"] = result.get("ai_reasoning", "")
                candidate["trap_risk"] = result.get("trap_risk", "MEDIUM")
                candidate["key_risk"] = result.get("key_risk", "")
                candidate["direction_confirmed"] = result.get("direction_confirmed", False)

                # Blended final score: 40% deterministic + 60% AI
                candidate["raw_score"] = candidate["score"]
                candidate["score"] = round(candidate["raw_score"] * 0.4 + ai_score * 0.6)

                # Explicit Quality Badge assignment for AI evaluated candidates
                if candidate["ai_verdict"] in {"LIKELY_TRAP", "AVOID"} or ai_score < 45:
                    candidate["quality_badge"] = "🚫 AI REJECTED"
                elif ai_score >= 70 or (candidate["score"] >= 75 and ai_score >= 60):
                    candidate["quality_badge"] = "🤖 AI VERIFIED"
                elif ai_score >= 50:
                    candidate["quality_badge"] = "🤖 AI CONFIRMED"
                else:
                    candidate["quality_badge"] = f"🤖 AI SCORED ({ai_score}/100)"

                logger.info(
                    f"AI Radar: {candidate['symbol']} — "
                    f"det_score={candidate['raw_score']}, ai_score={ai_score}, "
                    f"blended={candidate['score']}, verdict={candidate['ai_verdict']}"
                )

            except Exception as exc:
                is_quota = "free-models-per-day" in str(exc) or "quota exceeded" in str(exc).lower()
                if is_quota:
                    logger.error(f"AI Radar quota exhausted: {exc}")
                else:
                    logger.error(f"AI Radar scoring failed for {candidate['symbol']}: {exc}")
                candidate["ai_score"] = None
                candidate["ai_verdict"] = "AI_ERROR"
                candidate["ai_reasoning"] = str(exc)[:200]

            return candidate

    # Run AI evaluations concurrently with concurrency limit of 2
    ai_tasks = [evaluate_single(c, i) for i, c in enumerate(ai_worthy)]
    await asyncio.gather(*ai_tasks, return_exceptions=True)

    # Mark non-AI-evaluated candidates
    for c in candidates:
        if c["symbol"] not in ai_symbols:
            c["ai_score"] = None
            c["ai_verdict"] = "NOT_EVALUATED"
            c["ai_reasoning"] = "Score below AI evaluation threshold."

    # Re-sort by blended score
    candidates.sort(key=lambda x: (x["score"], x["rvol"]), reverse=True)
    return candidates
