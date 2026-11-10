# tests/unit/test_installation_webhooks.py — Installation webhook unit tests

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config.loader import ConfigInvalid
from app.db.database import Base
from app.db.models import Account, AccountRepo
from app.github.webhooks import WebhookRouter


@pytest.mark.asyncio
async def test_handle_installation_created_and_deleted(db):
    gh = MagicMock()
    config_loader = MagicMock()
    router = WebhookRouter(gh, config_loader)

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

    acc = await db.scalar(
        select(Account).where(Account.github_installation_id == 8888)
    )
    assert acc is not None
    assert acc.org_login == "testorg"

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

    acc = Account(
        github_installation_id=9999,
        org_login="repoorg",
        plan_tier="free",
    )
    db.add(acc)
    await db.commit()
    await db.refresh(acc)

    added_payload = {
        "action": "added",
        "installation": {"id": 9999},
        "repositories_added": [{"name": "repo-added"}],
    }

    res_add = await router._handle_installation_repositories(added_payload, db)
    assert res_add == {"ok": True, "action": "added"}

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


@pytest.mark.asyncio
async def test_repository_addition_is_persisted_after_session_closes(tmp_path):
    database_path = tmp_path / "repository-persistence.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
    )

    router = WebhookRouter(MagicMock(), MagicMock())

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with AsyncSession(
            engine,
            expire_on_commit=False,
        ) as setup_session:
            account = Account(
                github_installation_id=29999,
                org_login="persistedorg",
                plan_tier="free",
            )
            setup_session.add(account)
            await setup_session.commit()
            await setup_session.refresh(account)

        payload = {
            "action": "added",
            "installation": {"id": 29999},
            "repositories_added": [{"name": "persisted-repo"}],
        }

        async with AsyncSession(
            engine,
            expire_on_commit=False,
        ) as session:
            result = await router._handle_installation_repositories(
                payload,
                session,
            )

            assert result == {"ok": True, "action": "added"}

        async with AsyncSession(engine) as verification_session:
            account = await verification_session.scalar(
                select(Account).where(
                    Account.github_installation_id == 29999
                )
            )
            assert account is not None

            repo = await verification_session.scalar(
                select(AccountRepo).where(
                    AccountRepo.account_id == account.id,
                    AccountRepo.repo_name == "persisted-repo",
                )
            )

            assert repo is not None

    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_installation_creation_supports_active_transaction(db):
    router = WebhookRouter(MagicMock(), MagicMock())

    payload = {
        "action": "created",
        "installation": {
            "id": 18888,
            "account": {
                "id": 17777,
                "login": "activeorg",
                "type": "Organization",
            },
        },
        "repositories": [{"name": "active-repo"}],
    }

    async with db.begin():
        result = await router._handle_installation(payload, db)

        assert result == {"ok": True, "action": "created"}
        assert db.in_transaction()

        account = await db.scalar(
            select(Account).where(Account.github_installation_id == 18888)
        )
        assert account is not None

        repo = await db.scalar(
            select(AccountRepo).where(
                AccountRepo.account_id == account.id,
                AccountRepo.repo_name == "active-repo",
            )
        )
        assert repo is not None


@pytest.mark.asyncio
async def test_repository_addition_does_not_commit_outer_transaction(db):
    router = WebhookRouter(MagicMock(), MagicMock())

    account = Account(
        github_installation_id=19999,
        org_login="transactionorg",
        plan_tier="free",
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)

    account_id = account.id

    payload = {
        "action": "added",
        "installation": {"id": 19999},
        "repositories_added": [{"name": "transaction-repo"}],
    }

    outer_transaction = await db.begin_nested()

    result = await router._handle_installation_repositories(payload, db)

    assert result == {"ok": True, "action": "added"}

    repo = await db.scalar(
        select(AccountRepo).where(
            AccountRepo.account_id == account_id,
            AccountRepo.repo_name == "transaction-repo",
        )
    )
    assert repo is not None

    await outer_transaction.rollback()
    await db.rollback()

    repo = await db.scalar(
        select(AccountRepo).where(
            AccountRepo.account_id == account_id,
            AccountRepo.repo_name == "transaction-repo",
        )
    )
    assert repo is None


@pytest.mark.asyncio
async def test_installation_creation_preserves_external_transaction(tmp_path):
    database_path = tmp_path / "external-transaction.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
    )

    router = WebhookRouter(MagicMock(), MagicMock())

    payload = {
        "action": "created",
        "installation": {
            "id": 38888,
            "account": {
                "id": 37777,
                "login": "externaltransactionorg",
                "type": "Organization",
            },
        },
        "repositories": [{"name": "external-transaction-repo"}],
    }

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with engine.connect() as connection:
            outer_transaction = await connection.begin()

            async with AsyncSession(
                bind=connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            ) as session:
                await session.execute(select(1))

                assert session.in_transaction()
                assert outer_transaction.is_active

                result = await router._handle_installation(payload, session)

                assert result == {"ok": True, "action": "created"}
                assert outer_transaction.is_active

                account = await session.scalar(
                    select(Account).where(
                        Account.github_installation_id == 38888
                    )
                )
                assert account is not None

                repo = await session.scalar(
                    select(AccountRepo).where(
                        AccountRepo.account_id == account.id,
                        AccountRepo.repo_name == "external-transaction-repo",
                    )
                )
                assert repo is not None

            assert outer_transaction.is_active

            await outer_transaction.rollback()

        async with AsyncSession(engine) as verification_session:
            account = await verification_session.scalar(
                select(Account).where(
                    Account.github_installation_id == 38888
                )
            )

            assert account is None

    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_installation_creation_rejects_invalid_payload(db):
    router = WebhookRouter(MagicMock(), MagicMock())

    payload = {
        "action": "created",
        "installation": {
            "id": 48888,
            "account": {
                "id": 47777,
                "login": "invalidorg",
                "type": "Organization",
            },
        },
        "repositories": [{"name": ""}],
    }

    with pytest.raises(HTTPException) as exc_info:
        await router._handle_installation(payload, db)

    assert exc_info.value.status_code == 400
    assert "repository name is empty or too long" in exc_info.value.detail
