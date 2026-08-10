# tests/unit/test_scheduler.py — scheduled stale scan

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.scheduler.jobs import BotScheduler, ScanSummary


def make_loader(config=None, side_effect=None):
    loader = MagicMock()
    if side_effect is not None:
        loader.load = AsyncMock(side_effect=side_effect)
    else:
        loader.load = AsyncMock(return_value=config)
    loader.clear = MagicMock()
    return loader


@pytest.fixture
def gh_with_one_repo():
    gh = AsyncMock()
    gh.list_installations = AsyncMock(return_value=[{"id": 42}])
    gh.list_installation_repos = AsyncMock(
        return_value=[{"full_name": "hiero/sdk-js"}]
    )
    gh.list_issues = AsyncMock(return_value=[])
    return gh


@pytest.mark.asyncio
async def test_config_is_loaded_with_installation_id(gh_with_one_repo, base_config):
    """Regression for #39 — the scan authenticated as the app and always 404'd."""
    loader = make_loader(base_config)
    scheduler = BotScheduler(gh_with_one_repo, loader)

    await scheduler.run_stale_scan()

    loader.load.assert_awaited_once_with("hiero", "sdk-js", 42)


@pytest.mark.asyncio
async def test_scan_reports_scanned_repos(gh_with_one_repo, base_config):
    scheduler = BotScheduler(gh_with_one_repo, make_loader(base_config))

    summary = await scheduler.run_stale_scan()

    assert summary.installations == 1
    assert summary.repos_scanned == 1
    assert summary.repos_failed == 0


@pytest.mark.asyncio
async def test_repo_without_config_is_skipped(gh_with_one_repo):
    scheduler = BotScheduler(gh_with_one_repo, make_loader(None))

    summary = await scheduler.run_stale_scan()

    assert summary.repos_skipped == 1
    assert summary.repos_scanned == 0


@pytest.mark.asyncio
async def test_repo_with_issue_management_disabled_is_skipped(
    gh_with_one_repo, base_config
):
    base_config.workflows.issue_management.enabled = False
    scheduler = BotScheduler(gh_with_one_repo, make_loader(base_config))

    summary = await scheduler.run_stale_scan()

    assert summary.repos_skipped == 1


@pytest.mark.asyncio
async def test_one_failing_repo_does_not_abort_the_scan(base_config):
    gh = AsyncMock()
    gh.list_installations = AsyncMock(return_value=[{"id": 42}])
    gh.list_installation_repos = AsyncMock(
        return_value=[{"full_name": "hiero/broken"}, {"full_name": "hiero/sdk-js"}]
    )
    gh.list_issues = AsyncMock(return_value=[])

    loader = make_loader(side_effect=[RuntimeError("boom"), base_config])
    scheduler = BotScheduler(gh, loader)

    summary = await scheduler.run_stale_scan()

    assert summary.repos_failed == 1
    assert summary.repos_scanned == 1


@pytest.mark.asyncio
async def test_malformed_repo_entry_is_skipped(base_config):
    gh = AsyncMock()
    gh.list_installations = AsyncMock(return_value=[{"id": 42}])
    gh.list_installation_repos = AsyncMock(return_value=[{"name": "no-full-name"}])

    loader = make_loader(base_config)
    scheduler = BotScheduler(gh, loader)

    summary = await scheduler.run_stale_scan()

    assert summary.repos_skipped == 1
    loader.load.assert_not_awaited()


@pytest.mark.asyncio
async def test_installation_listing_failure_returns_empty_summary():
    gh = AsyncMock()
    gh.list_installations = AsyncMock(side_effect=RuntimeError("api down"))

    scheduler = BotScheduler(gh, make_loader(None))
    summary = await scheduler.run_stale_scan()

    assert summary.installations == 0
    assert summary.repos_scanned == 0


@pytest.mark.asyncio
async def test_repo_listing_failure_skips_that_installation(base_config):
    gh = AsyncMock()
    gh.list_installations = AsyncMock(return_value=[{"id": 1}, {"id": 2}])
    gh.list_installation_repos = AsyncMock(
        side_effect=[RuntimeError("no access"), [{"full_name": "hiero/sdk-js"}]]
    )
    gh.list_issues = AsyncMock(return_value=[])

    scheduler = BotScheduler(gh, make_loader(base_config))
    summary = await scheduler.run_stale_scan()

    assert summary.installations == 2
    assert summary.repos_scanned == 1


@pytest.mark.asyncio
async def test_installation_without_id_is_ignored(base_config):
    gh = AsyncMock()
    gh.list_installations = AsyncMock(return_value=[{"account": "orphan"}])
    gh.list_installation_repos = AsyncMock(return_value=[])

    scheduler = BotScheduler(gh, make_loader(base_config))
    summary = await scheduler.run_stale_scan()

    gh.list_installation_repos.assert_not_awaited()
    assert summary.repos_scanned == 0


def test_summary_accumulates_counts():
    summary = ScanSummary()
    summary.add({"stale_marked": 2, "closed": 1, "unassigned": 0})
    summary.add({"stale_marked": 3, "closed": 0, "unassigned": 4})

    assert summary.as_dict()["stale_marked"] == 5
    assert summary.as_dict()["closed"] == 1
    assert summary.as_dict()["unassigned"] == 4


def test_flush_config_cache_clears_loader():
    loader = make_loader(None)
    scheduler = BotScheduler(AsyncMock(), loader)

    import asyncio

    asyncio.run(scheduler._flush_config_cache())

    loader.clear.assert_called_once()
