from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "local"
    database_url: str = ""
    public_base_url: str = "http://localhost:8000"
    trusted_hosts: list[str] = ["localhost", "127.0.0.1", "testserver"]
    # Enable only behind the controlled reverse proxy; it lets rate limiting
    # identify the browser instead of the proxy container.
    trusted_proxy_headers: bool = False
    subscription_enforcement_enabled: bool = True
    allow_public_signup: bool = True
    auth_jwt_secret: str = ""
    auth_access_token_minutes: int = 15
    auth_refresh_token_days: int = 14
    bootstrap_admin_emails: list[str] = []

    # The payment provider owns chain/address/confirmation processing. This
    # boundary keeps product entitlements independent of a particular chain.
    payment_provider: str = "disabled"
    nowpayments_api_key: str = ""
    nowpayments_ipn_secret: str = ""
    nowpayments_api_base_url: str = "https://api.nowpayments.io/v1"
    nowpayments_sandbox: bool = False
    billing_currency: str = "usd"
    enable_real_trading: bool = False
    manual_trading_only: bool = True

    ai_provider: str = "openrouter"
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "openrouter/free"
    openrouter_model_scanner: str = "openrouter/free"
    openrouter_model_judge: str = "openrouter/free"
    openrouter_http_referer: str = "http://localhost:8000"
    openrouter_app_title: str = "AI Trader Copilot"
    # Users can supply an OpenRouter key for their own research.  The key is
    # encrypted at rest with this secret (or AUTH_JWT_SECRET when omitted).
    user_secrets_encryption_key: str = ""
    # Web dashboard requests should be BYOK by default so a public deployment
    # cannot silently spend the operator's OpenRouter credits.
    allow_platform_ai_fallback: bool = False
    # Deterministic evidence may always produce a watch state, but a new
    # published signal requires a successful final model synthesis by default.
    require_ai_for_signal_publication: bool = True

    openai_api_key: str = ""
    openai_model_scanner: str = "gpt-5.4-mini"
    openai_model_judge: str = "gpt-5.5"

    puter_api_key: str = ""
    puter_model: str = "meta-llama/llama-3.1-70b-instruct"
    puter_model_scanner: str = "meta-llama/llama-3.1-8b-instruct"
    puter_model_judge: str = "meta-llama/llama-3.1-70b-instruct"

    gemini_api_key: str = ""
    gemini_model_scanner: str = "gemini-2.5-flash-lite"
    gemini_model_judge: str = "gemini-2.5-flash-lite"

    qwen_api_key: str = ""
    qwen_base_url: str = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    qwen_model_scanner: str = "qwen3.6-plus"
    qwen_model_judge: str = "qwen3.6-plus"

    ai_max_calls_per_analysis: int = 1
    ai_monthly_budget_usd: float = 10.0
    # Reservations are deliberately conservative: a platform analysis reserves
    # its maximum allowed call count before dispatching to the provider.
    ai_estimated_cost_per_call_usd: float = 0.01

    binance_public_base_url: str = "https://api.binance.com"
    binance_futures_base_url: str = "https://fapi.binance.com"
    binance_stream_base_url: str = "wss://stream.binance.com:9443"
    gdelt_doc_api: str = "https://api.gdeltproject.org/api/v2/doc/doc"

    # Keep live research responsive without allowing every browser tab to fan
    # out into a complete independent market-data request.  These defaults are
    # deliberately conservative for small Docker hosts and can be tuned per
    # deployment through environment variables.
    market_snapshot_cache_seconds: float = 8.0
    market_snapshot_cache_max_entries: int = 96
    market_intelligence_max_concurrency: int = 8
    # Research tabs share one Binance connection per symbol/timeframe.  These
    # bounds protect small hosts from unbounded pairs and CPU-heavy analyses.
    analysis_stream_max_pairs: int = 32
    analysis_stream_idle_seconds: float = 30.0
    analysis_compute_max_concurrency: int = 4
    analysis_compute_wait_seconds: float = 20.0
    analysis_websocket_config_timeout_seconds: float = 10.0
    # One bounded process-wide execution tape. These public feeds use no API
    # key and are shared by every dashboard, scanner, and analysis request.
    multi_venue_ws_enabled: bool = True
    binance_spot_public_ws_url: str = "wss://stream.binance.com:9443/stream"
    binance_perp_public_ws_url: str = "wss://fstream.binance.com/stream"
    bybit_spot_public_ws_url: str = "wss://stream.bybit.com/v5/public/spot"
    bybit_perp_public_ws_url: str = "wss://stream.bybit.com/v5/public/linear"
    # Startup warm set only. Any valid USDT market selected in Radar/Research
    # is registered automatically and replaces the least-recently-used symbol.
    multi_venue_symbols: list[str] = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    # The bounded dynamic hub limits this to 12 even if configuration drifts.
    multi_venue_max_symbols: int = 12
    # Do not evict a symbol that an active Research/Radar consumer requested
    # recently; report the new pair unavailable instead of feed-churning.
    multi_venue_symbol_idle_seconds: float = 30.0
    multi_venue_book_levels: int = 200
    multi_venue_min_book_levels: int = 5
    multi_venue_max_events: int = 2_000
    multi_venue_stale_seconds: float = 30.0
    multi_venue_trade_window_seconds: float = 60.0
    multi_venue_liquidation_window_seconds: float = 300.0
    multi_venue_flow_warmup_seconds: float = 10.0
    multi_venue_min_flow_trades: int = 20
    multi_venue_min_flow_notional_usd: float = 5_000.0
    execution_tape_large_trade_notional_usd: float = 10_000.0
    multi_venue_max_event_lag_seconds: float = 10.0
    multi_venue_subscription_retry_seconds: float = 900.0

    background_jobs_enabled: bool = True
    # PostgreSQL advisory/row leases prevent duplicate writers if more than
    # one serving process starts background loops accidentally.
    max_request_body_bytes: int = 1_000_000
    background_idle_check_seconds: int = 60

    # Shared Radar is market-wide rather than user-specific. These freshness
    # targets keep popular short timeframes warm while slow pairs refresh only
    # after someone has actually requested them.
    radar_refresh_lease_seconds: int = 180
    radar_demand_window_seconds: int = 180
    radar_warm_check_seconds: int = 15
    radar_fresh_5m_1h_seconds: int = 30
    radar_fresh_15m_4h_seconds: int = 120
    radar_fresh_1h_1d_seconds: int = 600
    # A stale-while-refreshing snapshot is useful briefly, but must never turn
    # into apparently live multi-hour/day-old market research.
    radar_max_stale_multiplier: int = 6

    default_account_size_usd: float = 1000.0
    default_risk_per_idea_pct: float = 0.5
    min_risk_reward: float = 1.5
    max_drawdown_pct: float = 12.0
    max_gross_exposure_pct: float = 100.0

    # Institutional allocation controls.  ``portfolio_current_drawdown_pct``
    # must come from account equity (or an operator-provided value); the system
    # will WAIT rather than assume that drawdown is zero.
    # Balanced manual-review defaults. Strict capital-allocation mode can turn
    # the two ``require`` flags back on without changing committee code.
    institutional_require_validated_model: bool = False
    institutional_min_forecast_confidence: float = 0.15
    institutional_require_portfolio_state: bool = False
    institutional_unvalidated_risk_cap_pct: float = 0.25
    institutional_missing_portfolio_risk_cap_pct: float = 0.25
    institutional_min_directional_engines: int = 2
    institutional_max_leverage: int = 5
    institutional_expected_payoff_r: float = 2.5
    institutional_fee_bps_per_side: float = 3.0
    institutional_slippage_bps_per_side: float = 0.5
    # A stop inside (or almost inside) an entry zone makes a setup impossible
    # to execute safely. This floor applies before a plan can be published.
    institutional_min_stop_distance_bps: float = 25.0
    portfolio_current_drawdown_pct: float | None = None

    # CoinDesk WebSockets settings
    coindesk_api_key: str = ""
    coindesk_stream_base_url: str = "wss://data-streamer.coindesk.com"
    use_coindesk_ws: bool = False

    # Autonomous Scanner Settings
    watchlist: list[str] = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    scan_interval_seconds: int = 300
    autonomous_scan_enabled: bool = False
    # New deployments expand beyond the fixed three-pair watchlist with a
    # small, liquid discovery set. Existing database configuration remains
    # operator-controlled and is never silently changed by this default.
    autonomous_pair_discovery: bool = True




@lru_cache
def get_settings() -> Settings:
    return Settings()
