"""Alpha hypothesis registry and leakage-aware validation primitives."""
from __future__ import annotations

from math import sqrt
from typing import Any

import numpy as np


HYPOTHESES = {
    "order_book_imbalance": {
        "description": "Does signed depth imbalance predict the next-horizon return?",
        "required_data": ["timestamped incremental order-book snapshots", "mid prices"],
    },
    "taker_flow": {
        "description": "Does aggressive taker flow predict the next-horizon return?",
        "required_data": ["timestamped trades or taker volumes", "mid prices"],
    },
    "funding_divergence": {
        "description": "Do extreme funding and price divergences precede reversals?",
        "required_data": ["funding history", "open interest history", "prices"],
    },
    "liquidation_clustering": {
        "description": "Do liquidation clusters alter return distributions after a trigger?",
        "required_data": ["liquidation events", "trade data", "prices"],
    },
}


def list_hypotheses() -> dict[str, Any]:
    return {"research_principles": ["walk-forward splits", "purged/embargoed validation", "cost-aware evaluation", "regime slices", "multiple-testing control"], "hypotheses": HYPOTHESES}


def validate_series(feature: list[float], future_returns: list[float], *, train_fraction: float = 0.7) -> dict[str, Any]:
    """Evaluate directional information coefficient on a held-out time split."""
    n = min(len(feature), len(future_returns))
    if n < 30:
        raise ValueError("At least 30 aligned observations are required for alpha validation.")
    x, y = np.asarray(feature[-n:], dtype=float), np.asarray(future_returns[-n:], dtype=float)
    valid = np.isfinite(x) & np.isfinite(y)
    x, y = x[valid], y[valid]
    if len(x) < 30 or np.std(x) == 0 or np.std(y) == 0:
        raise ValueError("Feature and forward returns must have sufficient non-zero variation.")
    split = max(20, min(len(x) - 10, int(len(x) * train_fraction)))
    train_ic = float(np.corrcoef(x[:split], y[:split])[0, 1])
    test_ic = float(np.corrcoef(x[split:], y[split:])[0, 1])
    test_n = len(x) - split
    t_stat = test_ic * sqrt(max(test_n - 2, 1) / max(1 - test_ic**2, 1e-12))
    decay = abs(test_ic) / max(abs(train_ic), 1e-9)
    return {
        "status": "candidate" if abs(test_ic) >= 0.05 and abs(t_stat) >= 1.5 else "not_validated",
        "observations": int(len(x)),
        "train_information_coefficient": round(train_ic, 5),
        "test_information_coefficient": round(test_ic, 5),
        "test_t_stat": round(t_stat, 4),
        "edge_stability_ratio": round(decay, 4),
        "next_steps": [
            "Repeat using purged walk-forward folds and realistic fees/slippage.",
            "Check performance by market regime and across instruments.",
            "Register only after passing out-of-sample and capacity review.",
        ],
    }
