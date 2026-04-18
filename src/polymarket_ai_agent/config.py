from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "polymarket-ai-agent"
    trading_mode: str = "paper"
    market_family: str = "btc_1h"
    loop_seconds: int = 15

    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "openai/gpt-4.1-mini"

    polymarket_host: str = "https://clob.polymarket.com"
    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"
    polymarket_data_url: str = "https://data-api.polymarket.com"
    polymarket_ws_market_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    polymarket_ws_user_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
    polymarket_chain_id: int = 137
    btc_ws_url: str = "wss://stream.binance.com:9443/stream"
    btc_symbol: str = "btcusdt"
    btc_rest_fallback_url: str = "https://api.binance.com/api/v3/ticker/price"
    ws_reconnect_backoff_seconds: float = 2.0
    ws_reconnect_backoff_max_seconds: float = 30.0
    daemon_discovery_interval_seconds: int = 60
    daemon_decision_min_interval_seconds: float = 1.0
    polymarket_private_key: str = ""
    polymarket_funder: str = ""
    polymarket_signature_type: int = 0
    live_trading_enabled: bool = False
    live_order_type: str = "FOK"
    live_post_only: bool = False

    max_position_usd: float = 10.0
    min_confidence: float = 0.75
    min_edge: float = 0.03
    max_spread: float = 0.04
    min_depth_usd: float = 200.0
    exit_buffer_seconds: int = 5
    exit_buffer_pct_of_tte: float = 0.0
    max_daily_loss_usd: float = 25.0
    stale_data_seconds: int = 30
    max_rejected_orders: int = 3
    max_concurrent_positions: int = 1
    max_net_btc_exposure_usd: float = 50.0
    paper_starting_balance_usd: float = 100.0
    paper_position_ttl_seconds: int = 60
    paper_entry_slippage_bps: float = 10.0
    paper_exit_slippage_bps: float = 10.0

    fee_bps: float = 0.0
    execution_maker_min_edge: float = 0.04
    execution_maker_min_tte_seconds: int = 120
    execution_price_tick: float = 0.01
    execution_exit_buffer_floor_seconds: int = 10
    execution_exit_buffer_pct_of_tte: float = 0.1
    quant_drift_damping: float = 0.5
    quant_imbalance_tilt: float = 0.03
    quant_slippage_baseline_bps: float = 15.0
    quant_slippage_spread_coef: float = 0.25
    quant_default_vol_per_second: float = 0.00015
    quant_drift_horizon_seconds: float = 900.0
    quant_tte_floor_seconds: float = 5.0
    quant_confidence_per_edge: float = 10.0
    quant_high_expiry_risk_seconds: int = 15
    quant_medium_expiry_risk_seconds: int = 60

    events_jsonl_max_bytes: int = 200_000_000
    events_jsonl_keep_tail_bytes: int = 50_000_000

    data_dir: Path = Field(default=Path("data"))
    log_dir: Path = Field(default=Path("logs"))
    db_path: Path = Field(default=Path("data/agent.db"))
    events_path: Path = Field(default=Path("logs/events.jsonl"))
    runtime_settings_path: Path = Field(default=Path("data/runtime_settings.json"))


