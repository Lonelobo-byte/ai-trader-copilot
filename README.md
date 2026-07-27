# Institutional Crypto Market Intelligence System

Evidence-driven market research and manual-review software. It never sends an exchange order.

## Subscription SaaS deployment

For the complete AWS EC2 / Contabo VPS guide, see [DEPLOYMENT.md](DEPLOYMENT.md).

The application now has server-enforced account and subscription access. A
browser cannot unlock premium endpoints by changing local storage: every
premium REST call and the analysis WebSocket verify the current subscription in
the database. The simple production target is a single VPS running Docker
Compose: Caddy provides HTTPS, FastAPI runs the product, and PostgreSQL stores
accounts, subscriptions, payments, refresh-token hashes, and audit events.

1. Point a domain's DNS A record at the VPS and install Docker plus Compose.
2. Copy `.env.example` to `.env`, set `APP_ENV=production`, then set `DOMAIN`, `POSTGRES_PASSWORD`,
   `PUBLIC_BASE_URL=https://your-domain`, `TRUSTED_HOSTS=["your-domain"]`, and a
   persistent `AUTH_JWT_SECRET`.
3. Add the same provider callback URL to NOWPayments, set
   `PAYMENT_PROVIDER=nowpayments`, `NOWPAYMENTS_API_KEY`, and
   `NOWPAYMENTS_IPN_SECRET`, then run `docker compose up -d --build`.

NOWPayments creates a hosted crypto invoice and sends signed IPNs to
`/billing/webhooks/nowpayments`. The app then fetches the provider payment ID
server-to-server before activating a plan; only verified `finished` or
`confirmed` states grant access. The redirect back to `/dashboard` is only a
customer-experience step and never grants access on its own. The provider
handles chain addresses and confirmations, which keeps the app ready for BTC,
ETH, stablecoins, and additional chains without holding customer private keys.
Start with monthly, quarterly, half-yearly, and annual plans in
`backend/app/billing.py`; prices are deliberately centralized.

Before taking real payments, use the provider sandbox, verify its callback
signature configuration, add a legal refund policy, and make a tested encrypted
PostgreSQL backup. Crypto payments can have tax, consumer-protection, and local
regulatory implications—get jurisdiction-specific advice before launch.

### NOWPayments test mode

Create a separate account at `account-sandbox.nowpayments.io`, then use only
its API key and IPN secret in your local `.env`:

```env
PAYMENT_PROVIDER=nowpayments
NOWPAYMENTS_API_BASE_URL=https://api-sandbox.nowpayments.io/v1
NOWPAYMENTS_SANDBOX=true
NOWPAYMENTS_API_KEY=your-sandbox-key
NOWPAYMENTS_IPN_SECRET=your-sandbox-ipn-secret
```

Use the hosted sandbox invoice to exercise checkout. For automatic
subscription activation and the return-to-dashboard redirect, use a staging
HTTPS URL instead of `localhost`, because the provider must be able to reach
the webhook. The app intentionally blocks checkout when `PUBLIC_BASE_URL` is
not public HTTPS so a customer cannot pay into an unverifiable local setup.

When `APP_ENV=local` and `NOWPAYMENTS_SANDBOX=true`, local checkout is allowed
solely to test invoice creation and the hosted payment screen. A localhost
server cannot receive the signed provider IPN, so the subscription will remain
pending; use a temporary HTTPS tunnel or staging URL to test automatic
activation. The public-HTTPS requirement remains mandatory for production or
non-sandbox payments.
Return all values to the production defaults before launch.

## Institutional committee architecture

The allocation path is no longer a set of AI personas. A single evidence
snapshot is transformed by independent deterministic engines for Quant
Research, Market Microstructure, Derivatives, and Macro Intelligence. Missing
evidence is reported as unavailable rather than inferred.

Adversarial Review attempts to falsify the provisional thesis. The Risk
Committee then applies expected-value, model-validation, data-quality,
drawdown, exposure, macro, and adversarial controls. These vetoes are enforced
in code and cannot be overridden by the CIO language model. The optional CIO
model is limited to synthesizing the dossier and writing the investment memo.

The default manual-review policy can publish a `CONDITIONAL_MANUAL_REVIEW`
idea without a validated model or live portfolio drawdown, but risk is capped
at 0.25% and the missing coverage remains visible. Strict capital-allocation
mode can require both inputs and will return `WAIT` until they exist. Options
gamma/dealer positioning remains an explicit coverage gap until a dedicated
feed is connected. On-chain intelligence is intentionally outside the active
decision architecture.

An idea is published for manual review only when the unified market snapshot has the required core data, liquidity is healthy, the setup passes risk/reward checks, and the deterministic Risk Committee and Adversarial Review controls approve it. The system remains research/manual-execution software; it does not guarantee profitability.

REST analysis now uses one shared snapshot for candles, higher timeframes, order book, trades, derivatives, macro, sentiment, news, and calendar context. The response includes `data_quality` so missing core sources are visible and can block signal publication.

The Breakout Radar is an opportunity-triage screen, not a signal generator. It
first requires a completed close through a 20-candle structural level with
multi-timeframe alignment, participation, and a decisive candle. Only the
strongest structures then receive a bounded live check of 20-level order-book
depth, spread, taker buy/sell flow, open-interest change, and funding crowding.
A bounded SMC confluence layer also reports supply/demand order blocks,
unmitigated FVGs, break/change of structure, liquidity-sweep (stop-hunt)
patterns, and the operating phase (accumulation, markup, distribution,
markdown, or ranging). These are soft ranking inputs—not mandatory gates or
claims of institutional intent—so their absence cannot make the Radar silent.
A disagreement in any required live check is a hard veto (`WATCH_ONLY`), while
`REVIEW_CANDIDATE` means the evidence is aligned for manual review—not that a
trade is guaranteed to win. Its evidence score is not a forecasted win
probability, and an `Open Full Review` is required before a trade can be
considered.

Backtests include configurable fees and slippage, profit factor, drawdown, total costs, and transparent buy-and-hold / EMA20-EMA50 baseline comparisons. A positive backtest is not sufficient for live capital: validate across instruments, regimes, and unseen time periods first.

Published signals are persisted and monitored through a fixed lifecycle:

```text
SCANNING -> PENDING_ENTRY -> ACTIVE -> TP1_SECURED -> TP2_SECURED -> TP3_SECURED -> COMPLETED
                                  |                    |
                                  +-> STOPPED_OUT      +-> INVALIDATED / protected exit
```

The monitor runs from the live WebSocket and every five seconds in the background. A published setup remains `PENDING_ENTRY` until it receives a fresh leave-and-retest of its entry zone; it is not an immediate market entry. It sends explicit `WAIT_FOR_ENTRY`, `HOLD_POSITION`, `PROTECT_PROFIT`, `TAKE_PROFIT_COMPLETE`, or `EXIT_TRADE` commands. Stops are never widened, are kept outside the entry zone, and an adverse reversal exits the signal before TP1.
