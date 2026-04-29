# app/utils/settings.py — Environment-based settings

from __future__ import annotations
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # GitHub App
    github_app_id: str = ""
    github_private_key: str = ""
    github_webhook_secret: str = ""

    # Anthropic
    anthropic_api_key: Optional[str] = None

    # Database
    database_url: str = "sqlite+aiosqlite:///./hiero_bot.db"

    # Server
    port: int = 8000
    host: str = "0.0.0.0"
    log_level: str = "info"
    environment: str = "development"

    # Dashboard basic auth (optional)
    dashboard_username: Optional[str] = None
    dashboard_password: Optional[str] = None

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


settings = Settings()