EDITABLE_SETTINGS_METADATA: dict[str, dict[str, Any]] = {
    "trading_mode": {"label": "Mode", "type": "select", "options": ["paper", "live"], "group": "runtime"},
    "market_family": {
        "label": "Market Family",
        "type": "select",
        "options": ["btc_1h", "btc_15m", "btc_5m", "btc_daily_threshold"],
        "group": "runtime",
    },
    "loop_seconds": {"label": "Loop Seconds", "type": "number", "min": 1, "max": 300, "step": 1, "group": "runtime"},
    "openrouter_model": {"label": "OpenRouter Model", "type": "text", "group": "runtime"},
    "live_trading_enabled": {"label": "Live Trading Enabled", "type": "boolean", "group": "live"},
    "live_order_type": {"label": "Live Order Type", "type": "select", "options": ["FOK", "GTC"], "group": "live"},
    "live_post_only": {"label": "Live Post Only", "type": "boolean", "group": "live"},
    "max_position_usd": {"label": "Max Position USD", "type": "number", "min": 1, "max": 100000, "step": 0.5, "group": "thresholds"},
    "min_confidence": {"label": "Min Confidence", "type": "number", "min": 0, "max": 1, "step": 0.01, "group": "thresholds"},
    "min_edge": {"label": "Min Edge", "type": "number", "min": 0, "max": 1, "step": 0.001, "group": "thresholds"},
    "max_spread": {"label": "Max Spread", "type": "number", "min": 0, "max": 1, "step": 0.001, "group": "thresholds"},
    "min_depth_usd": {"label": "Min Depth USD", "type": "number", "min": 0, "max": 1000000, "step": 1, "group": "thresholds"},
    "exit_buffer_seconds": {"label": "Exit Buffer Seconds", "type": "number", "min": 0, "max": 3600, "step": 1, "group": "thresholds"},
    "max_daily_loss_usd": {"label": "Max Daily Loss USD", "type": "number", "min": 0, "max": 100000, "step": 0.5, "group": "thresholds"},
    "stale_data_seconds": {"label": "Stale Data Seconds", "type": "number", "min": 1, "max": 3600, "step": 1, "group": "thresholds"},
    "max_rejected_orders": {"label": "Max Rejected Orders", "type": "number", "min": 1, "max": 100, "step": 1, "group": "thresholds"},
    "max_concurrent_positions": {"label": "Max Concurrent Positions", "type": "number", "min": 1, "max": 20, "step": 1, "group": "thresholds"},
    "max_net_btc_exposure_usd": {"label": "Max Net BTC Exposure USD", "type": "number", "min": 0, "max": 1000000, "step": 1, "group": "thresholds"},
    "exit_buffer_pct_of_tte": {"label": "Exit Buffer % of TTE", "type": "number", "min": 0, "max": 1, "step": 0.01, "group": "thresholds"},
    "paper_starting_balance_usd": {
        "label": "Paper Starting Balance USD",
        "type": "number",
        "min": 0,
        "max": 1000000,
        "step": 1,
        "group": "paper",
    },
    "paper_position_ttl_seconds": {
        "label": "Paper Position TTL Seconds",
        "type": "number",
        "min": 1,
        "max": 86400,
        "step": 1,
        "group": "paper",
    },
    "paper_entry_slippage_bps": {
        "label": "Paper Entry Slippage BPS",
        "type": "number",
        "min": 0,
        "max": 10000,
        "step": 0.1,
        "group": "paper",
    },
    "paper_exit_slippage_bps": {
        "label": "Paper Exit Slippage BPS",
        "type": "number",
        "min": 0,
        "max": 10000,
        "step": 0.1,
        "group": "paper",
    },
}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.events_path.parent.mkdir(parents=True, exist_ok=True)
    settings.runtime_settings_path.parent.mkdir(parents=True, exist_ok=True)
    return settings


def load_runtime_overrides(settings: Settings) -> dict[str, Any]:
    path = settings.runtime_settings_path
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {key: value for key, value in raw.items() if key in EDITABLE_SETTINGS_METADATA}


def save_runtime_overrides(settings: Settings, updates: dict[str, Any]) -> dict[str, Any]:
    editable_updates = {key: value for key, value in updates.items() if key in EDITABLE_SETTINGS_METADATA}
    merged = {**load_runtime_overrides(settings), **editable_updates}
    candidate = Settings.model_validate({**settings.model_dump(), **merged})
    clean_overrides = {
        key: getattr(candidate, key)
        for key in EDITABLE_SETTINGS_METADATA
        if key in merged and getattr(candidate, key) != getattr(settings, key)
    }
    settings.runtime_settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings.runtime_settings_path.write_text(json.dumps(clean_overrides, indent=2, sort_keys=True), encoding="utf-8")
    return clean_overrides


