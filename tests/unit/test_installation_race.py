import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.database import Base
from app.db.models import Account, AccountRepo
from app.github.webhooks import WebhookRouter


@pytest.mark.asyncio
async def test_installation_rejects_when_account_missing(db):
    router = WebhookRouter(MagicMock(), MagicMock())

    payload = {
        "action": "created",
        "installation": {"id": 1},
    }

    with pytest.raises(HTTPException) as exc_info:
        await WebhookRouter._handle_installation(router, payload, db)

    assert exc_info.value.status_code == 400
    assert "account information is missing" in exc_info.value.detail


@pytest.mark.asyncio
async def test_installation_rejects_without_id(db):
    router = WebhookRouter(MagicMock(), MagicMock())

    with pytest.raises(HTTPException) as exc_info:
        await WebhookRouter._handle_installation(
            router,
            {"action": "created"},
            db,
        )

    assert exc_info.value.status_code == 400
    assert "installation object is missing" in exc_info.value.detail


@pytest.mark.asyncio
async def test_concurrent_installation_creation_is_idempotent(tmp_path):
    database_path = tmp_path / "installation-race.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"

    engine = create_async_engine(
        database_url,
        connect_args={"timeout": 30},
    )

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        router = WebhookRouter(MagicMock(), MagicMock())

        payload = {
            "action": "created",
            "installation": {
                "id": 90001,
                "account": {
                    "id": 80001,
                    "login": "concurrent-org",
                    "type": "Organization",
                },
            },
            "repositories": [
                {"name": "repo-one"},
                {"name": "repo-two"},
            ],
        }

        async def process_delivery():
            async with session_factory() as session:
                return await router._handle_installation(payload, session)

        results = await asyncio.gather(
            process_delivery(),
            process_delivery(),
        )

        assert results == [
            {"ok": True, "action": "created"},
            {"ok": True, "action": "created"},
        ]

        async with session_factory() as session:
            account_count = await session.scalar(
                select(func.count())
                .select_from(Account)
                .where(Account.github_installation_id == 90001)
            )

            assert account_count == 1

            account = await session.scalar(
                select(Account).where(
                    Account.github_installation_id == 90001
                )
            )
            assert account is not None

            repo_count = await session.scalar(
                select(func.count())
                .select_from(AccountRepo)
                .where(AccountRepo.account_id == account.id)
            )

            assert repo_count == 2

            repo_names = set(
                (
                    await session.scalars(
                        select(AccountRepo.repo_name).where(
                            AccountRepo.account_id == account.id
                        )
                    )
                ).all()
            )

            assert repo_names == {"repo-one", "repo-two"}

    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_repository_events_recover_missing_account(tmp_path):
    database_path = tmp_path / "repository-recovery-race.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"

    engine = create_async_engine(
        database_url,
        connect_args={"timeout": 30},
    )

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        router = WebhookRouter(MagicMock(), MagicMock())

        async def process_delivery(action, repositories):
            payload = {
                "action": action,
                "installation": {
                    "id": 90002,
                    "account": {
                        "id": 80002,
                        "login": "recovery-org",
                        "type": "Organization",
                    },
                },
                "repositories_added": repositories,
                "repositories_removed": [],
            }

            async with session_factory() as session:
                return await router._handle_installation_repositories(
                    payload,
                    session,
                )

        results = await asyncio.gather(
            process_delivery("added", [{"name": "repo-one"}]),
            process_delivery("added", [{"name": "repo-two"}]),
        )

        assert results == [
            {"ok": True, "action": "added"},
            {"ok": True, "action": "added"},
        ]

        async with session_factory() as session:
            account_count = await session.scalar(
                select(func.count())
                .select_from(Account)
                .where(Account.github_installation_id == 90002)
            )

            assert account_count == 1

            account = await session.scalar(
                select(Account).where(
                    Account.github_installation_id == 90002
                )
            )
            assert account is not None

            repo_names = set(
                (
                    await session.scalars(
                        select(AccountRepo.repo_name).where(
                            AccountRepo.account_id == account.id
                        )
                    )
                ).all()
            )

            assert repo_names == {"repo-one", "repo-two"}

    finally:
        await engine.dispose()
