# tests/unit/test_lifespan_scheduler.py
#
# Issue #90 — the stale scanner / config-cache flusher previously only
# started when settings.is_production was True, so staging and dev
# deployments serving real webhook traffic never ran scheduled work.
# The fix decouples scheduler startup onto its own `enable_scheduler` flag.

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.main as main_module
from app.utils.settings import Settings


def test_enable_scheduler_defaults_true_regardless_of_environment():
    dev = Settings(_env_file=None, environment="development")
    staging = Settings(_env_file=None, environment="staging")
    assert dev.enable_scheduler is True
    assert staging.enable_scheduler is True
    assert dev.is_production is False
    assert staging.is_production is False


@pytest.mark.asyncio
async def test_scheduler_starts_in_development_when_enabled(monkeypatch):
    """The core regression: a non-production environment with
    enable_scheduler=True must still start the scheduler."""
    monkeypatch.setattr(main_module.settings, "environment", "development")
    monkeypatch.setattr(main_module.settings, "enable_scheduler", True)

    mock_scheduler = MagicMock()
    mock_scheduler.start = MagicMock()
    mock_scheduler.shutdown = MagicMock()

    with (
        patch.object(main_module, "init_db", new=AsyncMock()),
        patch.object(main_module, "GitHubClient", return_value=MagicMock(close=AsyncMock())),
        patch.object(main_module, "ConfigLoader", return_value=MagicMock()),
        patch.object(main_module, "BotScheduler", return_value=mock_scheduler),
    ):
        assert main_module.settings.is_production is False
        async with main_module.lifespan(MagicMock()):
            pass

    mock_scheduler.start.assert_called_once()


@pytest.mark.asyncio
async def test_scheduler_does_not_start_when_disabled(monkeypatch):
    """enable_scheduler=False must suppress scheduler startup even in
    production, e.g. for unit tests or short-lived local runs."""
    monkeypatch.setattr(main_module.settings, "environment", "production")
    monkeypatch.setattr(main_module.settings, "enable_scheduler", False)

    mock_scheduler = MagicMock()
    mock_scheduler.start = MagicMock()
    mock_scheduler.shutdown = MagicMock()

    with (
        patch.object(main_module, "init_db", new=AsyncMock()),
        patch.object(main_module, "GitHubClient", return_value=MagicMock(close=AsyncMock())),
        patch.object(main_module, "ConfigLoader", return_value=MagicMock()),
        patch.object(main_module, "BotScheduler", return_value=mock_scheduler),
    ):
        async with main_module.lifespan(MagicMock()):
            pass

    mock_scheduler.start.assert_not_called()
