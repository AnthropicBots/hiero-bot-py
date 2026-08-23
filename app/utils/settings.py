# app/utils/settings.py — Environment-based settings

from __future__ import annotations

from pydantic import model_validator
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

    # GitHub OAuth & Session Security
    github_oauth_client_id: str = ""
    github_oauth_client_secret: str = ""
    session_secret_key: str = (
        "dev-secret-key-32-bytes-minimum-length-change-in-prod"
    )
    token_encryption_key: str = "dev-token-encryption-key-32b-change-in-prod="
    legacy_basic_auth_enabled: bool = True

    # Non-prod default below; override via env (comma/JSON-list) in production.
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    # Stripe Billing
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None

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

    @model_validator(mode="after")
    def validate_production_secrets(self) -> Settings:
        if self.is_production:
            if not self.github_app_id:
                raise ValueError("GITHUB_APP_ID must be configured in production.")
            if not self.github_private_key:
                raise ValueError("GITHUB_PRIVATE_KEY must be configured in production.")
            if (
                "dev-secret-key" in self.session_secret_key
                or len(self.session_secret_key) < 32
            ):
                raise ValueError(
                    "SESSION_SECRET_KEY must be set to a secure key "
                    "(min 32 chars) in production."
                )
            if (
                "dev-token-encryption-key" in self.token_encryption_key
                or len(self.token_encryption_key) < 32
            ):
                raise ValueError(
                    "TOKEN_ENCRYPTION_KEY must be set to a secure 32-byte key "
                    "in production."
                )
            if not self.github_webhook_secret:
                raise ValueError(
                    "GITHUB_WEBHOOK_SECRET must be set in production."
                )
        return self


settings = Settings()