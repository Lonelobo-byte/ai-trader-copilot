from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.data_sources.binance_public import Candle
from app.brains.signal_builder import build_ai_driven_trade_setup, evaluate_ai_driven_approval
from app.institutional.committee import (
    apply_cio_policy,
    build_deterministic_cio_decision,
    build_institutional_dossier,
    build_investment_memo,
)
from app.settings import Settings


def _settings(**overrides):
    values = {
        "institutional_require_validated_model": True,
        "institutional_min_forecast_confidence": 0.50,
        "institutional_require_portfolio_state": True,
        "max_drawdown_pct": 12.0,
        "max_gross_exposure_pct": 100.0,
        "default_account_size_usd": 1000.0,
        "default_risk_per_idea_pct": 0.5,
        "institutional_unvalidated_risk_cap_pct": 0.25,
        "institutional_missing_portfolio_risk_cap_pct": 0.25,
        "institutional_min_directional_engines": 2,
        "institutional_max_leverage": 5,
        "institutional_min_stop_distance_bps": 25.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _features() -> dict:
    return {
        "data_quality": {"passed": True, "missing_required": []},
        "volatility": {"atr_pct": 0.5},
        "microstructure": {
            "available": True,
            "signed_trade_flow": 0.30,
            "depth_imbalance": 0.25,
            "spread_bps": 1.2,
            "liquidity_quality": "normal",
            "limitations": ["snapshot only"],
        },
        "trade_flow": {"buy_ratio": 0.64, "whale_activity": {"whale_bias": "WHALE_BUYING"}},
        "derivatives": {
            "funding_rate": -0.0002,
            "open_interest": 1_000_000,
            "oi_delta": {"oi_change_pct": 2.0},
            "taker_volume": {"cvd_trend": "CVD_BULLISH_ACCUMULATION"},
            "top_traders": {"bias": "SMART_MONEY_LONG"},
        },
        "market_structure": {
            "phase": "MARKUP",
            "bos": {"detected": True, "direction": "bullish"},
            "choch": {"detected": False, "direction": "none"},
            "sweep": {"detected": False},
            "order_blocks": [],
            "fair_value_gaps": [],
        },
        "cross_asset": {
            "risk_environment": "NEUTRAL",
            "dxy": {"change_pct": 0.0},
            "nasdaq": {"change_pct": 0.1},
            "yields_10y": {"change_pct": 0.0},
        },
    }


def _intelligence() -> dict:
    return {
        "meta": {
            "sources_available": ["candles", "ticker", "order_book", "funding", "open_interest", "derivatives", "macro"],
            "sources_failed": [],
        },
        "global_liquidity": {"liquidity_status": "LIQUIDITY_NEUTRAL"},
    }


def _quantitative(*, validated: bool = True, expected_value: float = 0.002) -> dict:
    return {
        "statistical_features": {"available": True},
        "probability_engine": {
            "model": "walk_forward_ridge" if validated else "baseline_interpretable_probability_model",
            "model_status": "validated out-of-sample" if validated else "research baseline",
            "probability_up": 0.63,
            "probability_down": 0.37,
            "confidence": 0.80 if validated else 0.20,
            "expected_return": 0.004,
            "expected_risk": 0.003,
            "expected_value": expected_value,
            "confidence_interval": [-0.002, 0.01],
        },
        "microstructure": _features()["microstructure"],
        "market_state": {"state": "trending"},
        "risk_engine": {
            "blockers": [] if expected_value > 0 else ["Expected value is non-positive after estimated risk."],
            "volatility_adjusted_risk_fraction": 0.005,
            "illustrative_max_notional_usd": 500.0,
        },
    }


def _portfolio() -> dict:
    return {
        "available": True,
        "source": "test_account_equity",
        "current_drawdown_pct": 2.0,
        "gross_exposure_pct": 15.0,
    }


def _dossier(*, validated: bool = True, expected_value: float = 0.002, portfolio=None):
    return build_institutional_dossier(
        symbol="BTCUSDT",
        timeframe="15m",
        features=_features(),
        intelligence=_intelligence(),
        quantitative=_quantitative(validated=validated, expected_value=expected_value),
        portfolio_state=_portfolio() if portfolio is None else portfolio,
        macro_blockout={"active": False, "reason": ""},
        settings=_settings(),
    )


def test_on_chain_engine_is_not_part_of_decision_architecture() -> None:
    dossier = _dossier()
    assert "on_chain_intelligence_engine" not in dossier["engines"]
    assert dossier["engines"]["market_structure_engine"]["status"] == "COMPLETE"


def test_cross_venue_evidence_reaches_cio_dossier_and_memorandum() -> None:
    features = _features()
    cross_venue = {
        "available": True,
        "status": "HEALTHY",
        "fresh_venue_count": 2,
        "flow_venue_count": 2,
        "flow_confirmed": True,
        "flow_consensus": "BULLISH",
        "flow_score": 0.42,
        "depth_score": 0.18,
        "price_dispersion_bps": 1.5,
        "venues": {
            "bybit": {
                "health": "HEALTHY",
                "aggressive_buy_ratio": 0.68,
                "persistent_imbalance": 0.21,
                "removal_ratio": 0.31,
            },
            "coinbase": {
                "health": "HEALTHY",
                "aggressive_buy_ratio": 0.63,
                "persistent_imbalance": 0.12,
                "removal_ratio": 0.28,
            },
        },
        "limitations": ["Displayed public liquidity only."],
    }
    features["microstructure"]["incremental_public_feeds"] = cross_venue
    quantitative = _quantitative()
    quantitative["microstructure"] = features["microstructure"]
    dossier = build_institutional_dossier(
        symbol="BTCUSDT",
        timeframe="15m",
        features=features,
        intelligence=_intelligence(),
        quantitative=quantitative,
        portfolio_state=_portfolio(),
        macro_blockout={"active": False, "reason": ""},
        settings=_settings(),
    )

    engine = dossier["engines"]["market_microstructure_engine"]
    evidence = {item["metric"]: item["value"] for item in engine["evidence"]}
    assert engine["status"] == "COMPLETE"
    assert evidence["cross_venue_flow_consensus"] == "BULLISH"
    assert evidence["bybit_aggressive_buy_ratio"] == 0.68
    assert evidence["coinbase_aggressive_buy_ratio"] == 0.63

    memo = build_investment_memo({"decision": "WAIT"}, dossier)
    assert any("cross_venue_flow_consensus=BULLISH" in item for item in memo["supporting_evidence"])


def test_trade_plan_keeps_entry_zone_outside_stop() -> None:
    result = {
        "decision": "BUY_WATCH",
        "confidence_pct": 72,
        "trade_grade": "A",
        "institutional_dossier": {
            "risk_committee": {"allocation_ceiling": {"risk_fraction": 0.005, "max_notional_usd": 500.0}}
        },
    }
    setup = build_ai_driven_trade_setup(
        result,
        {"current_price": 100.0, "volatility": {"atr": 0.01}},
        _settings(),
    )
    assert setup["stop"]["selected"] < setup["entry"]["zone_low"]
    assert setup["stop"]["distance_pct"] >= 0.25


def test_directional_causal_context_gets_a_zero_risk_value_retest_watch() -> None:
    setup = build_ai_driven_trade_setup(
        {
            "decision": "WAIT",
            "confidence_pct": 54,
            "trade_grade": "C",
            "institutional_dossier": {
                "provisional_thesis": {"direction": "LONG"},
                "risk_committee": {
                    "hard_blockers": ["Live confirmation is still required."],
                    "allocation_ceiling": {"risk_fraction": 0.005, "max_notional_usd": 500.0},
                },
            },
        },
        {
            "current_price": 100.0,
            "volatility": {"atr": 1.0},
            "vwap_context": {"daily": 99.0, "weekly": 97.0, "anchored": 98.0},
            "volume_profile": {"poc": 98.0},
            "liquidity_map": {
                "nearest_below": {"kind": "equal_lows", "price": 96.5},
                "nearest_above": {"kind": "previous_day_high", "price": 104.0},
            },
        },
        _settings(),
    )
    assert setup["status"] == "WATCH_ONLY"
    assert setup["setup_type"] == "causal_value_retest_watch"
    assert setup["entry"]["reference"] == 99.0
    assert setup["stop"]["selected"] < 96.5
    assert setup["position"]["risk_amount_usd"] == 0.0
    assert setup["execution_permitted"] is False


def test_risk_veto_cannot_be_overridden_by_bullish_cio_output() -> None:
    dossier = _dossier(
        validated=False,
        portfolio={"available": False, "current_drawdown_pct": None, "gross_exposure_pct": 0.0},
    )
    result = apply_cio_policy(
        {
            "decision": "BUY_WATCH",
            "confidence_pct": 96,
            "trade_grade": "A+",
            "explanation": "Bullish proposal.",
            "suggested_entry": 100.0,
            "suggested_stop": 98.0,
            "suggested_targets": [103.0, 106.0, 109.0],
        },
        dossier,
    )
    assert dossier["risk_committee"]["approved_for_allocation"] is False
    assert result["decision"] == "WAIT"
    assert result["confidence_pct"] <= 55
    assert result["suggested_entry"] is None
    assert result["committee_controls"]["llm_override_permitted"] is False


def test_validated_positive_edge_can_reach_cio_review_without_removing_unknowns() -> None:
    dossier = _dossier()
    assert dossier["risk_committee"]["approved_for_allocation"] is True
    result = apply_cio_policy(
        {
            "decision": "BUY_WATCH",
            "confidence_pct": 78,
            "trade_grade": "B",
            "explanation": "Positive measured edge with aligned flow.",
            "suggested_entry": 100.0,
            "suggested_stop": 98.0,
            "suggested_targets": [103.0, 106.0, 109.0],
            "risk_warnings": [],
        },
        dossier,
    )
    assert result["decision"] == "BUY_WATCH"
    assert result["investment_memo"]["unknown_variables"]
    assert "## Contradictory Evidence" in result["report_md"]
    assert "## Reasons Confidence Was Reduced" in result["report_md"]


def test_balanced_policy_releases_reduced_size_conditional_manual_review() -> None:
    settings = _settings(
        institutional_require_validated_model=False,
        institutional_min_forecast_confidence=0.15,
        institutional_require_portfolio_state=False,
    )
    dossier = build_institutional_dossier(
        symbol="BTCUSDT",
        timeframe="15m",
        features=_features(),
        intelligence=_intelligence(),
        quantitative=_quantitative(validated=False, expected_value=0.002),
        portfolio_state={"available": False, "current_drawdown_pct": None, "gross_exposure_pct": 0.0},
        macro_blockout={"active": False, "reason": ""},
        settings=settings,
    )
    assert dossier["risk_committee"]["approved_for_allocation"] is True
    assert dossier["risk_committee"]["allocation_tier"] == "CONDITIONAL_MANUAL_REVIEW"
    assert dossier["risk_committee"]["allocation_ceiling"]["risk_fraction"] <= 0.0025

    deterministic_cio = build_deterministic_cio_decision(
        dossier, narrative_error="judge model unavailable",
    )
    assert deterministic_cio["decision"] == "BUY_WATCH"
    assert deterministic_cio["confidence_pct"] >= 60

    result = apply_cio_policy(
        {
            "decision": "BUY_WATCH",
            "confidence_pct": 62,
            "trade_grade": "B",
            "explanation": "Conditional positive edge.",
            "risk_warnings": [],
        },
        dossier,
    )
    result.update({
        "symbol": "BTCUSDT",
        "timeframe": "15m",
        "agent_agreement": {"bullish": 2, "bearish": 0, "neutral": 3},
        "data_quality": {"passed": True},
        "macro_blockout": {"active": False},
    })
    setup = build_ai_driven_trade_setup(
        result,
        {"current_price": 100.0, "volatility": {"atr": 1.0}},
        settings,
    )
    approval = evaluate_ai_driven_approval(result, setup)
    assert setup["status"] == "CONDITIONAL_MANUAL_REVIEW"
    assert setup["position"]["risk_pct"] <= 0.25
    assert approval["approved"] is True


def test_live_confirmation_failure_vetoes_an_otherwise_approved_trade() -> None:
    result = {
        "decision": "BUY_WATCH", "confidence_pct": 85, "trade_grade": "A",
        "data_quality": {"passed": True}, "macro_blockout": {"active": False},
        "institutional_dossier": {
            "risk_committee": {"approved_for_allocation": True, "minimum_directional_engines": 1},
            "adversarial_review": {"veto": False},
        },
        "agent_agreement": {"bullish": 3, "bearish": 0},
        "live_confirmation": {"passed": False, "reason": "Displayed order-book depth opposes the long."},
    }
    approval = evaluate_ai_driven_approval(
        result,
        {"status": "READY_FOR_MANUAL_REVIEW", "position": {"risk_amount_usd": 5, "units": 1}},
    )
    assert approval["approved"] is False
    assert any("Live confirmation failed" in blocker for blocker in approval["blockers"])


@pytest.mark.anyio
async def test_council_exposes_real_engine_reports_and_enforces_wait(monkeypatch) -> None:
    import app.brains.council as council

    candles = []
    for i in range(80):
        price = 100.0 + i * 0.05
        candles.append(Candle(
            open_time=i * 60_000, open=price - 0.03, high=price + 0.10,
            low=price - 0.10, close=price, volume=1000.0 + i,
            close_time=i * 60_000 + 59_999, quote_volume=price * (1000.0 + i),
            trade_count=100, taker_buy_base_volume=550.0,
            taker_buy_quote_volume=price * 550.0,
        ))
    available = ["candles", "ticker", "order_book", "funding", "open_interest", "macro"]
    intelligence = {
        "symbol": "BTCUSDT",
        "timeframe": "15m",
        "candles": candles,
        "ticker": {"lastPrice": "104", "bidPrice": "103.99", "askPrice": "104.01", "quoteVolume": "1000000"},
        "order_book": {
            "bids": [[104.0 - i * 0.01, 20.0] for i in range(1, 21)],
            "asks": [[104.0 + i * 0.01, 12.0] for i in range(1, 21)],
        },
        "recent_trades": [],
        "multi_tf_candles": {},
        "funding": {"funding_rate": -0.0001},
        "open_interest": {"open_interest": 1_000_000},
        "derivatives": {},
        "news": [],
        "global_news": [],
        "macro": {},
        "sentiment": {"fear_greed": {"value": 50, "available": False}},
        "calendar": [],
        "global_liquidity": {"available": False, "liquidity_status": "LIQUIDITY_NEUTRAL"},
        "options": {"available": False},
        "meta": {
            "sources_available": available,
            "sources_failed": ["recent_trades", "options"],
            "total_sources": len(available) + 2,
            "fetch_time_ms": 1.0,
        },
    }

    async def fake_cio(*args, **kwargs):
        return {
            "decision": "BUY_WATCH",
            "confidence_pct": 99,
            "trade_grade": "A+",
            "explanation": "Aggressive proposal that must be constrained.",
            "suggested_entry": 104.0,
            "suggested_stop": 102.0,
            "suggested_targets": [107.0, 110.0, 113.0],
        }

    async def fake_history(*args, **kwargs):
        return {"similar_setups_count": 0, "historical_win_rate": 50.0}

    async def fake_portfolio(*args, **kwargs):
        return {"available": False, "current_drawdown_pct": None, "gross_exposure_pct": 0.0}

    monkeypatch.setattr(council, "_run_institutional_cio", fake_cio)
    monkeypatch.setattr(council, "_fetch_historical_stats", fake_history)
    monkeypatch.setattr(council, "load_portfolio_state", fake_portfolio)

    result = await council.run_ai_council(
        "BTCUSDT", "15m", Settings(
            institutional_require_validated_model=True,
            institutional_min_forecast_confidence=0.50,
            institutional_require_portfolio_state=True,
        ), intelligence=intelligence,
    )
    assert result["decision"] == "WAIT"
    assert result["meta"]["engine"] == "institutional_committee_v1"
    assert "quant_research_engine" in result["agent_reports"]
    assert "risk_committee" in result["agent_reports"]
    assert result["committee_controls"]["llm_override_permitted"] is False
