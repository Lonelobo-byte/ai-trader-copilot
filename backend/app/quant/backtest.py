"""Backtesting Engine and Performance Tracker module."""
from __future__ import annotations

import logging
from math import sqrt
from statistics import mean
from typing import Any, Dict, List

import numpy as np

from app.analysis_pipeline import run_full_analysis
from app.data_sources.binance_public import Candle

logger = logging.getLogger(__name__)

REGIME_NAMES = ("bull", "bear", "sideways", "panic", "low_liquidity")
BASELINE_NAMES = ("ema20_ema50", "momentum_10", "breakout_20", "institutional_quant_proxy")


def _ema_value(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    if len(values) < period:
        return sum(values) / len(values)
    result = sum(values[:period]) / period
    alpha = 2.0 / (period + 1.0)
    for value in values[period:]:
        result = value * alpha + result * (1.0 - alpha)
    return result


def _regime_at(candles: list[Candle], index: int) -> str:
    """Classify the observable regime at a historical decision candle."""
    if index <= 0 or index >= len(candles):
        return "sideways"
    recent = candles[max(0, index - 50):index + 1]
    closes = [float(c.close) for c in recent]
    ranges = [max(float(c.high) - float(c.low), 0.0) for c in recent]
    volumes = [max(float(c.volume), 0.0) for c in recent]
    if len(closes) < 20:
        return "sideways"

    avg_range = mean(ranges[-20:]) or 1e-9
    current_range = ranges[-1]
    returns = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes)) if closes[i - 1] > 0]
    volatility = float(np.std(returns[-20:])) if len(returns) > 1 else 0.0
    last_return = returns[-1] if returns else 0.0
    if current_range >= avg_range * 2.5 or (volatility > 0 and abs(last_return) >= volatility * 3.0):
        return "panic"

    avg_volume = mean(volumes[-20:]) or 1e-9
    if volumes[-1] < avg_volume * 0.50:
        return "low_liquidity"

    fast = _ema_value(closes, 20)
    slow = _ema_value(closes, 50)
    slope_base = _ema_value(closes[:-5], 20) if len(closes) > 25 else fast
    slope = fast / slope_base - 1.0 if slope_base else 0.0
    if fast > slow and slope > 0:
        return "bull"
    if fast < slow and slope < 0:
        return "bear"
    return "sideways"


def _baseline_direction(candles: list[Candle], index: int, baseline: str) -> int:
    """Return a non-lookahead baseline direction: 1 long, -1 short, 0 flat."""
    closes = [float(c.close) for c in candles[:index + 1]]
    if len(closes) < 21:
        return 0
    if baseline == "ema20_ema50":
        fast, slow = _ema_value(closes, 20), _ema_value(closes, 50)
        return 1 if closes[-1] > fast > slow else -1 if closes[-1] < fast < slow else 0
    if baseline == "momentum_10":
        reference = closes[-11]
        return 1 if closes[-1] > reference else -1 if closes[-1] < reference else 0
    if baseline == "breakout_20":
        prior = closes[-21:-1]
        return 1 if closes[-1] > max(prior) else -1 if closes[-1] < min(prior) else 0
    if baseline == "institutional_quant_proxy":
        votes = [
            _baseline_direction(candles, index, "ema20_ema50"),
            _baseline_direction(candles, index, "momentum_10"),
            _baseline_direction(candles, index, "breakout_20"),
        ]
        score = sum(votes)
        return 1 if score >= 2 else -1 if score <= -2 else 0
    return 0


def _bootstrap_summary(values: list[float], *, seed: int = 42, samples: int = 2000) -> dict[str, Any]:
    """Return conservative bootstrap evidence for a mean, not a guarantee."""
    n = len(values)
    if n == 0:
        return {"n": 0, "mean": 0.0, "ci95": [None, None], "bootstrap_prob_mean_gt_zero": None, "status": "no_observations"}
    arr = np.asarray(values, dtype=float)
    avg = float(np.mean(arr))
    if n < 2:
        return {"n": n, "mean": round(avg, 6), "ci95": [None, None], "bootstrap_prob_mean_gt_zero": None, "status": "insufficient_sample"}
    rng = np.random.default_rng(seed)
    indices = rng.integers(0, n, size=(samples, n))
    boot_means = arr[indices].mean(axis=1)
    ci = np.quantile(boot_means, [0.025, 0.975])
    return {
        "n": n,
        "mean": round(avg, 6),
        "ci95": [round(float(ci[0]), 6), round(float(ci[1]), 6)],
        "bootstrap_prob_mean_gt_zero": round(float(np.mean(boot_means > 0)), 4),
        "status": "usable_but_not_proof" if n < 30 else "usable",
    }


