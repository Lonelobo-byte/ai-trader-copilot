"""Volume indicators — OBV, CMF, MFI, VWAP.

Fully implemented institutional-grade volume analysis tools.
"""
from __future__ import annotations

from typing import Any

from app.data_sources.binance_public import Candle, completed_candles


def obv(candles: list[Candle]) -> float:
    """On-Balance Volume — cumulative volume flow indicator.

    Adds volume on up-close candles, subtracts on down-close candles.
    Rising OBV confirms bullish price trends; falling OBV confirms bearish.
    Divergence between OBV and price signals potential reversals.
    """
    closed = completed_candles(candles)
    if len(closed) < 2:
        return 0.0

    result = 0.0
    for i in range(1, len(closed)):
        if closed[i].close > closed[i - 1].close:
            result += closed[i].volume
        elif closed[i].close < closed[i - 1].close:
            result -= closed[i].volume
        # Equal closes: OBV unchanged
    return round(result, 4)


def cmf(candles: list[Candle], period: int = 20) -> float:
    """Chaikin Money Flow — measures institutional accumulation/distribution.

    Formula: CMF = Σ(CLV × Volume) / Σ(Volume) over the period
    where CLV = ((Close - Low) - (High - Close)) / (High - Low)

    CMF > 0 indicates buying pressure (accumulation).
    CMF < 0 indicates selling pressure (distribution).
    Values above +0.25 or below -0.25 are significant.
    """
    closed = completed_candles(candles)
    if len(closed) < period:
        return 0.0

    recent = closed[-period:]
    numerator = 0.0
    denominator = 0.0

    for c in recent:
        hl_range = c.high - c.low
        if hl_range > 0:
            # Close Location Value: where the close sits within the H-L range
            clv = ((c.close - c.low) - (c.high - c.close)) / hl_range
            numerator += clv * c.volume
        denominator += c.volume

    if denominator == 0:
        return 0.0

    return round(numerator / denominator, 4)


def mfi(candles: list[Candle], period: int = 14) -> float:
    """Money Flow Index — volume-weighted RSI.

    Formula: MFI = 100 - (100 / (1 + MoneyFlowRatio))
    where MoneyFlowRatio = PositiveMoneyFlow / NegativeMoneyFlow
    and MoneyFlow = TypicalPrice × Volume

    MFI > 80: overbought (potential selling pressure ahead).
    MFI < 20: oversold (potential buying pressure ahead).
    Divergence between MFI and price is a strong signal.
    """
    closed = completed_candles(candles)
    if len(closed) < period + 1:
        return 50.0

    # Calculate typical prices
    typical_prices = [(c.high + c.low + c.close) / 3.0 for c in closed]

    # Use the last (period + 1) candles to get 'period' comparisons
    start = len(closed) - period - 1
    positive_flow = 0.0
    negative_flow = 0.0

    for i in range(start + 1, len(closed)):
        tp_current = typical_prices[i]
        tp_previous = typical_prices[i - 1]
        money_flow = tp_current * closed[i].volume

        if tp_current > tp_previous:
            positive_flow += money_flow
        elif tp_current < tp_previous:
            negative_flow += money_flow
        # Equal typical prices: money flow is ignored

    if negative_flow == 0:
        return 100.0 if positive_flow > 0 else 50.0

    money_flow_ratio = positive_flow / negative_flow
    return round(100.0 - (100.0 / (1.0 + money_flow_ratio)), 2)


def vwap(candles: list[Candle]) -> float:
    """Volume Weighted Average Price — institutional reference level.

    Formula: VWAP = Σ(TypicalPrice × Volume) / Σ(Volume)
    where TypicalPrice = (High + Low + Close) / 3

    Price above VWAP: bullish bias (buyers in control).
    Price below VWAP: bearish bias (sellers in control).
    VWAP acts as dynamic support/resistance for institutional traders.
    """
    closed = completed_candles(candles)
    if not closed:
        return 0.0

    total_tp_vol = 0.0
    total_vol = 0.0

    for c in closed:
        tp = (c.high + c.low + c.close) / 3.0
        total_tp_vol += tp * c.volume
        total_vol += c.volume

    if total_vol == 0:
        return closed[-1].close if closed else 0.0

    return round(total_tp_vol / total_vol, 4)