def get_effective_settings() -> Settings:
    base = get_settings()
    overrides = load_runtime_overrides(base)
    if not overrides:
        return base
    effective = Settings.model_validate({**base.model_dump(), **overrides})
    effective.data_dir.mkdir(parents=True, exist_ok=True)
    effective.log_dir.mkdir(parents=True, exist_ok=True)
    effective.db_path.parent.mkdir(parents=True, exist_ok=True)
    effective.events_path.parent.mkdir(parents=True, exist_ok=True)
    effective.runtime_settings_path.parent.mkdir(parents=True, exist_ok=True)
    return effective


def runtime_settings_payload(settings: Settings) -> dict[str, Any]:
    overrides = load_runtime_overrides(settings)
    return {
        "values": {key: getattr(settings, key) for key in EDITABLE_SETTINGS_METADATA},
        "overrides": overrides,
        "fields": EDITABLE_SETTINGS_METADATA,
    }


@dataclass(slots=True, frozen=True)
class RiskProfile:
    """Resolved per-family risk parameters consumed by :class:`RiskEngine`.

    The profile is derived from :class:`Settings` via :func:`resolve_risk_profile`:
    if a field was explicitly overridden on ``Settings`` the override wins, so
    existing deployments that tune globals via env vars are unchanged. If the
    field is left at the built-in default and the active ``market_family`` has
    a per-family override in :data:`FAMILY_PROFILE_OVERRIDES`, the tighter
    family value is applied.
    """

    family: str
    min_edge: float
    max_spread: float
    min_depth_usd: float
    stale_data_seconds: int
    exit_buffer_floor_seconds: int
    exit_buffer_pct_of_tte: float
    family_window_seconds: int
    max_position_usd: float
    max_concurrent_positions: int
    max_net_btc_exposure_usd: float


FAMILY_PROFILE_OVERRIDES: dict[str, dict[str, Any]] = {
    "btc_1h": {
        "stale_data_seconds": 5,
        "exit_buffer_pct_of_tte": 0.05,
        "max_concurrent_positions": 2,
        "family_window_seconds": 3600,
    },
    "btc_15m": {
        "stale_data_seconds": 3,
        "exit_buffer_pct_of_tte": 0.07,
        "max_concurrent_positions": 2,
        "family_window_seconds": 900,
    },
    "btc_5m": {
        "stale_data_seconds": 2,
        "exit_buffer_pct_of_tte": 0.10,
        "max_concurrent_positions": 1,
        "family_window_seconds": 300,
    },
}


_PROFILE_FIELD_TO_SETTING = {
    "stale_data_seconds": "stale_data_seconds",
    "exit_buffer_floor_seconds": "exit_buffer_seconds",
    "exit_buffer_pct_of_tte": "exit_buffer_pct_of_tte",
    "max_position_usd": "max_position_usd",
    "max_concurrent_positions": "max_concurrent_positions",
}


def resolve_risk_profile(settings: Settings) -> RiskProfile:
    family = settings.market_family
    overrides = FAMILY_PROFILE_OVERRIDES.get(family, {})
    explicit = settings.model_fields_set

    def pick(profile_field: str) -> Any:
        settings_field = _PROFILE_FIELD_TO_SETTING.get(profile_field, profile_field)
        # Explicit operator override wins.
        if settings_field in explicit:
            return getattr(settings, settings_field)
        if profile_field in overrides:
            return overrides[profile_field]
        return getattr(settings, settings_field)

    family_window = int(overrides.get("family_window_seconds", 0))
    return RiskProfile(
        family=family,
        min_edge=settings.min_edge,
        max_spread=settings.max_spread,
        min_depth_usd=settings.min_depth_usd,
        stale_data_seconds=int(pick("stale_data_seconds")),
        exit_buffer_floor_seconds=int(pick("exit_buffer_floor_seconds")),
        exit_buffer_pct_of_tte=float(pick("exit_buffer_pct_of_tte")),
        family_window_seconds=family_window,
        max_position_usd=float(pick("max_position_usd")),
        max_concurrent_positions=int(pick("max_concurrent_positions")),
        max_net_btc_exposure_usd=float(settings.max_net_btc_exposure_usd),
    )
