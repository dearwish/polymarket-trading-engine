from functools import lru_cache
from pathlib import Path

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
    market_family: str = "btc_5m"
    loop_seconds: int = 15

    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "openai/gpt-4.1-mini"

    polymarket_host: str = "https://clob.polymarket.com"
    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"
    polymarket_chain_id: int = 137
    polymarket_private_key: str = ""
    polymarket_funder: str = ""
    polymarket_signature_type: int = 0

    max_position_usd: float = 10.0
    min_confidence: float = 0.75
    min_edge: float = 0.03
    max_spread: float = 0.04
    min_depth_usd: float = 200.0
    exit_buffer_seconds: int = 5
    max_daily_loss_usd: float = 25.0
    stale_data_seconds: int = 30
    max_rejected_orders: int = 3

    data_dir: Path = Field(default=Path("data"))
    log_dir: Path = Field(default=Path("logs"))
    db_path: Path = Field(default=Path("data/agent.db"))
    events_path: Path = Field(default=Path("logs/events.jsonl"))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.events_path.parent.mkdir(parents=True, exist_ok=True)
    return settings
