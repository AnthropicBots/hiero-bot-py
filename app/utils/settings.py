# app/utils/settings.py — Environment-based settings

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # GitHub App
    github_app_id: str = ""
    github_private_key: str = ""
    github_webhook_secret: str = ""
    github_webhook_secret_old: str | None = None
    webhook_max_skew_seconds: int = 300

    # Anthropic
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    openai_base_url: str | None = None

    # Database
    database_url: str = "sqlite+aiosqlite:///./hiero_bot.db"

    # Server
    port: int = 8000
    host: str = "0.0.0.0"
    log_level: str = "info"
    environment: str = "development"

    # Dashboard basic auth (optional)
    dashboard_username: str | None = None
    dashboard_password: str | None = None

    # Rate limiting (per process, per caller). The webhook budget is generous
    # because a busy org can legitimately burst deliveries; the API budget is
    # tighter because it is the surface a stranger can reach.
    rate_limit_enabled: bool = True
    rate_limit_webhook_per_minute: int = 600
    rate_limit_api_per_minute: int = 120
    rate_limit_burst: int = 0  # 0 = burst equals the per-minute budget

    # Number of proxies in front of this app. Above 0, X-Forwarded-For is
    # trusted for caller identity; at 0 it is ignored, because a client that
    # can forge it can hand itself an unlimited number of buckets.
    trusted_proxy_hops: int = 0

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


settings = Settings()
