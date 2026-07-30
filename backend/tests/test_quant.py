"""Unit and integration tests for the quantitative research platform features."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.indicators.quantitative import hurst_exponent, parkinson_volatility
from app.data_sources.binance_public import Candle
from app.main import app


def _dummy_candles(count: int = 40, start_price: float = 100.0) -> list[Candle]:
    return [
        Candle(
            open_time=i * 60000,
            open=start_price,
            high=start_price + 1.0,
            low=start_price - 1.0,
            close=start_price + (i % 2 - 0.5),
            volume=1000.0,
            close_time=(i * 60000) + 59999,
            quote_volume=100000.0,
            trade_count=100,
            taker_buy_base_volume=500.0,
            taker_buy_quote_volume=50000.0,
        )
        for i in range(count)
    ]


def test_hurst_exponent_and_parkinson_volatility() -> None:
    candles = _dummy_candles(50, 100.0)
    prices = [c.close for c in candles]

    # Verify Hurst Exponent calculates valid bounds
    h = hurst_exponent(prices)
    assert isinstance(h, float)

    # Verify Parkinson Volatility calculations
    p_vol = parkinson_volatility(candles)
    assert isinstance(p_vol, float)
    assert p_vol >= 0.0


def test_alpha_endpoints() -> None:
    client = TestClient(app)

    # 1. Hypotheses registry
    r_hyp = client.get("/alpha/hypotheses")
    assert r_hyp.status_code == 200
    data_hyp = r_hyp.json()
    assert "hypotheses" in data_hyp
    assert "order_book_imbalance" in data_hyp["hypotheses"]

    # 2. Candidate series validation
    feature = [0.1 * i for i in range(40)]
    future_returns = [0.01 * (i % 3 - 1) for i in range(40)]
    r_val = client.post(
        "/alpha/validate",
        json={
            "feature": feature,
            "future_returns": future_returns,
            "train_fraction": 0.7,
        },
    )
    assert r_val.status_code == 200
    data_val = r_val.json()
    assert "status" in data_val
    assert "train_information_coefficient" in data_val

    # Synthetic/demo observations must not be creatable through the
    # production research API because they contaminate alpha statistics.
    r_seed = client.post("/alpha/seed")
    assert r_seed.status_code == 404

    # 4. Fetch Alpha Report
    r_rep = client.get("/alpha/report")
    assert r_rep.status_code == 200
    data_rep = r_rep.json()
    assert data_rep["status"] in {"active", "insufficient_data"}
    if data_rep["status"] == "active":
        assert "discovered_edges" in data_rep
        assert "actual_execution_flow" in data_rep["discovered_edges"]
        assert "causal_context_alignment" in data_rep["discovered_edges"]
