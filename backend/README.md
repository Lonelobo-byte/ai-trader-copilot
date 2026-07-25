# Institutional Crypto Market Intelligence System

The primary decision path now uses the evidence-driven institutional committee
described in [ARCHITECTURE.md](ARCHITECTURE.md). Specialist analysis and vetoes
are deterministic; the optional CIO model is limited to memorandum synthesis.

A research-first market-analysis platform for discovering, validating, and monitoring statistical trading edges. It does **not** produce deterministic BUY/SELL commands and never submits exchange orders.

## What it evaluates

- Market microstructure: top-of-book spread, depth imbalance, near-touch liquidity, aggregated taker flow, and an explicitly labelled absorption proxy.
- Statistical features: log-return distributions, rolling volatility and percentile, Z-score, skew, kurtosis, entropy, autocorrelation, and an ADF-style stationarity diagnostic.
- Market state: probabilistic classification across trend, mean-reversion, volatility, breakout/compression/expansion, panic, and euphoria states.
- Probability engine: probability up/down, confidence interval, expected return, expected risk, expected value, and calibration uncertainty.
- Risk engine: fractional Kelly ceiling, volatility-adjusted notional constraint, drawdown/exposure limits, and volatility-based invalidation distance.
- Explainability: ranked feature contributions, market state, risk factors, and a concise statistical justification.
- Robustness reporting: backtest results are segmented into bull, bear, sideways, panic, and low-liquidity regimes.
- Benchmark reporting: EMA20/EMA50, 10-bar momentum, 20-bar breakout, and a transparent ensemble proxy are compared with cost assumptions.

Technical indicators remain available in the codebase only as optional derived features; they are not used by the public research-assessment path.

The signal path and research path now share a single market-intelligence snapshot on REST analysis. The snapshot records available and failed sources, and required core data (candles, ticker, and order book) must be present before a new signal can be approved. AI output is an explanation and decision layer over quantitative inputs; it is not evidence of predictive accuracy by itself.

## Alpha Research Engine

The platform exposes a hypothesis registry and a minimum viable validation primitive:

- `GET /research/alpha/hypotheses` lists research hypotheses and their required raw data.
- `POST /research/alpha/validate` accepts aligned `feature` and `future_returns` series and reports train/test information coefficients, held-out t-statistic, and edge-stability ratio.

This is intentionally conservative. A candidate must subsequently pass purged walk-forward validation, realistic transaction-cost and capacity analysis, cross-instrument/regime checks, and multiple-testing controls before it is registered for allocation.

## Main API

`POST /analyze` returns a `quantitative` object with independent `microstructure`, `statistical_features`, `market_state`, `probability_engine`, `risk_engine`, and `explainability` modules. The response is marked `RESEARCH_ONLY`.

Run locally from `backend`:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/dashboard` for the live research dashboard.

Install development dependencies and run validation:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

The project is not production-capital-ready until a sufficiently large, unseen, cost-aware walk-forward study demonstrates positive expectancy and acceptable drawdown against the included baselines.

Backtest `statistical_validation` reports bootstrap confidence intervals and matched trade-window differences. Its status remains `not_proven` until at least 30 trades and positive lower confidence bounds against every selected baseline are observed. The `institutional_quant_proxy` is an interpretable reference ensemble, not a claim to reproduce a proprietary institutional strategy.
