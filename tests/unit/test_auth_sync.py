# tests/unit/test_auth_sync.py
#
# Issue #97 — app/auth/sync.py had no tests at all: the GitHub API sync path
# that decides which organizations a logged-in user can see in the dashboard
# was completely uncovered.

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.auth.session import encrypt_token
from app.auth.sync import (
    _SYNC_CACHE,
    clear_sync_cache,
    get_user_authorized_accounts,
)
from app.db.models import Account, AccountUser, User, UserOAuthToken


def make_github_client_mock(status_code=200, json_data=None):
    """Build a mock for `async with httpx.AsyncClient() as client: await client.get(...)`."""
    response = MagicMock()
    response.status_code = status_code
    response.json = MagicMock(return_value=json_data or {})

    client = MagicMock()
    client.get = AsyncMock(return_value=response)

    ctx_manager = MagicMock()
    ctx_manager.__aenter__ = AsyncMock(return_value=client)
    ctx_manager.__aexit__ = AsyncMock(return_value=False)
    return ctx_manager


@pytest.mark.asyncio
async def test_user_with_no_authorized_accounts_returns_empty_list(db):
    user = User(github_user_id=1, github_login="alice")
    db.add(user)
    await db.commit()
    await db.refresh(user)

    result = await get_user_authorized_accounts(user, db)

    assert result == []
    assert _SYNC_CACHE[user.id][1] == []


@pytest.mark.asyncio
async def test_valid_token_syncs_installations_and_creates_account_user(db):
    """User with a valid OAuth token gets installations synced from the
    GitHub API, and a new AccountUser row is created for the matching
    account."""
    user = User(github_user_id=2, github_login="bob")
    db.add(user)
    await db.commit()
    await db.refresh(user)

    account = Account(github_installation_id=555, org_login="hiero", plan_tier="free")
    db.add(account)
    await db.commit()
    await db.refresh(account)

    token = UserOAuthToken(
        user_id=user.id, encrypted_access_token=encrypt_token("gho_realtoken")
    )
    db.add(token)
    await db.commit()

    ctx_manager = make_github_client_mock(
        status_code=200, json_data={"installations": [{"id": 555}]}
    )
    with patch("app.auth.sync.httpx.AsyncClient", return_value=ctx_manager):
        result = await get_user_authorized_accounts(user, db)

    assert len(result) == 1
    assert result[0]["org_login"] == "hiero"

    from sqlalchemy import select
    au = (
        await db.execute(
            select(AccountUser).where(
                AccountUser.account_id == account.id, AccountUser.user_id == user.id
            )
        )
    ).scalar_one()
    assert au.authorized is True


