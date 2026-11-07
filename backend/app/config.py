"""Runtime settings. Every secret comes from the environment — nothing is hardcoded."""
from __future__ import annotations

import json
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="BTCS_", extra="ignore")

    # Storage
    database_url: str = "sqlite:///./btc_signal.db"
    redis_url: str | None = None

    # Admin API protection. When unset, admin write endpoints are refused.
    admin_token: str | None = None
    cors_origins: str = "*"

    # Scheduling
    cycle_seconds: int = 60
    signal_record_seconds: int = 300
    run_worker_in_api: bool = True
    liquidation_stream: bool = True
    http_timeout: float = 12.0

    # Esplora-compatible Bitcoin APIs, tried in order (mempool.space first).
    esplora_urls: str = "https://mempool.space/api,https://blockstream.info/api"

    # Optional provider keys. Adapters that need one stay "unconfigured" without it.
    coinglass_api_key: str | None = None
    whale_alert_api_key: str | None = None
    cryptoquant_api_key: str | None = None
    glassnode_api_key: str | None = None
    etherscan_api_key: str | None = None
    dune_api_key: str | None = None
    # JSON list: [{"query_id": 123, "metric": "EXCHANGE_NETFLOW_BTC", "date_col": "day", "value_col": "netflow"}]
    dune_queries: str = "[]"

    # Hyperliquid smart-money cohort size
    hyperliquid_top_n: int = 50

    def dune_query_list(self) -> list[dict]:
        try:
            data = json.loads(self.dune_queries or "[]")
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []

    def esplora_list(self) -> list[str]:
        return [u.strip().rstrip("/") for u in self.esplora_urls.split(",") if u.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
