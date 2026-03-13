"""Configuration loading and validation."""

import os
from dataclasses import dataclass, field

import yaml


@dataclass
class BotConfig:
    """Typed configuration for the arbitrage bot."""

    # Trading parameters
    price_threshold: float = 0.98
    max_position_per_market: float = 50.0
    max_total_deployed: float = 400.0
    max_open_positions: int = 5
    min_liquidity_usdc: float = 20.0
    capital_floor: float = 100.0

    # Market filtering (blocklist — block risky categories, allow everything else)
    blocked_categories: list = field(default_factory=lambda: ["Sports", "Esports", "Entertainment"])
    surprise_price_cutoff: float = 0.50
    high_confidence_threshold: float = 0.95

    # Order execution
    order_type: str = "GTC"
    order_ttl_seconds: int = 300
    tick_size: str = "0.01"
    min_net_profit: float = 0.30
    min_position_size: float = 20.0
    estimated_gas_cost_usd: float = 0.05
    platform_fee_rate: float = 0.0
    crypto_short_term_fee_rate: float = 0.0156
    crypto_short_term_price_threshold: float = 0.98
    max_retries_on_failure: int = 1
    retry_delay_seconds: int = 2

    # Timing
    polling_interval_seconds: int = 30
    redeem_check_interval_seconds: int = 60
    ws_ping_interval_seconds: int = 10
    ws_reconnect_base_seconds: int = 5

    # Scanner / Watchlist
    watchlist_max_age_hours: int = 48
    end_date_grace_minutes: int = 30
    end_date_grace_by_category: dict = field(default_factory=lambda: {
        "Crypto": 5, "Economics/Finance": 5, "Science/Weather": 10, "Politics": 30,
    })
    price_history_window_hours: int = 2
    gamma_poll_max_pages: int = 5
    gamma_poll_limit: int = 100
    watchlist_scan_interval_seconds: int = 120
    watchlist_recheck_cooldown_seconds: int = 300
    watchlist_max_checks_per_cycle: int = 10

    # Rate limiting
    clob_rate_limit_per_min: int = 85
    gamma_rate_limit_per_min: int = 120

    # API endpoints
    clob_base_url: str = "https://clob.polymarket.com"
    gamma_base_url: str = "https://gamma-api.polymarket.com"
    ws_market_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    ws_user_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user"

    # Blockchain
    chain_id: int = 137
    polygon_rpc_url: str = "https://polygon-rpc.com"
    usdc_address: str = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    ctf_exchange_address: str = "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e"

    # Logging
    log_file: str = "bot.log"
    log_level: str = "INFO"

    # Safety
    dry_run: bool = True

    # Risk management
    blacklist_manual: list = field(default_factory=list)
    daily_loss_limit: float = 15.0
    consecutive_failure_limit: int = 3
    stats_log_interval_seconds: int = 600

    # Settlement tracker
    ct_framework_address: str = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
    rpc_poll_interval_seconds: int = 15
    rpc_poll_batch_size: int = 5
    settlement_timeout_hours: dict = field(default_factory=lambda: {
        "Crypto": 8, "Esports": 24, "default": 24,
    })
    price_movement_alert_threshold: float = 0.03
    esports_variance_cv_threshold: float = 1.5

    # Cut loss
    cut_loss_enabled: bool = True
    cut_loss_threshold: float = 0.75
    cut_loss_emergency_threshold: float = 0.50
    cut_loss_check_interval: int = 30
    cut_loss_min_hold_minutes: int = 5
    cut_loss_confirmations: int = 2
    cut_loss_min_bid_depth: float = 10.0
    cut_loss_order_ttl: int = 120
    cut_loss_overdue_threshold: float = 0.90

    # Dashboard
    dashboard_enabled: bool = False
    dashboard_refresh_seconds: int = 5

    # Secrets (from environment only)
    private_key: str = ""
    clob_api_key: str = ""
    clob_api_secret: str = ""
    clob_api_passphrase: str = ""
    polymarket_proxy_address: str = ""


def load_config(config_path: str = "config.yaml") -> BotConfig:
    """Load config from YAML file, merge with environment variables."""
    # Auto-load .env file if present (secrets stay out of config.yaml)
    from dotenv import load_dotenv
    load_dotenv()

    config_data = {}

    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            config_data = yaml.safe_load(f) or {}

    # Environment variable overrides
    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    if private_key:
        config_data["private_key"] = private_key

    rpc_url = os.environ.get("POLYGON_RPC_URL", "")
    if rpc_url:
        config_data["polygon_rpc_url"] = rpc_url

    for env_key, cfg_key in [
        ("CLOB_API_KEY", "clob_api_key"),
        ("CLOB_API_SECRET", "clob_api_secret"),
        ("CLOB_API_PASSPHRASE", "clob_api_passphrase"),
        ("POLYMARKET_PROXY_ADDRESS", "polymarket_proxy_address"),
    ]:
        val = os.environ.get(env_key, "")
        if val:
            config_data[cfg_key] = val

    # Build config, ignoring unknown keys from YAML
    valid_fields = {f.name for f in BotConfig.__dataclass_fields__.values()}
    filtered = {k: v for k, v in config_data.items() if k in valid_fields}

    return BotConfig(**filtered)


def validate_config(config: BotConfig) -> list[str]:
    """Validate config and return list of errors (empty = valid)."""
    errors = []

    if not config.private_key and not config.dry_run:
        errors.append("POLYMARKET_PRIVATE_KEY env var required when dry_run=false")

    if not 0.90 <= config.price_threshold <= 0.99:
        errors.append(f"price_threshold must be 0.90-0.99, got {config.price_threshold}")

    if config.max_position_per_market <= 0:
        errors.append("max_position_per_market must be positive")

    if config.max_total_deployed <= 0:
        errors.append("max_total_deployed must be positive")

    if config.max_open_positions < 1:
        errors.append("max_open_positions must be >= 1")

    if not isinstance(config.blocked_categories, list):
        errors.append("blocked_categories must be a list")

    if config.rpc_poll_interval_seconds < 5:
        errors.append("rpc_poll_interval_seconds must be >= 5")

    if config.rpc_poll_batch_size < 1:
        errors.append("rpc_poll_batch_size must be >= 1")

    return errors
