# tests/unit/test_auth_sync.py
#
# Issue #97 — app/auth/sync.py had no tests at all: the GitHub API sync path
# that decides which organizations a logged-in user can see in the dashboard
# was completely uncovered.

import itertools
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

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


# ── Revocation on sync (#147) ─────────────────────────────────


def make_paged_github_client_mock(pages):
    """Like make_github_client_mock, but each call returns the next page.

    ``pages`` holds (status_code, installation_ids) tuples.
    """
    responses = []
    for status_code, ids in pages:
        response = MagicMock()
        response.status_code = status_code
        response.json = MagicMock(
            return_value={"installations": [{"id": i} for i in ids]}
        )
        responses.append(response)

    client = MagicMock()
    client.get = AsyncMock(side_effect=responses)

    ctx_manager = MagicMock()
    ctx_manager.__aenter__ = AsyncMock(return_value=client)
    ctx_manager.__aexit__ = AsyncMock(return_value=False)
    return ctx_manager, client


_github_user_ids = itertools.count(10_000)


async def user_with_accounts(db, login, installation_ids, *, authorized=True):
    """A user holding AccountUser rows for the given installations, plus a token."""
    user = User(github_user_id=next(_github_user_ids), github_login=login)
    db.add(user)
    await db.commit()
    await db.refresh(user)

    for inst_id in installation_ids:
        account = Account(
            github_installation_id=inst_id, org_login=f"org-{inst_id}", plan_tier="free"
        )
        db.add(account)
        await db.commit()
        await db.refresh(account)
        db.add(AccountUser(account_id=account.id, user_id=user.id, authorized=authorized))

    db.add(UserOAuthToken(user_id=user.id, encrypted_access_token=encrypt_token("gho_tok")))
    await db.commit()
    return user


async def authorized_installations(db, user):
    rows = await db.execute(
        select(Account.github_installation_id)
        .join(AccountUser, Account.id == AccountUser.account_id)
        .where(AccountUser.user_id == user.id, AccountUser.authorized.is_(True))
    )
    return sorted(rows.scalars().all())


async def sync(db, user, pages):
    ctx_manager, client = make_paged_github_client_mock(pages)
    with patch("app.auth.sync.httpx.AsyncClient", return_value=ctx_manager):
        result = await get_user_authorized_accounts(user, db, force_sync=True)
    return result, client


@pytest.mark.asyncio
async def test_installation_missing_from_github_is_revoked(db):
    user = await user_with_accounts(db, "erin", [555, 666])

    result, _ = await sync(db, user, [(200, [555])])

    assert [a["github_installation_id"] for a in result] == [555]
    assert await authorized_installations(db, user) == [555]


@pytest.mark.asyncio
async def test_removed_from_every_installation_revokes_all(db):
    """An empty list is a real answer: the user has no installations left."""
    user = await user_with_accounts(db, "frank", [555, 666])

    result, _ = await sync(db, user, [(200, [])])

    assert result == []
    assert await authorized_installations(db, user) == []


@pytest.mark.asyncio
async def test_restored_access_is_authorized_again(db):
    user = await user_with_accounts(db, "grace", [555], authorized=False)

    result, _ = await sync(db, user, [(200, [555])])

    assert [a["github_installation_id"] for a in result] == [555]
    assert await authorized_installations(db, user) == [555]


@pytest.mark.asyncio
async def test_installations_on_later_pages_are_not_revoked(db):
    """/user/installations is paginated; page one alone is not the full list."""
    from app.auth.sync import INSTALLATIONS_PER_PAGE

    user = await user_with_accounts(db, "heidi", [555, 666])
    first_page = [555] + list(range(10_000, 10_000 + INSTALLATIONS_PER_PAGE - 1))

    _, client = await sync(db, user, [(200, first_page), (200, [666])])

    assert await authorized_installations(db, user) == [555, 666]
    pages = [call.kwargs["params"]["page"] for call in client.get.await_args_list]
    assert pages == [1, 2]


@pytest.mark.asyncio
async def test_failed_later_page_grants_but_does_not_revoke(db):
    from app.auth.sync import INSTALLATIONS_PER_PAGE

    user = await user_with_accounts(db, "ivan", [666])
    await user_with_accounts(db, "other-owner", [555])  # creates account 555
    first_page = [555] + list(range(10_000, 10_000 + INSTALLATIONS_PER_PAGE - 1))

    await sync(db, user, [(200, first_page), (502, [])])

    # 555 (seen on page one) is granted; 666 might be on the page that failed.
    assert await authorized_installations(db, user) == [555, 666]


@pytest.mark.asyncio
async def test_page_cap_reached_does_not_revoke(db):
    from app.auth.sync import INSTALLATIONS_PER_PAGE, MAX_INSTALLATION_PAGES

    user = await user_with_accounts(db, "judy", [666])
    full_page = list(range(10_000, 10_000 + INSTALLATIONS_PER_PAGE))

    _, client = await sync(db, user, [(200, full_page)] * MAX_INSTALLATION_PAGES)

    assert client.get.await_count == MAX_INSTALLATION_PAGES
    assert await authorized_installations(db, user) == [666]


@pytest.mark.asyncio
async def test_revocation_only_affects_the_syncing_user(db):
    alice = await user_with_accounts(db, "mallory", [555])
    bob = User(github_user_id=424242, github_login="trent")
    db.add(bob)
    await db.commit()
    await db.refresh(bob)
    account_id = (await db.execute(
        select(Account.id).where(Account.github_installation_id == 555)
    )).scalar_one()
    db.add(AccountUser(account_id=account_id, user_id=bob.id, authorized=True))
    await db.commit()

    await sync(db, alice, [(200, [])])

    assert await authorized_installations(db, alice) == []
    assert await authorized_installations(db, bob) == [555]


@pytest.mark.asyncio
async def test_sync_query_count_does_not_grow_with_installations(db):
    """One query per installation (N+1) would make a large sync slow."""
    from sqlalchemy import event

    async def count_sync_queries(login, installation_ids):
        user = await user_with_accounts(db, login, installation_ids)
        clear_sync_cache()
        statements = []

        def record(conn, cursor, statement, *args):
            statements.append(statement)

        engine = db.bind.sync_engine
        event.listen(engine, "before_cursor_execute", record)
        try:
            await sync(db, user, [(200, installation_ids)])
        finally:
            event.remove(engine, "before_cursor_execute", record)
        return len(statements)

    small = await count_sync_queries("few-orgs", [700 + i for i in range(3)])
    large = await count_sync_queries("many-orgs", [800 + i for i in range(40)])

    assert large == small
