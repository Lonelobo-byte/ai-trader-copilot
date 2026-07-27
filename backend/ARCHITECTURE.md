# Institutional Committee Architecture

## Decision path

```text
MarketIntelligence snapshot
  -> deterministic feature and probability calculations
  -> Quant Research Engine
  -> Market Microstructure Engine
  -> Derivatives Engine
  -> Macro Intelligence Engine
  -> Adversarial Review Engine
  -> Risk Committee
  -> optional CIO narrative synthesis
  -> code-enforced CIO policy
  -> investment memorandum or WAIT
```

All engines consume the same snapshot. Each engine reports evidence, source,
availability, contradictions, unknowns, limitations, bias, and evidence
confidence. `UNAVAILABLE` is different from `NEUTRAL`.

## Causal market-context score

The directional thesis is owned by `causal_market_context_v1`. It scores only
observable market context: regime/structure, candle-derived liquidity pools
and sweeps, price × open-interest positioning, funding crowding, aggregated
taker flow and displayed depth, realized-volatility state, candle-volume
profile, daily/weekly/swing-anchored VWAP, and available cross-market context.
At least four evidence domains must be available before it can nominate a
setup. Any explicitly opposing domain is reported as a contradiction; missing
domains are never silently interpreted as neutral.

RSI, MACD, Bollinger values, and moving averages remain dashboard/research
telemetry only. They do not enter the live market-context score, the
production probability baseline, or the non-AI fallback. Historical ML
artifacts are also excluded from production scoring until retrained and
validated on timestamped causal features.

## Authority boundaries

- Data adapters may report observations and failures. They cannot recommend an
  allocation.
- Specialist engines transform observations into bounded research opinions.
  They cannot publish a trade.
- Adversarial Review attempts to falsify the provisional thesis and can veto a
  proposal at high severity.
- The Risk Committee owns expected-value, model-validation, portfolio
  drawdown, gross exposure, macro blockout, and allocation ceilings. Its veto
  is final.
- The CIO can choose `WAIT`, reduce confidence, or synthesize an eligible
  proposal. The CIO cannot override a veto or introduce evidence absent from
  the dossier.
- The signal lifecycle remains manual-review only and never submits an order.

## Decision tiers

- `CAPITAL_ELIGIBLE` requires a validated out-of-sample model and current
  portfolio drawdown input.
- `CONDITIONAL_MANUAL_REVIEW` may be produced by the default research/manual
  workflow when those two inputs are missing, but risk is capped at 0.25%,
  leverage remains capped, and every limitation stays visible.
- `WAIT` remains mandatory for negative EV, incomplete core data, macro
  blockouts, hard exposure/drawdown breaches, or an adversarial veto.

## Hard prerequisites

Default policy requires:

1. Complete core candles, ticker, and order-book data.
2. Positive expected value.
3. Forecast confidence at or above the configured tier minimum.
4. Known exposure below hard limits; strict mode can also require current
   portfolio drawdown input.
5. No macro blockout.
6. No Adversarial Review veto.
7. A valid entry, invalidation, stop, targets, and minimum risk/reward plan.

If any hard prerequisite fails, the decision is `WAIT`. This is intentional.

## Current evidence coverage

- Quant: statistical distribution, regime, probability, EV, confidence
  interval, walk-forward model adapter, backtests, and similarity memory.
- Microstructure: snapshot depth, spread, imbalance, aggregated taker flow, and
  an explicitly labelled absorption proxy.
- Derivatives: Binance perpetual funding, OI, OI history, long/short ratios,
  top-trader positioning, and taker CVD.
- Macro: DXY, Nasdaq, gold, US 10Y yield, global liquidity, and economic
  calendar.
- Options: provider contract is represented, but the current snapshot is
  `UNAVAILABLE` until an IV/skew/term-structure/gamma provider is connected.

On-chain intelligence is intentionally excluded from the active decision path;
it does not affect voting, coverage, adversarial severity, or signal approval.

## Configuration

```dotenv
INSTITUTIONAL_REQUIRE_VALIDATED_MODEL=false
INSTITUTIONAL_MIN_FORECAST_CONFIDENCE=0.15
INSTITUTIONAL_REQUIRE_PORTFOLIO_STATE=false
INSTITUTIONAL_UNVALIDATED_RISK_CAP_PCT=0.25
INSTITUTIONAL_MISSING_PORTFOLIO_RISK_CAP_PCT=0.25
INSTITUTIONAL_MIN_DIRECTIONAL_ENGINES=2
INSTITUTIONAL_EXPECTED_PAYOFF_R=2.5
INSTITUTIONAL_FEE_BPS_PER_SIDE=3.0
INSTITUTIONAL_SLIPPAGE_BPS_PER_SIDE=0.5
PORTFOLIO_CURRENT_DRAWDOWN_PCT=
```

`PORTFOLIO_CURRENT_DRAWDOWN_PCT` should come from account equity or an
operator-controlled source. Leaving it blank restricts the default workflow to
conditional, reduced-size manual review; strict mode keeps allocation in
`WAIT`.
