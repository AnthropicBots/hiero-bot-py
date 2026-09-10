import pytest
from cryptography.fernet import Fernet

from app.auth.sync import _SYNC_CACHE, _prune_expired_cache
from app.utils.settings import Settings


def test_production_settings_validation():
    # Dev settings should pass
    dev_settings = Settings(_env_file=None, environment="development")
    assert dev_settings.is_production is False

    # Production without GITHUB_APP_ID should fail
    with pytest.raises(ValueError) as exc_info:
        Settings(_env_file=None, environment="production")
    assert "GITHUB_APP_ID" in str(exc_info.value)

    # Production with dev secrets should fail validation
    with pytest.raises(ValueError) as exc_info:
        Settings(
            _env_file=None,
            environment="production",
            github_app_id="123456",
            github_private_key="test-private-key",
            session_secret_key="dev-secret-key-32-bytes-minimum-length-change-in-prod",
            token_encryption_key="dev-token-encryption-key-32b-change-in-prod=",
            github_webhook_secret="sec",
        )
    assert "SESSION_SECRET_KEY" in str(exc_info.value)


def test_production_settings_accepts_valid_fernet_key():
    settings = Settings(
        _env_file=None,
        environment="production",
        github_app_id="123456",
        github_private_key="test-private-key",
        session_secret_key="session-secret-key-that-is-long-enough",
        token_encryption_key=Fernet.generate_key().decode(),
        github_webhook_secret="sec",
    )

    assert settings.is_production is True


def test_production_settings_rejects_arbitrary_32_character_key():
    with pytest.raises(
        ValueError,
        match="TOKEN_ENCRYPTION_KEY must be a valid Fernet key in production.",
    ):
        Settings(
            _env_file=None,
            environment="production",
            github_app_id="123456",
            github_private_key="test-private-key",
            session_secret_key="session-secret-key-that-is-long-enough",
            token_encryption_key="a" * 32,
            github_webhook_secret="sec",
        )


def test_sync_cache_pruning():
    _SYNC_CACHE[999] = (100.0, [{"id": 1}])
    _prune_expired_cache(now=1000.0)
    assert 999 not in _SYNC_CACHE
