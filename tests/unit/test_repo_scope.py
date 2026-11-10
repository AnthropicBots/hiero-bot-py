from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from app.auth.sync import get_user_authorized_accounts
from app.db.models import Account, AccountRepo, AccountUser, User
from app.utils.access import get_authorized_access, verify_repo_access


def test_allows_authorized_repo():
    verify_repo_access(
        "hiero",
        "sdk",
        {"hiero"},
        {"hiero": {"sdk"}},
    )


def test_denies_other_repo_in_same_org():
    with pytest.raises(HTTPException) as exc:
        verify_repo_access(
            "hiero",
            "secret",
            {"hiero"},
            {"hiero": {"sdk"}},
        )

    assert exc.value.status_code == 403


def test_denies_repo_when_inventory_is_empty():
    with pytest.raises(HTTPException) as exc:
        verify_repo_access(
            "hiero",
            "anything",
            {"hiero"},
            {"hiero": set()},
        )

    assert exc.value.status_code == 403


def test_denies_repo_when_inventory_is_missing():
    with pytest.raises(HTTPException) as exc:
        verify_repo_access(
            "hiero",
            "anything",
            {"hiero"},
            {},
        )

    assert exc.value.status_code == 403


def test_denies_unauthorized_owner():
    with pytest.raises(HTTPException) as exc:
        verify_repo_access(
            "other",
            "sdk",
            {"hiero"},
            {"hiero": {"sdk"}},
        )

    assert exc.value.status_code == 403


def test_repo_check_skips_missing_repo_filter():
    verify_repo_access(
        "hiero",
        None,
        {"hiero"},
        {"hiero": {"sdk"}},
    )


@pytest.mark.asyncio
async def test_removed_repository_is_not_authorized_with_stale_cache(db):
    user = User(github_user_id=9001, github_login="scope-test-user")
    db.add(user)
    await db.commit()
    await db.refresh(user)

    account = Account(
        github_installation_id=9001,
        org_login="hiero",
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

    repo = AccountRepo(
        account_id=account.id,
        repo_name="sdk-js",
    )
    db.add(repo)
    await db.commit()

    # Prime the cache while the repository is authorized.
    cached_accounts = await get_user_authorized_accounts(user, db)
    assert cached_accounts[0]["repos"] == ["sdk-js"]

    # Simulate a repository-removal webhook updating the database.
    await db.delete(repo)
    await db.commit()

    # The cached data still contains the removed repository.
    cached_accounts = await get_user_authorized_accounts(user, db)
    assert cached_accounts[0]["repos"] == ["sdk-js"]

    # Dashboard authorization must use the current database state.
    access = await get_authorized_access(user, db)

    assert access.owners == {"hiero"}
    assert access.repos == {}

    with pytest.raises(HTTPException) as exc:
        verify_repo_access(
            "hiero",
            "sdk-js",
            access.owners,
            access.repos,
        )

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_suspended_installation_is_not_authorized_with_stale_cache(db):
    user = User(github_user_id=9002, github_login="suspension-test-user")
    db.add(user)
    await db.commit()
    await db.refresh(user)

    account = Account(
        github_installation_id=9002,
        org_login="hiero",
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
        repo_name="sdk-js",
    ))
    await db.commit()

    # Prime the cache while the installation is active.
    cached_accounts = await get_user_authorized_accounts(user, db)
    assert cached_accounts[0]["repos"] == ["sdk-js"]

    # Simulate an installation suspension webhook.
    account.suspended_at = datetime.now(UTC)
    await db.commit()

    # The old cache still contains the installation.
    cached_accounts = await get_user_authorized_accounts(user, db)
    assert cached_accounts[0]["repos"] == ["sdk-js"]

    # Suspended installations must not grant dashboard access.
    access = await get_authorized_access(user, db)

    assert access.owners == set()
    assert access.repos == {}
