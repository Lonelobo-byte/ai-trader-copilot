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
    background_jobs_enabled: bool = True
    background_idle_check_seconds: int = 60

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
    autonomous_pair_discovery: bool = False




@lru_cache
def get_settings() -> Settings:
    return Settings()