@pytest.mark.asyncio
async def test_existing_account_user_row_is_updated_to_authorized(db):
    """An AccountUser row that already exists but was previously
    unauthorized must be flipped to authorized=True, not duplicated."""
    user = User(github_user_id=3, github_login="carol")
    db.add(user)
    await db.commit()
    await db.refresh(user)

    account = Account(github_installation_id=777, org_login="hiero-org", plan_tier="free")
    db.add(account)
    await db.commit()
    await db.refresh(account)

    existing_au = AccountUser(account_id=account.id, user_id=user.id, authorized=False)
    db.add(existing_au)

    token = UserOAuthToken(
        user_id=user.id, encrypted_access_token=encrypt_token("gho_realtoken")
    )
    db.add(token)
    await db.commit()

    ctx_manager = make_github_client_mock(
        status_code=200, json_data={"installations": [{"id": 777}]}
    )
    with patch("app.auth.sync.httpx.AsyncClient", return_value=ctx_manager):
        result = await get_user_authorized_accounts(user, db)

    assert len(result) == 1

    from sqlalchemy import select
    rows = (
        await db.execute(
            select(AccountUser).where(
                AccountUser.account_id == account.id, AccountUser.user_id == user.id
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].authorized is True


@pytest.mark.asyncio
async def test_expired_token_falls_back_to_db_query(db):
    """A GitHub API call that fails (expired/invalid token) must not raise —
    the function falls back to whatever is already authorized in the DB."""
    user = User(github_user_id=4, github_login="dave")
    db.add(user)
    await db.commit()
    await db.refresh(user)

    account = Account(github_installation_id=888, org_login="fallback-org", plan_tier="free")
    db.add(account)
    await db.commit()
    await db.refresh(account)

    db.add(AccountUser(account_id=account.id, user_id=user.id, authorized=True))

    token = UserOAuthToken(
        user_id=user.id, encrypted_access_token=encrypt_token("gho_realtoken")
    )
    db.add(token)
    await db.commit()

    ctx_manager = make_github_client_mock(status_code=401)
    with patch("app.auth.sync.httpx.AsyncClient", return_value=ctx_manager):
        result = await get_user_authorized_accounts(user, db)

    assert len(result) == 1
    assert result[0]["org_login"] == "fallback-org"


@pytest.mark.asyncio
async def test_api_exception_during_sync_falls_back_to_db_query(db):
    """If the sync call itself raises (network error, decrypt failure,
    etc.), the broad except must swallow it and fall back to the DB."""
    user = User(github_user_id=5, github_login="erin")
    db.add(user)
    await db.commit()
    await db.refresh(user)

    account = Account(github_installation_id=999, org_login="resilient-org", plan_tier="free")
    db.add(account)
    await db.commit()
    await db.refresh(account)

    db.add(AccountUser(account_id=account.id, user_id=user.id, authorized=True))

    token = UserOAuthToken(
        user_id=user.id, encrypted_access_token=encrypt_token("gho_realtoken")
    )
    db.add(token)
    await db.commit()

    with patch(
        "app.auth.sync.httpx.AsyncClient", side_effect=RuntimeError("network down")
    ):
        result = await get_user_authorized_accounts(user, db)

    assert len(result) == 1
    assert result[0]["org_login"] == "resilient-org"


@pytest.mark.asyncio
async def test_user_without_oauth_token_skips_api_call_uses_db(db):
    user = User(github_user_id=6, github_login="frank")
    db.add(user)
    await db.commit()
    await db.refresh(user)

    account = Account(github_installation_id=1010, org_login="no-token-org", plan_tier="free")
    db.add(account)
    await db.commit()
    await db.refresh(account)

    db.add(AccountUser(account_id=account.id, user_id=user.id, authorized=True))
    await db.commit()

    with patch("app.auth.sync.httpx.AsyncClient") as mock_client_cls:
        result = await get_user_authorized_accounts(user, db)
        mock_client_cls.assert_not_called()

    assert len(result) == 1
    assert result[0]["org_login"] == "no-token-org"


@pytest.mark.asyncio
async def test_cache_hit_returns_cached_accounts_without_db_query(db):
    user = User(github_user_id=7, github_login="grace")
    db.add(user)
    await db.commit()
    await db.refresh(user)

    account = Account(github_installation_id=1111, org_login="cached-org", plan_tier="free")
    db.add(account)
    await db.commit()
    await db.refresh(account)

    db.add(AccountUser(account_id=account.id, user_id=user.id, authorized=True))
    await db.commit()

    # First call — cache miss, hits the DB and populates the cache.
    first = await get_user_authorized_accounts(user, db)
    assert len(first) == 1

    # Second call — cache hit. Patch db.execute to fail loudly if touched.
    with patch.object(
        db, "execute", side_effect=AssertionError("should not query DB on cache hit")
    ):
        second = await get_user_authorized_accounts(user, db)

    assert second == first


@pytest.mark.asyncio
async def test_force_sync_bypasses_cache(db):
    user = User(github_user_id=8, github_login="heidi")
    db.add(user)
    await db.commit()
    await db.refresh(user)

    account = Account(github_installation_id=1212, org_login="force-sync-org", plan_tier="free")
    db.add(account)
    await db.commit()
    await db.refresh(account)

    db.add(AccountUser(account_id=account.id, user_id=user.id, authorized=True))
    await db.commit()

    first = await get_user_authorized_accounts(user, db)
    assert len(first) == 1

    # force_sync=True must re-query the DB even though the cache is warm.
    second = await get_user_authorized_accounts(user, db, force_sync=True)
    assert second == first


@pytest.mark.asyncio
async def test_stale_cache_entry_triggers_resync(db):
    user = User(github_user_id=9, github_login="ivan")
    db.add(user)
    await db.commit()
    await db.refresh(user)

    account = Account(github_installation_id=1313, org_login="stale-cache-org", plan_tier="free")
    db.add(account)
    await db.commit()
    await db.refresh(account)

    db.add(AccountUser(account_id=account.id, user_id=user.id, authorized=True))
    await db.commit()

    await get_user_authorized_accounts(user, db)

    # Manually age the cache entry beyond the TTL.
    cached_time, cached_accounts = _SYNC_CACHE[user.id]
    _SYNC_CACHE[user.id] = (cached_time - 10_000, cached_accounts)

    result = await get_user_authorized_accounts(user, db)
    assert len(result) == 1
    assert result[0]["org_login"] == "stale-cache-org"


@pytest.mark.asyncio
async def test_formatted_accounts_include_their_repos(db):
    user = User(github_user_id=10, github_login="judy")
    db.add(user)
    await db.commit()
    await db.refresh(user)

    account = Account(github_installation_id=1414, org_login="repo-org", plan_tier="free")
    db.add(account)
    await db.commit()
    await db.refresh(account)

    db.add(AccountUser(account_id=account.id, user_id=user.id, authorized=True))
    from app.db.models import AccountRepo
    db.add(AccountRepo(account_id=account.id, repo_name="sdk-js"))
    db.add(AccountRepo(account_id=account.id, repo_name="sdk-python"))
    await db.commit()

    result = await get_user_authorized_accounts(user, db)

    assert len(result) == 1
    assert sorted(result[0]["repos"]) == ["sdk-js", "sdk-python"]


def test_clear_sync_cache_empties_the_cache():
    _SYNC_CACHE[123] = (time.time(), [{"id": 1}])
    clear_sync_cache()
    assert _SYNC_CACHE == {}
