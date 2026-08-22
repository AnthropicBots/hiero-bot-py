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

    # CORS
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:5173"]  # override via env in prod

    # Dashboard basic auth (optional)
    dashboard_username: str | None = None
    dashboard_password: str | None = None

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


settings = Settings()