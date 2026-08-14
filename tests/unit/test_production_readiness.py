import pytest

from app.auth.sync import _SYNC_CACHE, _prune_expired_cache
from app.utils.settings import Settings


def test_production_settings_validation():
    # Dev settings should pass
    dev_settings = Settings(environment="development")
    assert dev_settings.is_production is False

    # Production with dev secrets should fail validation
    with pytest.raises(ValueError) as exc_info:
        Settings(
            environment="production",
            session_secret_key="dev-secret-key-32-bytes-minimum-length-change-in-prod",
            token_encryption_key="dev-token-encryption-key-32b-change-in-prod=",
            github_webhook_secret="sec",
        )
    assert "SESSION_SECRET_KEY" in str(exc_info.value)


def test_sync_cache_pruning():
    _SYNC_CACHE[999] = (100.0, [{"id": 1}])
    _prune_expired_cache(now=1000.0)
    assert 999 not in _SYNC_CACHE
