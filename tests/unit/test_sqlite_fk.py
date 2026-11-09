from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import delete, event, select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.database import Base, _set_sqlite_pragma, engine
from app.db.models import (
    Account,
    AccountRepo,
    AccountUser,
    Session,
    User,
    UserOAuthToken,
)


@pytest.mark.asyncio
async def test_sqlite_foreign_keys_and_cascade_deletes():
    test_engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
    )

    event.listen(
        test_engine.sync_engine,
        "connect",
        _set_sqlite_pragma,
    )

    try:
        # The production listener checks the application's engine dialect.
        # Force its SQLite branch while exercising an isolated test engine.
        with patch.object(engine.dialect, "name", "sqlite"):
            async with test_engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)

            session_factory = async_sessionmaker(
                test_engine,
                class_=AsyncSession,
                expire_on_commit=False,
            )

            async with session_factory() as db:
                # Verify foreign-key enforcement on the actual connection.
                result = await db.execute(text("PRAGMA foreign_keys"))
                assert result.scalar_one() == 1

                # Create a user and all relevant dependent records.
                user = User(
                    github_user_id=1001,
                    github_login="user-delete-test",
                )
                account = Account(
                    github_installation_id=2001,
                    org_login="user-delete-org",
                )

                db.add_all([user, account])
                await db.flush()

                token = UserOAuthToken(
                    user_id=user.id,
                    encrypted_access_token="encrypted-test-token",
                )
                user_session = Session(
                    id="user-delete-session",
                    user_id=user.id,
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                )
                account_user = AccountUser(
                    account_id=account.id,
                    user_id=user.id,
                )

                db.add_all([token, user_session, account_user])

                # Create a separate account and user to test account deletion.
                account_to_delete = Account(
                    github_installation_id=2002,
                    org_login="account-delete-org",
                )
                user_for_account = User(
                    github_user_id=1002,
                    github_login="account-delete-test",
                )

                db.add_all([account_to_delete, user_for_account])
                await db.flush()

                account_user_to_delete = AccountUser(
                    account_id=account_to_delete.id,
                    user_id=user_for_account.id,
                )
                account_repo = AccountRepo(
                    account_id=account_to_delete.id,
                    repo_name="test-repository",
                )

                db.add_all([account_user_to_delete, account_repo])
                await db.commit()

                user_id = user.id
                account_id = account_to_delete.id

                # Delete the user using SQL DELETE so the database-level
                # ON DELETE CASCADE behavior is exercised.
                await db.execute(
                    delete(User).where(User.id == user_id)
                )
                await db.commit()

                assert (
                    await db.execute(
                        select(UserOAuthToken.id).where(
                            UserOAuthToken.user_id == user_id
                        )
                    )
                ).scalars().all() == []

                assert (
                    await db.execute(
                        select(Session.id).where(
                            Session.user_id == user_id
                        )
                    )
                ).scalars().all() == []

                assert (
                    await db.execute(
                        select(AccountUser.id).where(
                            AccountUser.user_id == user_id
                        )
                    )
                ).scalars().all() == []

                # Delete the account and verify its dependent records.
                await db.execute(
                    delete(Account).where(Account.id == account_id)
                )
                await db.commit()

                assert (
                    await db.execute(
                        select(AccountUser.id).where(
                            AccountUser.account_id == account_id
                        )
                    )
                ).scalars().all() == []

                assert (
                    await db.execute(
                        select(AccountRepo.id).where(
                            AccountRepo.account_id == account_id
                        )
                    )
                ).scalars().all() == []

    finally:
        await test_engine.dispose()
