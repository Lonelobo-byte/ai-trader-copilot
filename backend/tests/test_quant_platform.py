from __future__ import annotations

from app.data_sources.binance_public import Candle
from app.quant.engine import build_quantitative_assessment
from app.quant.research import validate_series


def _candles() -> list[Candle]:
    rows = []
    for i in range(80):
        price = 100.0 + i * 0.08 + (i % 3 - 1) * 0.03
        rows.append(Candle(
            open_time=i * 60_000, open=price - 0.04, high=price + 0.12,
            low=price - 0.12, close=price, volume=1000.0 + i,
            close_time=i * 60_000 + 59_999, quote_volume=price * (1000.0 + i),
            trade_count=100 + i, taker_buy_base_volume=550.0,
            taker_buy_quote_volume=price * 550.0,
        ))
    return rows


def test_quantitative_assessment_is_probabilistic_and_non_executable() -> None:
    book = {
        "bids": [[100.0 - i * 0.01, 20.0] for i in range(1, 21)],
        "asks": [[100.0 + i * 0.01, 12.0] for i in range(1, 21)],
    }
    assessment = build_quantitative_assessment(
        _candles(), book, account_value=100_000, max_drawdown_pct=12.0, max_gross_exposure_pct=100.0,
    )
    forecast = assessment["probability_engine"]
    assert assessment["platform_mode"] == "quantitative_research_only"
    assert assessment["execution_policy"].startswith("no_order_submission")
    assert 0.0 <= forecast["probability_up"] <= 1.0
    assert 0.0 <= forecast["probability_down"] <= 1.0
    assert round(forecast["probability_up"] + forecast["probability_down"], 6) == 1.0
    assert len(forecast["confidence_interval"]) == 2
    assert "top_factors" in assessment["explainability"]


def test_alpha_validation_uses_held_out_information_coefficient() -> None:
    feature = [float(i) for i in range(60)]
    forward_returns = [value * 0.001 for value in feature]
    result = validate_series(feature, forward_returns)
    assert result["observations"] == 60
    assert result["test_information_coefficient"] > 0.9
    assert result["status"] == "candidate"