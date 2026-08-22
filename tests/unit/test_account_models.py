# tests/unit/test_account_models.py

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Account, AccountRepo, AccountUser, User


@pytest.mark.asyncio
async def test_account_models_crud(db: AsyncSession):
    # Create account and user
    account = Account(github_installation_id=12345, org_login="test-org", plan_tier="free")
    user = User(github_user_id=999, github_login="test-user", github_email="test@example.com")

    db.add_all([account, user])
    await db.commit()
    await db.refresh(account)
    await db.refresh(user)

    assert account.id is not None
    assert user.id is not None

    # Link user to account and add repo
    account_user = AccountUser(account_id=account.id, user_id=user.id, authorized=True)
    account_repo = AccountRepo(account_id=account.id, repo_name="test-repo")

    db.add_all([account_user, account_repo])
    await db.commit()

    # Query back
    result_acc = await db.execute(select(Account).where(Account.org_login == "test-org"))
    fetched_acc = result_acc.scalar_one_or_none()
    assert fetched_acc is not None
    assert fetched_acc.github_installation_id == 12345
    assert fetched_acc.plan_tier == "free"

    result_au = await db.execute(select(AccountUser).where(AccountUser.account_id == account.id))
    fetched_au = result_au.scalar_one_or_none()
    assert fetched_au is not None
    assert fetched_au.user_id == user.id
    assert fetched_au.authorized is True