def _regime_report(trades: list[dict[str, Any]]) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for regime in REGIME_NAMES:
        subset = [t for t in trades if t.get("regime") == regime]
        returns = [float(t["r_return"]) for t in subset]
        equity = 0.0
        peak = 0.0
        drawdown = 0.0
        for value in returns:
            equity += value
            peak = max(peak, equity)
            drawdown = max(drawdown, peak - equity)
        report[regime] = {
            "trades": len(subset),
            "win_rate": round(sum(v > 0 for v in returns) / len(returns), 4) if returns else 0.0,
            "expectancy_r": round(float(np.mean(returns)), 4) if returns else 0.0,
            "max_drawdown_r": round(drawdown, 4),
            "status": "observed" if returns else "no_observations",
        }
    return report


def _statistical_validation(trades: list[dict[str, Any]], candles: list[Candle], cost_pct: float) -> dict[str, Any]:
    strategy_r = [float(t["r_return"]) for t in trades]
    result: dict[str, Any] = {
        "minimum_recommended_trades": 30,
        "strategy_expectancy": _bootstrap_summary(strategy_r),
        "baseline_comparisons": {},
        "status": "not_proven",
        "method": "Matched trade-window bootstrap; exploratory, not a formal institutional validation protocol.",
    }
    if not trades:
        return result

    for baseline in BASELINE_NAMES:
        differences: list[float] = []
        for trade in trades:
            entry_idx = int(trade.get("entry_index", trade.get("_entry_idx", 0)))
            exit_idx = int(trade.get("exit_index", trade.get("closed_at_idx", trade.get("_exit_idx", entry_idx))))
            if exit_idx <= entry_idx or entry_idx >= len(candles):
                continue
            direction = _baseline_direction(candles, entry_idx, baseline)
            if direction == 0:
                baseline_return = 0.0
            else:
                entry = float(candles[entry_idx].close)
                exit_price = float(candles[min(exit_idx, len(candles) - 1)].close)
                raw = (exit_price / entry - 1.0) * 100.0 if entry else 0.0
                baseline_return = direction * raw - cost_pct
            differences.append(float(trade["pct_return"]) - baseline_return)
        result["baseline_comparisons"][baseline] = _bootstrap_summary(differences)

    expectancy_ci = result["strategy_expectancy"]["ci95"]
    comparison_cis = [v["ci95"] for v in result["baseline_comparisons"].values()]
    enough_data = len(trades) >= 30
    result["status"] = (
        "candidate_edge_against_selected_baselines"
        if enough_data and expectancy_ci[0] is not None and expectancy_ci[0] > 0 and all(ci[0] is not None and ci[0] > 0 for ci in comparison_cis)
        else "not_proven"
    )
    return result


def _benchmark_comparison(
    candles: list[Candle],
    start_offset: int,
    fee_bps: float,
    slippage_bps: float,
) -> dict[str, Any]:
    """Compare the strategy with transparent, non-optimized baselines."""
    closes = [float(c.close) for c in candles]
    if len(closes) <= start_offset + 1:
        return {"status": "insufficient_data"}

    cost_pct = max(0.0, (2.0 * (fee_bps + slippage_bps)) / 100.0)
    first, last = closes[start_offset], closes[-1]
    buy_hold = (last / first - 1.0) * 100.0 if first > 0 else 0.0

    baseline_metrics: dict[str, Any] = {}
    for baseline in BASELINE_NAMES:
        returns: list[float] = []
        for idx in range(start_offset, len(closes) - 1):
            direction = _baseline_direction(candles, idx, baseline)
            if direction:
                next_return_pct = (closes[idx + 1] / closes[idx] - 1.0) * 100.0
                returns.append(direction * next_return_pct - cost_pct)
        equity = 100.0
        peak = equity
        max_drawdown = 0.0
        for ret in returns:
            equity += ret
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)
        baseline_metrics[baseline] = {
            "return_pct": round(sum(returns), 4),
            "observations": len(returns),
            "directional_accuracy": round(sum(ret > 0 for ret in returns) / len(returns), 4) if returns else 0.0,
            "max_drawdown_pct": round(max_drawdown, 4),
        }

    return {
        "status": "completed",
        "buy_and_hold_return_pct": round(buy_hold, 4),
        "ema20_ema50_return_pct": baseline_metrics["ema20_ema50"]["return_pct"],
        "ema20_ema50_observations": baseline_metrics["ema20_ema50"]["observations"],
        "ema20_ema50_directional_accuracy": baseline_metrics["ema20_ema50"]["directional_accuracy"],
        "ema20_ema50_max_drawdown_pct": baseline_metrics["ema20_ema50"]["max_drawdown_pct"],
        "baselines": baseline_metrics,
        "cost_model": {
            "fee_bps_per_side": round(fee_bps, 3),
            "slippage_bps_per_side": round(slippage_bps, 3),
        },
        "note": "Baselines are transparent references, not optimized trading strategies.",
    }


