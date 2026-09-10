# tests/unit/test_installation_webhooks.py — Installation webhook unit tests

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config.loader import ConfigInvalid
from app.db.models import Account
from app.github.webhooks import WebhookRouter


@pytest.mark.asyncio
async def test_handle_installation_created_and_deleted(db):
    gh = MagicMock()
    config_loader = MagicMock()
    router = WebhookRouter(gh, config_loader)

    # 1. Created action
    created_payload = {
        "action": "created",
        "installation": {
            "id": 8888,
            "account": {
                "id": 7777,
                "login": "testorg",
                "type": "Organization",
            },
        },
        "repositories": [{"name": "repo1"}, {"name": "repo2"}],
    }

    res = await router._handle_installation(created_payload, db)
    assert res == {"ok": True, "action": "created"}

    acc = (await db.execute(Account.__table__.select().where(Account.github_installation_id == 8888))).first()
    assert acc is not None
    assert acc.org_login == "testorg"

    # 2. Deleted action
    deleted_payload = {
        "action": "deleted",
        "installation": {"id": 8888},
    }
    res_del = await router._handle_installation(deleted_payload, db)
    assert res_del == {"ok": True, "action": "deleted"}


@pytest.mark.asyncio
async def test_handle_installation_repositories_added_and_removed(db):
    gh = MagicMock()
    config_loader = MagicMock()
    router = WebhookRouter(gh, config_loader)

    acc = Account(github_installation_id=9999, org_login="repoorg", plan_tier="free")
    db.add(acc)
    await db.commit()
    await db.refresh(acc)

    # Added action
    added_payload = {
        "action": "added",
        "installation": {"id": 9999},
        "repositories_added": [{"name": "repo-added"}],
    }
    res_add = await router._handle_installation_repositories(added_payload, db)
    assert res_add == {"ok": True, "action": "added"}

    # Removed action
    removed_payload = {
        "action": "removed",
        "installation": {"id": 9999},
        "repositories_removed": [{"name": "repo-added"}],
    }
    res_rem = await router._handle_installation_repositories(removed_payload, db)
    assert res_rem == {"ok": True, "action": "removed"}


@pytest.mark.asyncio
async def test_config_change_invalidates_cache_before_loading(db):
    gh = MagicMock()
    config_loader = MagicMock()
    invalidated = False

    def invalidate(owner, repo):
        nonlocal invalidated
        invalidated = True

    async def load(owner, repo, installation_id):
        assert invalidated is True
        return {"enabled": True}

    config_loader.invalidate.side_effect = invalidate
    config_loader.load.side_effect = load

    router = WebhookRouter(gh, config_loader)

    payload = {
        "repository": {
            "owner": {"login": "testorg"},
            "name": "testrepo",
        },
        "installation": {"id": 8888},
        "commits": [
            {
                "added": [".github/hiero-bot.yml"],
                "modified": [],
                "removed": [],
            }
        ],
    }

    request = MagicMock()
    request.body = AsyncMock(return_value=b"")
    router._verify_signature = MagicMock()
    request.headers = {
        "X-GitHub-Event": "push",
    }
    request.json = AsyncMock(return_value=payload)

    result = await router.handle(request, db)

    assert result == {"ok": True}
    config_loader.invalidate.assert_called_once_with("testorg", "testrepo")
    config_loader.load.assert_called_once_with("testorg", "testrepo", 8888)


@pytest.mark.asyncio
async def test_invalid_config_is_acknowledged_without_retrying(db):
    gh = MagicMock()
    config_loader = MagicMock()
    config_loader.load = AsyncMock()

    async def load(owner, repo, installation_id):
        raise ConfigInvalid(
            f"{owner}/{repo}",
            "workflows.pull_request.enabled: Input should be a valid boolean",
        )

    config_loader.load.side_effect = load

    router = WebhookRouter(gh, config_loader)

    payload = {
        "repository": {
            "owner": {"login": "testorg"},
            "name": "testrepo",
        },
        "installation": {"id": 8888},
    }

    request = MagicMock()
    request.body = AsyncMock(return_value=b"")
    router._verify_signature = MagicMock()
    request.headers = {
        "X-GitHub-Event": "pull_request",
    }
    request.json = AsyncMock(return_value=payload)

    result = await router.handle(request, db)

    assert result == {
        "ok": True,
        "skipped": "invalid config",
    }
    config_loader.load.assert_awaited_once_with(
        "testorg",
        "testrepo",
        8888,
    )
