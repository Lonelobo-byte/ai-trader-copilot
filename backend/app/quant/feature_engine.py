"""Unified Quant Feature Engine — transforms raw market data into AI-readable features.

This module is the bridge between raw data (candles, order book, derivatives)
and the AI Agent Council.  It computes every quantitative feature that
institutional traders use, packaged into a single structured dict.

The existing ``app.indicators.*`` modules are reused internally — this engine
calls them and normalizes their output into a consistent feature set.

Features are organized into domains:
  • trend       — EMA crosses, ADX, slope metrics
  • momentum    — RSI, StochRSI, MACD, Williams %R, CCI, ROC
  • volatility  — ATR, Bollinger bandwidth, Parkinson vol, squeeze detection
  • volume      — OBV trend, VWAP deviation, volume profile, cumulative delta
  • microstructure — bid-ask imbalance, depth, spread, absorption
  • statistical — Hurst exponent, autocorrelation, skew/kurtosis, z-scores
  • regime      — HMM-style regime classification
  • derivatives — funding, OI change, long/short ratio, liquidation zones
  • cross_asset — macro correlations (DXY, NQ, Gold, Yields)
"""
from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np

from app.data_sources.binance_public import Candle, completed_candles
from app.indicators.trend import analyze_trend, detect_market_regime, calculate_alignment_score
from app.indicators.momentum import analyze_momentum
from app.indicators.volatility import atr as compute_atr
from app.indicators.volume import obv as compute_obv, cmf as compute_cmf, mfi as compute_mfi, vwap as compute_vwap
from app.indicators.liquidity import analyze_liquidity, analyze_order_book, detect_liquidity_sweep
from app.indicators.structure import classify_market_phase, detect_bos, detect_choch, find_fair_value_gaps, find_order_blocks
from app.indicators.funding import analyze_funding_oi_divergence
from app.indicators.liquidation_heatmap import calculate_liquidation_heatmap
from app.quant.statistics import build_statistical_features
from app.quant.microstructure import analyze_microstructure
from app.quant.regimes import classify_market_state

logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _ema(values: list[float], period: int) -> list[float]:
    """Compute EMA series."""
    if len(values) < period:
        return values[:]
    k = 2.0 / (period + 1)
    ema = [sum(values[:period]) / period]
    for v in values[period:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return ema


def _rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 50.0 if avg_gain == 0 else 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _stoch_rsi(closes: list[float], rsi_period: int = 14, stoch_period: int = 14) -> dict[str, float]:
    """Stochastic RSI."""
    if len(closes) < rsi_period + stoch_period:
        return {"k": 50.0, "d": 50.0}
    # Compute RSI series.  %D must be a moving average of %K values, not of
    # raw RSI values (the previous implementation mixed the two scales).
    rsi_vals = [_rsi(closes[:i + 1], rsi_period) for i in range(rsi_period, len(closes))]
    if len(rsi_vals) < stoch_period:
        return {"k": 50.0, "d": 50.0}
    raw_k: list[float] = []
    for i in range(stoch_period, len(rsi_vals) + 1):
        window = rsi_vals[i - stoch_period:i]
        low_rsi, high_rsi = min(window), max(window)
        raw_k.append((rsi_vals[i - 1] - low_rsi) / (high_rsi - low_rsi) * 100.0
                     if high_rsi > low_rsi else 50.0)
    k_series = [sum(raw_k[max(0, i - 2):i + 1]) / len(raw_k[max(0, i - 2):i + 1])
                for i in range(len(raw_k))]
    d_series = [sum(k_series[max(0, i - 2):i + 1]) / len(k_series[max(0, i - 2):i + 1])
                for i in range(len(k_series))]
    return {"k": round(k_series[-1], 2), "d": round(d_series[-1], 2)}


def _macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict[str, float]:
    if len(closes) < slow + signal:
        return {"macd_line": 0.0, "signal_line": 0.0, "histogram": 0.0}
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    # Align lengths
    offset = len(ema_fast) - len(ema_slow)
    macd_line = [f - s for f, s in zip(ema_fast[offset:], ema_slow)]
    if len(macd_line) < signal:
        return {"macd_line": macd_line[-1] if macd_line else 0.0, "signal_line": 0.0, "histogram": 0.0}
    signal_line = _ema(macd_line, signal)
    offset2 = len(macd_line) - len(signal_line)
    hist = macd_line[-1] - signal_line[-1] if signal_line else 0.0
    return {
        "macd_line": round(macd_line[-1], 6),
        "signal_line": round(signal_line[-1], 6),
        "histogram": round(hist, 6),
    }


def _williams_r(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < period:
        return -50.0
    recent = candles[-period:]
    high = max(c.high for c in recent)
    low = min(c.low for c in recent)
    close = candles[-1].close
    if high == low:
        return -50.0
    return round((high - close) / (high - low) * -100.0, 2)


def _cci(candles: list[Candle], period: int = 20) -> float:
    if len(candles) < period:
        return 0.0
    recent = candles[-period:]
    tp_vals = [(c.high + c.low + c.close) / 3.0 for c in recent]
    mean_tp = sum(tp_vals) / len(tp_vals)
    mean_dev = sum(abs(tp - mean_tp) for tp in tp_vals) / len(tp_vals)
    if mean_dev == 0:
        return 0.0
    return round((tp_vals[-1] - mean_tp) / (0.015 * mean_dev), 2)


def _roc(closes: list[float], period: int = 10) -> float:
    if len(closes) < period + 1:
        return 0.0
    prev = closes[-period - 1]
    if prev == 0:
        return 0.0
    return round((closes[-1] - prev) / prev * 100.0, 4)


def _bollinger(closes: list[float], period: int = 20, std_dev: float = 2.0) -> dict[str, float]:
    if len(closes) < period:
        return {"upper": 0, "middle": 0, "lower": 0, "bandwidth": 0, "percent_b": 50.0}
    recent = closes[-period:]
    mid = sum(recent) / len(recent)
    std = (sum((x - mid) ** 2 for x in recent) / (len(recent) - 1)) ** 0.5 if len(recent) > 1 else 0.0
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    bw = ((upper - lower) / mid * 100.0) if mid > 0 else 0.0
    pct_b = ((closes[-1] - lower) / (upper - lower) * 100.0) if (upper - lower) > 0 else 50.0
    return {
        "upper": round(upper, 4),
        "middle": round(mid, 4),
        "lower": round(lower, 4),
        "bandwidth": round(bw, 4),
        "percent_b": round(pct_b, 2),
    }


def _atr_percentile(closed: list[Candle], current_atr: float, lookback: int = 90) -> float:
    """Compute where current ATR sits as a percentile of recent ATR history.

    Returns 0-100 where 100 means current volatility is the highest in the lookback window.
    This tells the AI whether current volatility is historically high or low.
    """
    if len(closed) < 20 or current_atr <= 0:
        return 50.0
    # Compute ATR for each rolling window of 14 candles
    atr_period = 14
    atr_values = []
    window = closed[-lookback:] if len(closed) >= lookback else closed
    for i in range(atr_period, len(window)):
        trs = []
        for j in range(i - atr_period + 1, i + 1):
            tr = max(
                window[j].high - window[j].low,
                abs(window[j].high - window[j - 1].close) if j > 0 else 0,
                abs(window[j].low - window[j - 1].close) if j > 0 else 0,
            )
            trs.append(tr)
        if trs:
            atr_values.append(sum(trs) / len(trs))
    if not atr_values:
        return 50.0
    # Count how many historical ATR values are below the current ATR
    below = sum(1 for a in atr_values if a < current_atr)
    return (below / len(atr_values)) * 100.0


def _summarize_htf_candles(candles: list[Candle]) -> dict[str, Any]:
    """Summarize higher timeframe candle structures for AI prompt injection."""
    if not candles or len(candles) < 3:
        return {"available": False}
    recent = candles[-10:]
    last = recent[-1]
    prev = recent[-2]
    highs = [float(c.high) for c in recent]
    lows = [float(c.low) for c in recent]
    closes = [float(c.close) for c in recent]

    last_body_pct = (abs(float(last.close) - float(last.open)) / float(last.open) * 100.0) if float(last.open) > 0 else 0.0
    last_direction = "BULLISH" if float(last.close) >= float(last.open) else "BEARISH"
    prev_direction = "BULLISH" if float(prev.close) >= float(prev.open) else "BEARISH"

    return {
        "available": True,
        "latest_close": float(last.close),
        "last_candle_direction": last_direction,
        "last_candle_body_pct": round(last_body_pct, 2),
        "prev_candle_direction": prev_direction,
        "swing_high_10bar": max(highs),
        "swing_low_10bar": min(lows),
        "recent_closes": [round(c, 4) for c in closes[-5:]],
    }


def _obv_trend(candles: list[Candle]) -> str:
    if len(candles) < 10:
        return "NEUTRAL"
    obv = 0.0
    obv_series = []
    for i, c in enumerate(candles):
        if i == 0:
            obv_series.append(0.0)
            continue
        if c.close > candles[i - 1].close:
            obv += c.volume
        elif c.close < candles[i - 1].close:
            obv -= c.volume
        obv_series.append(obv)

    recent = obv_series[-10:]
    change = recent[-1] - recent[0]
    scale = sum(c.volume for c in candles[-10:]) / 10.0
    if change > scale * 0.10:
        return "RISING"
    elif change < -scale * 0.10:
        return "FALLING"
    return "FLAT"


def _cumulative_delta(candles: list[Candle], lookback: int = 20) -> dict[str, float]:
    recent = candles[-lookback:] if len(candles) >= lookback else candles
    total_buy = sum(c.taker_buy_base_volume for c in recent)
    total_sell = sum(c.taker_sell_base_volume for c in recent)
    delta = total_buy - total_sell
    total = total_buy + total_sell
    ratio = (total_buy / total) if total > 0 else 0.5
    return {
        "delta": round(delta, 4),
        "buy_volume": round(total_buy, 4),
        "sell_volume": round(total_sell, 4),
        "buy_ratio": round(ratio, 4),
        "bias": "BUYING" if ratio > 0.55 else ("SELLING" if ratio < 0.45 else "NEUTRAL"),
    }


def _vwap_deviation(candles: list[Candle]) -> dict[str, float]:
    """VWAP deviation from current price."""
    if not candles:
        return {"vwap": 0.0, "deviation_pct": 0.0}
    total_tp_vol = 0.0
    total_vol = 0.0
    for c in candles:
        tp = (c.high + c.low + c.close) / 3.0
        total_tp_vol += tp * c.volume
        total_vol += c.volume
    vwap = total_tp_vol / total_vol if total_vol > 0 else candles[-1].close
    dev_pct = ((candles[-1].close - vwap) / vwap * 100.0) if vwap > 0 else 0.0
    return {"vwap": round(vwap, 4), "deviation_pct": round(dev_pct, 4)}


# ── Main engine ──────────────────────────────────────────────────────────────


def compute_quant_features(
    intelligence: dict[str, Any],
) -> dict[str, Any]:
    """Transform raw MarketIntelligence into structured quant features.

    Parameters
    ----------
    intelligence:
        The output of ``data_aggregator.fetch_market_intelligence()``.

    Returns
    -------
    dict
        Structured feature dict with domains: trend, momentum, volatility,
        volume, microstructure, statistical, regime, derivatives, cross_asset,
        liquidity, sweep, confidence_inputs.
    """
    candles: list[Candle] = intelligence.get("candles", [])
    ticker: dict[str, Any] = intelligence.get("ticker", {})
    order_book: dict[str, Any] = intelligence.get("order_book", {"bids": [], "asks": []})
    multi_tf: dict[str, list[Candle]] = intelligence.get("multi_tf_candles", {})
    funding_data = intelligence.get("funding", {})
    oi_data = intelligence.get("open_interest", {})
    derivatives = intelligence.get("derivatives", {})
    recent_trades = intelligence.get("recent_trades", [])
    macro = intelligence.get("macro", {})
    sentiment = intelligence.get("sentiment", {})
    calendar = intelligence.get("calendar", [])

    closed = completed_candles(candles) if candles else []
    closes = [c.close for c in closed] if closed else []
    current_price = _safe_float(ticker.get("lastPrice"), closes[-1] if closes else 0.0)

    trend_primary = analyze_trend(candles) if candles else {}
    trend_higher = {}
    htf_candle_structures = {}
    for tf_key, tf_candles in multi_tf.items():
        if tf_candles:
            trend_higher[tf_key] = analyze_trend(tf_candles)
            htf_candle_structures[tf_key] = _summarize_htf_candles(tf_candles)

    # Multi-timeframe alignment
    tf_statuses = [trend_primary.get("status", "sideways_or_mixed")]
    for t in trend_higher.values():
        tf_statuses.append(t.get("status", "sideways_or_mixed"))

    bullish_count = sum(1 for s in tf_statuses if s == "bullish")
    bearish_count = sum(1 for s in tf_statuses if s == "bearish")
    total_tfs = len(tf_statuses)

    mtf_alignment = "STRONG_BULLISH" if bullish_count == total_tfs else \
                    "BULLISH" if bullish_count >= total_tfs * 0.6 else \
                    "STRONG_BEARISH" if bearish_count == total_tfs else \
                    "BEARISH" if bearish_count >= total_tfs * 0.6 else \
                    "MIXED"

    # ── Momentum features ────────────────────────────────────────────────
    momentum = analyze_momentum(candles) if candles else {}
    rsi_val = _rsi(closes) if len(closes) > 14 else 50.0
    stoch_rsi = _stoch_rsi(closes) if len(closes) > 28 else {"k": 50.0, "d": 50.0}
    macd = _macd(closes) if len(closes) > 35 else {"macd_line": 0, "signal_line": 0, "histogram": 0}
    williams = _williams_r(closed) if len(closed) > 14 else -50.0
    cci_val = _cci(closed) if len(closed) > 20 else 0.0
    roc_val = _roc(closes) if len(closes) > 10 else 0.0

    # ── Volatility features ──────────────────────────────────────────────
    atr_val = compute_atr(candles) if candles else 0.0
    bb = _bollinger(closes)
    atr_pct = (atr_val / current_price * 100.0) if current_price > 0 else 0.0

    # Keltner squeeze detection (simple: BB inside Keltner)
    ema20 = _ema(closes, 20)[-1] if len(closes) >= 20 else (sum(closes) / len(closes) if closes else 0)
    keltner_upper = ema20 + 1.5 * atr_val
    keltner_lower = ema20 - 1.5 * atr_val
    in_squeeze = bb["upper"] < keltner_upper and bb["lower"] > keltner_lower if bb["upper"] > 0 else False

    # ── Volume features ──────────────────────────────────────────────────
    obv = _obv_trend(closed) if closed else "NEUTRAL"
    cum_delta = _cumulative_delta(closed) if closed else {"delta": 0, "bias": "NEUTRAL"}
    vwap = _vwap_deviation(closed) if closed else {"vwap": 0, "deviation_pct": 0}

    # Real volume indicators (now fully implemented)
    obv_value = compute_obv(candles) if candles else 0.0
    cmf_value = compute_cmf(candles) if candles else 0.0
    mfi_value = compute_mfi(candles) if candles else 50.0
    vwap_value = compute_vwap(candles) if candles else 0.0
    vwap_deviation_pct = ((current_price - vwap_value) / vwap_value * 100.0) if vwap_value > 0 else 0.0

    # Volume ratio (current vs average)
    if len(closed) >= 21:
        # Compare the latest completed candle with the preceding baseline;
        # including the latest candle makes genuine spikes look ordinary.
        baseline = closed[-21:-1]
        avg_vol = sum(c.volume for c in baseline) / len(baseline)
        latest_vol = closed[-1].volume
        vol_ratio = (latest_vol / avg_vol) if avg_vol > 0 else 1.0
    else:
        vol_ratio = 1.0

    # ── Microstructure features ──────────────────────────────────────────
    # Keep all candle-derived evidence on completed bars. The ticker/order
    # book may be live, but a partially formed OHLCV bar is not stable enough
    # for a directional decision.
    micro = analyze_microstructure(order_book, closed) if closed else {}

    # Trade flow from recent trades
    whale_trades = {"whale_buy_count": 0, "whale_sell_count": 0, "whale_buy_volume": 0.0, "whale_sell_volume": 0.0, "whale_bias": "NEUTRAL"}
    if recent_trades:
        buy_trades = [t for t in recent_trades if not t.get("isBuyerMaker")]
        sell_trades = [t for t in recent_trades if t.get("isBuyerMaker")]
        buy_vol = sum(float(t.get("qty", 0)) for t in buy_trades)
        sell_vol = sum(float(t.get("qty", 0)) for t in sell_trades)
        total_vol = buy_vol + sell_vol
        trade_flow_ratio = (buy_vol / total_vol) if total_vol > 0 else 0.5

        # Whale / large trade detection — orders > 2 std above mean size
        all_qtys = [float(t.get("qty", 0)) for t in recent_trades if float(t.get("qty", 0)) > 0]
        if len(all_qtys) >= 10:
            import numpy as np
            mean_qty = float(np.mean(all_qtys))
            std_qty = float(np.std(all_qtys))
            whale_threshold = mean_qty + 2.0 * std_qty if std_qty > 0 else mean_qty * 3.0
            for t in recent_trades:
                qty = float(t.get("qty", 0))
                if qty >= whale_threshold:
                    if not t.get("isBuyerMaker"):
                        whale_trades["whale_buy_count"] += 1
                        whale_trades["whale_buy_volume"] += qty
                    else:
                        whale_trades["whale_sell_count"] += 1
                        whale_trades["whale_sell_volume"] += qty
            wb = whale_trades["whale_buy_volume"]
            ws = whale_trades["whale_sell_volume"]
            if wb > ws * 1.5:
                whale_trades["whale_bias"] = "WHALE_BUYING"
            elif ws > wb * 1.5:
                whale_trades["whale_bias"] = "WHALE_SELLING"
    else:
        trade_flow_ratio = 0.5

    # ── Statistical features ─────────────────────────────────────────────
    stats = build_statistical_features(closed) if closed else {}

    # ── Regime detection ─────────────────────────────────────────────────
    regime_result = classify_market_state(stats, micro) if stats and micro else {}
    market_regime = detect_market_regime(candles, atr_val) if candles else "UNKNOWN"

    # ── Derivatives features ─────────────────────────────────────────────
    funding_rate = _safe_float(funding_data.get("funding_rate"))
    open_interest_val = _safe_float(oi_data.get("open_interest"))
    squeeze_data = analyze_funding_oi_divergence(funding_rate, open_interest_val)

    ls_ratio = derivatives.get("long_short_ratio", {})
    top_traders = derivatives.get("top_trader_positions", {})
    taker_vol = derivatives.get("taker_buy_sell_volume", {})
    oi_hist = derivatives.get("oi_history", {})

    # OI Delta Rate — how fast is OI changing?
    oi_delta = {"oi_change_pct": 0.0, "oi_trend": "STABLE"}
    if oi_hist.get("available"):
        # ``fetch_oi_history`` already calculates this from the Binance series.
        # The prior feature code expected a non-existent raw ``history`` field,
        # making OI delta permanently show as zero/stable.
        oi_change_pct = _safe_float(oi_hist.get("oi_change_pct"))
        oi_delta["oi_change_pct"] = round(oi_change_pct, 2)
        if oi_change_pct > 5.0:
            oi_delta["oi_trend"] = "RISING_FAST"
        elif oi_change_pct > 1.0:
            oi_delta["oi_trend"] = "RISING"
        elif oi_change_pct < -5.0:
            oi_delta["oi_trend"] = "FALLING_FAST"
        elif oi_change_pct < -1.0:
            oi_delta["oi_trend"] = "FALLING"

    # Liquidation heatmap
    if closed:
        prev_high = max(c.high for c in closed[-20:]) if len(closed) >= 20 else closed[-1].high
        prev_low = min(c.low for c in closed[-20:]) if len(closed) >= 20 else closed[-1].low
        liquidations = calculate_liquidation_heatmap(current_price, prev_high, prev_low, atr_val, open_interest_val)
    else:
        liquidations = {}

    # ── Liquidity & sweep ────────────────────────────────────────────────
    liquidity = analyze_liquidity(ticker, order_book) if ticker else {}
    order_book_analysis = analyze_order_book(order_book) if order_book else {}
    sweep = detect_liquidity_sweep(candles) if candles else {}
    # Shared SMC structure is computed once from completed candles and passed
    # directly to the committee; it is no longer merely a dashboard-side idea.
    market_structure = {
        "phase": classify_market_phase(candles) if candles else "RANGING",
        "bos": detect_bos(candles) if candles else {"detected": False, "direction": "none"},
        "choch": detect_choch(candles) if candles else {"detected": False, "direction": "none"},
        "order_blocks": find_order_blocks(candles) if candles else [],
        "fair_value_gaps": find_fair_value_gaps(candles) if candles else [],
        "limitations": "Structure labels are confluence evidence, not proof of institutional intent.",
    }

    # ── Cross-asset features ─────────────────────────────────────────────
    dxy = macro.get("DXY (Dollar Index)", {})
    nq = macro.get("NASDAQ Futures", {})
    gold = macro.get("Gold Futures", {})
    yields_10y = macro.get("10Y Treasury Yield", {})

    cross_asset = {
        "dxy": {
            "close": dxy.get("close"),
            "change_pct": dxy.get("change_pct"),
            "bias": "RISK_OFF" if _safe_float(dxy.get("change_pct")) > 0.3 else
                    "RISK_ON" if _safe_float(dxy.get("change_pct")) < -0.3 else "NEUTRAL",
        },
        "nasdaq": {
            "close": nq.get("close"),
            "change_pct": nq.get("change_pct"),
        },
        "gold": {
            "close": gold.get("close"),
            "change_pct": gold.get("change_pct"),
        },
        "yields_10y": {
            "close": yields_10y.get("close"),
            "change_pct": yields_10y.get("change_pct"),
        },
        "risk_environment": _classify_risk_env(dxy, nq, gold, yields_10y),
    }

    # ── Sentiment features ───────────────────────────────────────────────
    fear_greed = sentiment.get("fear_greed", {})

    # ── Calendar risk ────────────────────────────────────────────────────
    high_impact_upcoming = [e for e in calendar if e.get("importance") == "HIGH"]

    # ── Assemble ─────────────────────────────────────────────────────────
    features: dict[str, Any] = {
        "current_price": current_price,
        "trend": {
            "primary": trend_primary,
            "higher_timeframes": trend_higher,
            "htf_candle_structures": htf_candle_structures,
            "mtf_alignment": mtf_alignment,
            "bullish_tfs": bullish_count,
            "bearish_tfs": bearish_count,
            "total_tfs": total_tfs,
        },
        "momentum": {
            "summary": momentum,
            "rsi": round(rsi_val, 2),
            "stoch_rsi": stoch_rsi,
            "macd": macd,
            "williams_r": williams,
            "cci": cci_val,
            "roc": roc_val,
        },
        "volatility": {
            "atr": round(atr_val, 6),
            "atr_pct": round(atr_pct, 4),
            "atr_percentile": round(_atr_percentile(closed, atr_val), 1) if closed else 50.0,
            "bollinger": bb,
            "in_squeeze": in_squeeze,
            "regime": market_regime,
        },
        "volume": {
            "obv_trend": obv,
            "obv_value": round(obv_value, 4),
            "cmf": round(cmf_value, 4),
            "mfi": round(mfi_value, 2),
            "vwap": vwap,
            "vwap_price": round(vwap_value, 4),
            "vwap_deviation_pct": round(vwap_deviation_pct, 4),
            "cumulative_delta": cum_delta,
            "volume_ratio": round(vol_ratio, 2),
        },
        "microstructure": micro,
        "trade_flow": {
            "buy_ratio": round(trade_flow_ratio, 4),
            "bias": "BUYING" if trade_flow_ratio > 0.55 else (
                "SELLING" if trade_flow_ratio < 0.45 else "NEUTRAL"
            ),
            "whale_activity": whale_trades,
        },
        "statistical": stats,
        "regime": regime_result,
        "derivatives": {
            "funding_rate": round(funding_rate, 6),
            "open_interest": round(open_interest_val, 2),
            "oi_delta": oi_delta,
            "squeeze": squeeze_data,
            "long_short_ratio": ls_ratio,
            "top_traders": top_traders,
            "taker_volume": taker_vol,
            "oi_history": oi_hist,
            "liquidations": liquidations,
        },
        "liquidity": liquidity,
        "order_book": order_book_analysis,
        "sweep": sweep,
        "market_structure": market_structure,
        "cross_asset": cross_asset,
        "sentiment": {
            "fear_greed_value": fear_greed.get("value", 50),
            "fear_greed_zone": fear_greed.get("zone", "NEUTRAL"),
            "available": fear_greed.get("available", False),
        },
        "calendar_risk": {
            "high_impact_events": len(high_impact_upcoming),
            "events": high_impact_upcoming[:5],
        },
        "data_quality": _data_quality(intelligence, closed, current_price),
    }

    return features


def _data_quality(intelligence: dict[str, Any], closed: list[Candle], current_price: float) -> dict[str, Any]:
    """Expose whether the signal was computed from a complete usable snapshot."""
    meta = intelligence.get("meta", {}) or {}
    available = list(meta.get("sources_available", []))
    failed = list(meta.get("sources_failed", []))
    required = {"candles", "ticker", "order_book"}
    missing_required = sorted(required - set(available))
    passed = bool(closed and current_price > 0 and not missing_required)
    return {
        "passed": passed,
        "closed_candles": len(closed),
        "sources_available": available,
        "sources_failed": failed,
        "missing_required": missing_required,
        "coverage_pct": round(len(available) / max(int(meta.get("total_sources", len(available) or 1)), 1) * 100, 1),
        "reason": "complete_core_snapshot" if passed else "missing_core_market_data",
    }


def _classify_risk_env(dxy: dict, nq: dict, gold: dict, yields: dict) -> str:
    """Classify macro risk environment from cross-asset moves."""
    dxy_chg = _safe_float(dxy.get("change_pct"))
    nq_chg = _safe_float(nq.get("change_pct"))
    gold_chg = _safe_float(gold.get("change_pct"))

    # Risk-off: DXY up, stocks down, gold up
    risk_off_score = 0
    if dxy_chg > 0.2:
        risk_off_score += 1
    if nq_chg < -0.3:
        risk_off_score += 1
    if gold_chg > 0.3:
        risk_off_score += 1

    if risk_off_score >= 2:
        return "RISK_OFF"
    elif dxy_chg < -0.2 and nq_chg > 0.3:
        return "RISK_ON"
    return "NEUTRAL"