async def run_backtest(
    symbol: str,
    timeframe: str,
    candles: list[Candle],
    settings: Any,
    start_offset: int = 100,
    fee_bps: float = 4.0,
    slippage_bps: float = 2.0,
) -> dict[str, Any]:
    """Walk forward through historical candles and replay the analysis pipeline.

    Calculates performance metrics, win rates, Sharpe ratio, expectancy, and targets hit rate.
    """
    total_candles = len(candles)
    cost_pct = max(0.0, (2.0 * (fee_bps + slippage_bps)) / 100.0)
    benchmark = _benchmark_comparison(candles, start_offset, fee_bps, slippage_bps)
    if total_candles < start_offset + 10:
        return {
            "status": "insufficient_data",
            "message": f"Need at least {start_offset + 10} candles to backtest, got {total_candles}",
        }

    # Simulate basic order book and ticker objects
    mock_order_book = {
        "bids": [[100.0, 10.0]],
        "asks": [[100.0, 10.0]],
    }
    
    trades: List[Dict[str, Any]] = []
    active_trade: Dict[str, Any] | None = None

    # Step-by-step walk-forward replay
    for t in range(start_offset, total_candles - 1):
        current_candle = candles[t]
        close_price = float(current_candle.close)

        # 1. Update any active trade
        if active_trade:
            low_p = float(current_candle.low)
            high_p = float(current_candle.high)
            side = active_trade["side"]
            stop_loss = active_trade["stop"]
            tp1 = active_trade["targets"]["tp1_1r"]
            tp2 = active_trade["targets"]["tp2_2r"]
            tp3 = active_trade["targets"]["tp3_3r"]
            tp_runner = active_trade["targets"]["runner_5r"]

            # Evaluate execution outcomes
            stopped = False
            hit_tp = []

            if side == "LONG":
                if low_p <= stop_loss:
                    stopped = True
                if high_p >= tp1:
                    hit_tp.append("tp1")
                if high_p >= tp2:
                    hit_tp.append("tp2")
                if high_p >= tp3:
                    hit_tp.append("tp3")
                if high_p >= tp_runner:
                    hit_tp.append("runner")
            else:  # SHORT
                if high_p >= stop_loss:
                    stopped = True
                if low_p <= tp1:
                    hit_tp.append("tp1")
                if low_p <= tp2:
                    hit_tp.append("tp2")
                if low_p <= tp3:
                    hit_tp.append("tp3")
                if low_p <= tp_runner:
                    hit_tp.append("runner")

            # Determine trade resolution
            if stopped:
                active_trade["status"] = "STOPPED_OUT"
                active_trade["exit_price"] = stop_loss
                active_trade["closed_at_idx"] = t
                # Return is -1.0 R-multiple
                active_trade["r_return"] = -1.0 - cost_pct / max(float(active_trade["stop_distance_pct"]), 1e-9)
                active_trade["pct_return"] = -float(active_trade["stop_distance_pct"]) - cost_pct
                active_trade["cost_pct"] = cost_pct
                trades.append(active_trade)
                active_trade = None
            elif "runner" in hit_tp:
                active_trade["status"] = "HIT_RUNNER"
                active_trade["exit_price"] = tp_runner
                active_trade["closed_at_idx"] = t
                active_trade["r_return"] = 5.0 - cost_pct / max(float(active_trade["stop_distance_pct"]), 1e-9)
                active_trade["pct_return"] = float(active_trade["stop_distance_pct"]) * 5.0 - cost_pct
                active_trade["cost_pct"] = cost_pct
                trades.append(active_trade)
                active_trade = None
            else:
                # Update highest target hit
                for level in ["tp3", "tp2", "tp1"]:
                    if level in hit_tp and level not in active_trade["hit_targets"]:
                        active_trade["hit_targets"].append(level)
                        # Lock in/trail stop optionally
                        if level == "tp1":
                            # Move stop to breakeven
                            active_trade["stop"] = active_trade["entry"]

            # If trade has been active for too long (e.g. 50 candles), close it
            if active_trade and (t - active_trade["entry_at_idx"] >= 50):
                active_trade["status"] = "TIMEOUT"
                active_trade["exit_price"] = close_price
                active_trade["closed_at_idx"] = t
                # TP1 moves the active stop to breakeven. Use the immutable
                # initial stop for R accounting so a later timeout cannot
                # divide by zero or rewrite the original trade risk.
                initial_stop = float(active_trade.get("initial_stop", stop_loss))
                denominator = abs(active_trade["entry"] - initial_stop)
                r_mult = (
                    ((close_price - active_trade["entry"]) / denominator)
                    if side == "LONG" else ((active_trade["entry"] - close_price) / denominator)
                ) if denominator > 0 else 0.0
                active_trade["r_return"] = float(r_mult) - cost_pct / max(float(active_trade["stop_distance_pct"]), 1e-9)
                active_trade["pct_return"] = float(r_mult) * float(active_trade["stop_distance_pct"]) - cost_pct
                active_trade["cost_pct"] = cost_pct
                trades.append(active_trade)
                active_trade = None

        # 2. Run analysis at step t to check for new setups
        if active_trade is None:
            slice_candles = candles[: t + 1]
            mock_ticker = {
                "lastPrice": str(close_price),
                "highPrice": str(max(c.high for c in slice_candles[-20:])),
                "lowPrice": str(min(c.low for c in slice_candles[-20:])),
            }
            historical_intelligence = {
                "symbol": symbol,
                "timeframe": timeframe,
                "candles": slice_candles,
                "ticker": mock_ticker,
                "order_book": mock_order_book,
                "multi_tf_candles": {},
                "funding": {},
                "open_interest": {},
                "derivatives": {},
                "recent_trades": [],
                "news": [],
                "global_news": [],
                "macro": {},
                "sentiment": {},
                "calendar": [],
                "global_liquidity": {},
                "options": {},
                "meta": {
                    "sources_available": ["historical_candles", "historical_ticker", "synthetic_order_book"],
                    "sources_failed": [],
                },
            }

            try:
                # Run with use_ai=False to make it fast
                payload, _ = await run_full_analysis(
                    symbol=symbol,
                    timeframe=timeframe,
                    candles=slice_candles,
                    ticker=mock_ticker,
                    order_book_raw=mock_order_book,
                    settings=settings,
                    use_ai=False,
                    market_intelligence=historical_intelligence,
                    reconcile_signals=False,
                    historical_replay=True,
                )
            except Exception as e:
                logger.error(f"Error in backtest pipeline execution: {e}")
                continue

            setup = payload.get("trade_setup", {})
            # A research watch is not an executable historical trade. Replay
            # only plans which the same lifecycle approval actually released.
            release = payload.get("signal_monitor", {})
            if (
                setup
                and setup.get("status") == "READY_FOR_MANUAL_REVIEW"
                and release.get("status") in {"SIMULATED_APPROVED", "PENDING_ENTRY", "ACTIVE"}
            ):
                side = setup["side"]
                entry_ref = float(setup["entry"]["reference"])
                stop_ref = float(setup["stop"]["selected"])
                targets = setup["targets"]
                stop_dist = float(setup["stop"]["distance_pct"])

                # Trigger active trade starting next candle
                active_trade = {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "side": side,
                    "entry": entry_ref,
                    "stop": stop_ref,
                    "initial_stop": stop_ref,
                    "targets": targets,
                    "stop_distance_pct": stop_dist,
                    "entry_at_idx": t,
                    "entry_index": t,
                    "regime": _regime_at(candles, t),
                    "hit_targets": [],
                    "status": "ACTIVE",
                    "r_return": 0.0,
                    "pct_return": 0.0,
                }

    # Close any remaining active trade at the final candle
    if active_trade:
        final_close = float(candles[-1].close)
        active_trade["status"] = "EXPIRED"
        active_trade["exit_price"] = final_close
        active_trade["closed_at_idx"] = total_candles - 1
        initial_stop = active_trade.get("initial_stop", active_trade["stop"])
        denom = abs(active_trade["entry"] - initial_stop)
        if denom > 0:
            r_mult = (final_close - active_trade["entry"]) / (active_trade["entry"] - initial_stop) if active_trade["side"] == "LONG" else (active_trade["entry"] - final_close) / (initial_stop - active_trade["entry"])
        else:
            r_mult = 0.0
        active_trade["r_return"] = float(r_mult) - cost_pct / max(float(active_trade["stop_distance_pct"]), 1e-9)
        active_trade["pct_return"] = float(r_mult) * float(active_trade["stop_distance_pct"]) - cost_pct
        active_trade["cost_pct"] = cost_pct
        trades.append(active_trade)

    # 3. Calculate Performance Metrics
    total_trades = len(trades)
    if total_trades == 0:
        return {
            "status": "completed",
            "symbol": symbol,
            "timeframe": timeframe,
            "total_candles": total_candles,
            "total_trades": 0,
            "win_rate": 0.0,
            "sharpe_ratio": 0.0,
            "expectancy": 0.0,
            "benchmark_comparison": benchmark,
            "robustness_by_regime": _regime_report([]),
            "statistical_validation": _statistical_validation([], candles, cost_pct),
            "trades": [],
        }

    returns = [t["r_return"] for t in trades]
    win_count = sum(1 for t in trades if t["r_return"] > 0)
    win_rate = win_count / total_trades

    # Sharpe ratio of net R returns
    avg_return = np.mean(returns)
    std_return = np.std(returns, ddof=1) if len(returns) > 1 else 0.0
    sharpe = (avg_return / std_return) * sqrt(252) if std_return > 0 else 0.0

    # expectancy: Average R-multiple
    expectancy = avg_return
    gross_profit = sum(t["r_return"] for t in trades if t["r_return"] > 0)
    gross_loss = abs(sum(t["r_return"] for t in trades if t["r_return"] < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)

    # Hit rates by level
    tp1_hits = sum(1 for t in trades if "tp1" in t["hit_targets"] or t["status"] == "HIT_RUNNER")
    tp2_hits = sum(1 for t in trades if "tp2" in t["hit_targets"] or t["status"] == "HIT_RUNNER")
    tp3_hits = sum(1 for t in trades if "tp3" in t["hit_targets"] or t["status"] == "HIT_RUNNER")
    runner_hits = sum(1 for t in trades if t["status"] == "HIT_RUNNER")
    stopped_hits = sum(1 for t in trades if t["status"] == "STOPPED_OUT")

    # Construct cumulative return curve
    equity_curve = [100.0]
    curr_equity = 100.0
    for t in trades:
        curr_equity += t["pct_return"]
        equity_curve.append(round(curr_equity, 2))
    peak = equity_curve[0]
    max_drawdown = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, peak - value)

    return {
        "status": "completed",
        "symbol": symbol,
        "timeframe": timeframe,
        "total_candles": total_candles,
        "total_trades": total_trades,
        "win_rate": round(win_rate, 4),
        "sharpe_ratio": round(sharpe, 4),
        "expectancy": round(expectancy, 4),
        "profit_factor": round(profit_factor, 4) if np.isfinite(profit_factor) else None,
        "max_drawdown_pct": round(max_drawdown, 4),
        "total_cost_pct": round(sum(t.get("cost_pct", cost_pct) for t in trades), 4),
        "fee_bps_per_side": round(fee_bps, 3),
        "slippage_bps_per_side": round(slippage_bps, 3),
        "benchmark_comparison": benchmark,
        "robustness_by_regime": _regime_report(trades),
        "statistical_validation": _statistical_validation(trades, candles, cost_pct),
        "tp1_hit_rate": round(tp1_hits / total_trades, 4),
        "tp2_hit_rate": round(tp2_hits / total_trades, 4),
        "tp3_hit_rate": round(tp3_hits / total_trades, 4),
        "runner_hit_rate": round(runner_hits / total_trades, 4),
        "stopped_out_rate": round(stopped_hits / total_trades, 4),
        "equity_curve": equity_curve,
        "trades": [
            {
                "side": t["side"],
                "entry": round(t["entry"], 4),
                "stop": round(t["stop"], 4),
                "exit": round(t.get("exit_price", 0.0), 4),
                "status": t["status"],
                "r_return": round(t["r_return"], 2),
                "pct_return": round(t["pct_return"], 2),
                "duration_candles": t["closed_at_idx"] - t["entry_at_idx"],
                "entry_index": t["entry_at_idx"],
                "exit_index": t["closed_at_idx"],
                "entry_regime": t.get("regime", "sideways"),
            }
            for t in trades
        ],
    }
