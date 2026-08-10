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

    # AI review backends. All optional — AI review is off by default, and each
    # backend is only built when the repo config selects it.
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    # A local Ollama endpoint, e.g. http://localhost:11434. The one backend
    # that keeps source code on your own infrastructure.
    ollama_base_url: str | None = None

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

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


settings = Settings()
