# tests/unit/test_installation_webhooks.py — Installation webhook unit tests

import base64
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from app.auth.sync import get_user_authorized_accounts
from app.config.loader import ConfigInvalid, ConfigLoader
from app.db.models import Account, AccountRepo, AccountUser, User
from app.github.webhooks import WebhookRouter
from app.utils.access import get_authorized_access


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
async def test_invalid_config_is_acknowledged_without_retrying(db, monkeypatch):
    from app.github import webhooks as webhooks_module

    webhook_log = Mock()
    monkeypatch.setattr(webhooks_module, "log", webhook_log)

    gh = MagicMock()
    config_loader = MagicMock()

    async def load(owner, repo, installation_id):
        raise ConfigInvalid(
            f"{owner}/{repo}",
            "workflows.pull_request.enabled: Input should be a valid boolean",
        )

    config_loader.load = AsyncMock(side_effect=load)
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
    request.headers = {"X-GitHub-Event": "pull_request"}
    request.json = AsyncMock(return_value=payload)

    for _ in range(3):
        result = await router.handle(request, db)
        assert result == {"ok": True, "skipped": "invalid config"}

    assert config_loader.load.await_count == 3
    webhook_log.error.assert_not_called()


@pytest.mark.asyncio
async def test_push_config_change_invalidates_real_invalid_config_cache(db):
    invalid_content = base64.b64encode(b"- invalid\n").decode()
    valid_content = base64.b64encode(
        b'repo: "testorg/testrepo"\nworkflows: {}\n'
    ).decode()

    gh = MagicMock()
    gh.get_file_content = AsyncMock(
        side_effect=[invalid_content, valid_content]
    )
    config_loader = ConfigLoader(gh)
    router = WebhookRouter(gh, config_loader)

    # Prime the real loader with an invalid configuration.
    with pytest.raises(ConfigInvalid):
        await config_loader.load("testorg", "testrepo", 8888)

    # A second load should use the cached invalid result.
    with pytest.raises(ConfigInvalid):
        await config_loader.load("testorg", "testrepo", 8888)

    assert gh.get_file_content.await_count == 1

    payload = {
        "repository": {
            "owner": {"login": "testorg"},
            "name": "testrepo",
        },
        "installation": {"id": 8888},
        "commits": [
            {
                "added": [],
                "modified": [".github/hiero-bot.yml"],
                "removed": [],
            }
        ],
    }

    request = MagicMock()
    request.body = AsyncMock(return_value=b"")
    request.headers = {"X-GitHub-Event": "push"}
    request.json = AsyncMock(return_value=payload)
    router._verify_signature = MagicMock()

    result = await router.handle(request, db)

    assert result == {"ok": True}
    assert gh.get_file_content.await_count == 2

    # The valid configuration should now be cached.
    cached = await config_loader.load("testorg", "testrepo", 8888)

    assert cached is not None
    assert cached.repo == "testorg/testrepo"
    assert gh.get_file_content.await_count == 2


@pytest.mark.asyncio
async def test_repository_removal_webhook_revokes_dashboard_access_with_stale_cache(db):
    router = WebhookRouter(MagicMock(), MagicMock())

    user = User(github_user_id=9901, github_login="webhook-test-user")
    db.add(user)
    await db.commit()
    await db.refresh(user)

    account = Account(
        github_installation_id=9901,
        org_login="webhook-org",
        plan_tier="free",
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)

    db.add(AccountUser(
        account_id=account.id,
        user_id=user.id,
        authorized=True,
    ))
    db.add(AccountRepo(
        account_id=account.id,
        repo_name="private-repo",
    ))
    await db.commit()

    # Populate the authorization cache before the webhook arrives.
    cached = await get_user_authorized_accounts(user, db)
    assert cached[0]["repos"] == ["private-repo"]

    result = await router._handle_installation_repositories(
        {
            "action": "removed",
            "installation": {"id": 9901},
            "repositories_removed": [{"name": "private-repo"}],
        },
        db,
    )

    assert result == {"ok": True, "action": "removed"}

    # The existing cache must still contain the old repository.
    cached = await get_user_authorized_accounts(user, db)
    assert cached[0]["repos"] == ["private-repo"]

    # Dashboard authorization must reflect the webhook's DB update.
    access = await get_authorized_access(user, db)

    assert access.owners == {"webhook-org"}
    assert access.repos == {}


@pytest.mark.asyncio
async def test_installation_deletion_webhook_revokes_dashboard_access_with_stale_cache(db):
    router = WebhookRouter(MagicMock(), MagicMock())

    user = User(github_user_id=9902, github_login="deletion-test-user")
    db.add(user)
    await db.commit()
    await db.refresh(user)

    account = Account(
        github_installation_id=9902,
        org_login="deleted-org",
        plan_tier="free",
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)

    db.add(AccountUser(
        account_id=account.id,
        user_id=user.id,
        authorized=True,
    ))
    db.add(AccountRepo(
        account_id=account.id,
        repo_name="private-repo",
    ))
    await db.commit()

    # Populate the authorization cache before installation deletion.
    cached = await get_user_authorized_accounts(user, db)
    assert cached[0]["repos"] == ["private-repo"]

    result = await router._handle_installation(
        {
            "action": "deleted",
            "installation": {"id": 9902},
        },
        db,
    )

    assert result == {"ok": True, "action": "deleted"}

    # The existing cache must still contain the old installation.
    cached = await get_user_authorized_accounts(user, db)
    assert cached[0]["repos"] == ["private-repo"]

    # Dashboard authorization must reject the deleted installation.
    access = await get_authorized_access(user, db)

    assert access.owners == set()
    assert access.repos == {}
