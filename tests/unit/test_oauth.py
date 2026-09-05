from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.session import (
    SESSION_COOKIE_NAME,
    create_db_session,
    unsign_session_id,
)
from app.db.database import get_db
from app.db.models import Account, AccountRepo, AccountUser, Session, User
from app.main import app
from app.utils.settings import settings


@pytest.mark.asyncio
async def test_oauth_login_dev_creates_demo_user_and_session(
    db: AsyncSession,
):
    app.dependency_overrides[get_db] = lambda: db

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/auth/login")

        assert response.status_code == 302
        assert response.headers["location"] == "/"

        result = await db.execute(
            select(User).where(User.github_login == "demo_maintainer")
        )
        user = result.scalar_one_or_none()

        assert user is not None
        assert user.github_user_id == 99999
        assert user.github_email == "maintainer@hiero.local"

        account_result = await db.execute(
            select(Account).where(Account.org_login == "AnthropicBots")
        )
        account = account_result.scalar_one_or_none()

        assert account is not None
        assert account.github_installation_id == 12345
        assert account.github_account_id == 67890

        account_user_result = await db.execute(
            select(AccountUser).where(
                AccountUser.account_id == account.id,
                AccountUser.user_id == user.id,
            )
        )
        account_user = account_user_result.scalar_one_or_none()

        assert account_user is not None
        assert account_user.authorized is True

        repo_result = await db.execute(
            select(AccountRepo).where(
                AccountRepo.account_id == account.id,
                AccountRepo.repo_name == "hiero-bot-py",
            )
        )
        repo = repo_result.scalar_one_or_none()

        assert repo is not None

        session_result = await db.execute(
            select(Session).where(Session.user_id == user.id)
        )
        session = session_result.scalar_one_or_none()

        assert session is not None

        set_cookie = response.headers.get("set-cookie")
        assert SESSION_COOKIE_NAME in set_cookie
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_oauth_login_production_redirects_to_github(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    app.dependency_overrides[get_db] = lambda: db

    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "github_oauth_client_id", "test-client-id")
    monkeypatch.setattr(settings, "github_oauth_client_secret", "test-client-secret")

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://test",
        ) as client:
            response = await client.get(
                "/auth/login",
                follow_redirects=False,
            )

        assert response.status_code == 307

        location = response.headers["location"]
        assert location.startswith(
            "https://github.com/login/oauth/authorize?"
        )
        assert "client_id=test-client-id" in location
        assert "scope=read%3Aorg" in location or "scope=read%3Aorg%2Cuser%3Aemail" in location

        set_cookie = response.headers.get("set-cookie")
        assert "oauth_state=" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "Secure" in set_cookie
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_oauth_login_without_client_id_returns_501(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    app.dependency_overrides[get_db] = lambda: db
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "github_oauth_client_id", None)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://test",
        ) as client:
            response = await client.get(
                "/auth/login",
                follow_redirects=False,
            )

        assert response.status_code == 501
        assert response.json()["detail"] == (
            "GitHub OAuth Client ID is not configured on this server."
        )
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_oauth_callback_rejects_invalid_state(
    db: AsyncSession,
):
    app.dependency_overrides[get_db] = lambda: db

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            client.cookies.set("oauth_state", "saved-state")

            response = await client.get(
                "/auth/callback",
                params={
                    "code": "test-code",
                    "state": "different-state",
                },
            )

        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid OAuth state parameter"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_oauth_callback_rejects_missing_code(
    db: AsyncSession,
):
    app.dependency_overrides[get_db] = lambda: db

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            client.cookies.set("oauth_state", "valid-state")

            response = await client.get(
                "/auth/callback",
                params={
                    "state": "valid-state",
                },
            )

        assert response.status_code == 400
        assert response.json()["detail"] == "Missing code parameter"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_oauth_callback_handles_token_exchange_failure(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    app.dependency_overrides[get_db] = lambda: db

    mock_client = AsyncMock()
    mock_client.fetch_token = AsyncMock(
        side_effect=Exception("token exchange failed")
    )

    monkeypatch.setattr(
        "app.auth.oauth.AsyncOAuth2Client",
        lambda **kwargs: mock_client,
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            client.cookies.set("oauth_state", "valid-state")

            response = await client.get(
                "/auth/callback",
                params={
                    "code": "test-code",
                    "state": "valid-state",
                },
            )

        assert response.status_code == 400
        assert response.json()["detail"] == (
            "GitHub OAuth error: token exchange failed"
        )
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_oauth_callback_rejects_missing_access_token(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    app.dependency_overrides[get_db] = lambda: db

    mock_client = AsyncMock()
    mock_client.fetch_token = AsyncMock(
        return_value={
            "token_type": "bearer",
        }
    )

    monkeypatch.setattr(
        "app.auth.oauth.AsyncOAuth2Client",
        lambda **kwargs: mock_client,
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            client.cookies.set("oauth_state", "valid-state")

            response = await client.get(
                "/auth/callback",
                params={
                    "code": "test-code",
                    "state": "valid-state",
                },
            )

        assert response.status_code == 400
        assert response.json()["detail"] == (
            "No access token returned from GitHub"
        )
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_oauth_callback_handles_github_profile_failure(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    app.dependency_overrides[get_db] = lambda: db

    mock_client = AsyncMock()
    mock_client.fetch_token = AsyncMock(
        return_value={
            "access_token": "test-access-token",
        }
    )
    mock_client.get = AsyncMock(
        return_value=Response(
            status_code=500,
            json={
                "message": "Internal Server Error",
            },
            request=None,
        )
    )

    monkeypatch.setattr(
        "app.auth.oauth.AsyncOAuth2Client",
        lambda **kwargs: mock_client,
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            client.cookies.set("oauth_state", "valid-state")

            response = await client.get(
                "/auth/callback",
                params={
                    "code": "test-code",
                    "state": "valid-state",
                },
            )

        assert response.status_code == 500
        assert response.json()["detail"] == (
            "Failed to fetch user profile from GitHub"
        )
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_oauth_callback_success_creates_new_user_token_and_session(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    app.dependency_overrides[get_db] = lambda: db

    mock_client = AsyncMock()
    mock_client.fetch_token = AsyncMock(
        return_value={
            "access_token": "test-access-token",
            "scope": "read:org,user:email",
        }
    )
    mock_client.get = AsyncMock(
        return_value=Response(
            status_code=200,
            json={
                "id": 123456,
                "login": "test-user",
                "email": "test@example.com",
                "avatar_url": "https://avatars.githubusercontent.com/u/123456",
            },
            request=None,
        )
    )

    monkeypatch.setattr(
        "app.auth.oauth.AsyncOAuth2Client",
        lambda **kwargs: mock_client,
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            client.cookies.set("oauth_state", "valid-state")

            response = await client.get(
                "/auth/callback",
                params={
                    "code": "test-code",
                    "state": "valid-state",
                },
                follow_redirects=False,
            )

        assert response.status_code == 302
        assert response.headers["location"] == "/"

        mock_client.fetch_token.assert_awaited_once_with(
            "https://github.com/login/oauth/access_token",
            code="test-code",
            state="valid-state",
        )
        mock_client.get.assert_awaited_once_with(
            "https://api.github.com/user",
            headers={"User-Agent": "Hiero-Bot-Py"},
        )

        user_result = await db.execute(
            select(User).where(User.github_user_id == 123456)
        )
        user = user_result.scalar_one_or_none()

        assert user is not None
        assert user.github_login == "test-user"
        assert user.github_email == "test@example.com"
        assert (
            user.avatar_url
            == "https://avatars.githubusercontent.com/u/123456"
        )
        assert user.last_login_at is not None

        from app.db.models import UserOAuthToken

        token_result = await db.execute(
            select(UserOAuthToken).where(UserOAuthToken.user_id == user.id)
        )
        user_token = token_result.scalar_one_or_none()

        assert user_token is not None
        assert user_token.encrypted_access_token != "test-access-token"
        assert user_token.scope == "read:org,user:email"

        session_result = await db.execute(
            select(Session).where(Session.user_id == user.id)
        )
        session = session_result.scalar_one_or_none()

        assert session is not None
        assert session.id

        session_cookies = response.headers.get_list("set-cookie")
        assert any(
            SESSION_COOKIE_NAME in cookie for cookie in session_cookies
        )

        state_cookie = response.headers.get_list("set-cookie")
        assert any("oauth_state" in cookie for cookie in state_cookie)
        assert any("Max-Age=0" in cookie for cookie in state_cookie)

        session_cookie_value = None
        for header in response.headers.get_list("set-cookie"):
            if header.startswith(f"{SESSION_COOKIE_NAME}="):
                session_cookie_value = header.split(";", 1)[0].split("=", 1)[1]
                break

        assert session_cookie_value is not None
        assert unsign_session_id(session_cookie_value) == session.id
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_oauth_callback_existing_user_updates_user_and_token(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    app.dependency_overrides[get_db] = lambda: db

    user = User(
        github_user_id=123456,
        github_login="old-user",
        github_email="old@example.com",
        avatar_url="old-avatar",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    mock_client = AsyncMock()
    mock_client.fetch_token = AsyncMock(
        return_value={
            "access_token": "new-access-token",
            "scope": "read:org,user:email",
        }
    )
    mock_client.get = AsyncMock(
        return_value=Response(
            status_code=200,
            json={
                "id": 123456,
                "login": "updated-user",
                "email": "updated@example.com",
                "avatar_url": "new-avatar",
            },
            request=None,
        )
    )

    monkeypatch.setattr(
        "app.auth.oauth.AsyncOAuth2Client",
        lambda **kwargs: mock_client,
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            client.cookies.set("oauth_state", "valid-state")

            response = await client.get(
                "/auth/callback",
                params={
                    "code": "test-code",
                    "state": "valid-state",
                },
                follow_redirects=False,
            )

        assert response.status_code == 302

        await db.refresh(user)

        assert user.github_login == "updated-user"
        assert user.github_email == "updated@example.com"
        assert user.avatar_url == "new-avatar"
        assert user.last_login_at is not None
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_oauth_logout_deletes_session_and_cookie(
    db: AsyncSession,
):
    app.dependency_overrides[get_db] = lambda: db

    user = User(
        github_user_id=123456,
        github_login="logout-user",
        github_email="logout@example.com",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    session_id, cookie_val = await create_db_session(db, user.id)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            client.cookies.set(SESSION_COOKIE_NAME, cookie_val)

            response = await client.get(
                "/auth/logout",
                follow_redirects=False,
            )

        assert response.status_code == 302
        assert response.headers["location"] == "/"

        result = await db.execute(
            select(Session).where(Session.id == session_id)
        )
        session = result.scalar_one_or_none()

        assert session is None

        set_cookie = response.headers.get("set-cookie")
        assert SESSION_COOKIE_NAME in set_cookie
        assert "Max-Age=0" in set_cookie
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_oauth_logout_without_session_cookie_redirects_and_deletes_cookie(
    db: AsyncSession,
):
    app.dependency_overrides[get_db] = lambda: db

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/auth/logout",
                follow_redirects=False,
            )

        assert response.status_code == 302
        assert response.headers["location"] == "/"

        set_cookie = response.headers.get("set-cookie")
        assert SESSION_COOKIE_NAME in set_cookie
        assert "Max-Age=0" in set_cookie
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_oauth_callback_existing_user_updates_existing_token(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    app.dependency_overrides[get_db] = lambda: db

    from app.db.models import UserOAuthToken

    user = User(
        github_user_id=123456,
        github_login="old-user",
        github_email="old@example.com",
        avatar_url="old-avatar",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    token = UserOAuthToken(
        user_id=user.id,
        encrypted_access_token="old-encrypted-token",
        scope="read:org",
    )
    db.add(token)
    await db.commit()
    await db.refresh(token)

    mock_client = AsyncMock()
    mock_client.fetch_token = AsyncMock(
        return_value={
            "access_token": "new-access-token",
            "scope": "read:org,user:email",
        }
    )
    mock_client.get = AsyncMock(
        return_value=Response(
            status_code=200,
            json={
                "id": 123456,
                "login": "updated-user",
                "email": "updated@example.com",
                "avatar_url": "new-avatar",
            },
            request=None,
        )
    )

    monkeypatch.setattr(
        "app.auth.oauth.AsyncOAuth2Client",
        lambda **kwargs: mock_client,
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            client.cookies.set("oauth_state", "valid-state")

            response = await client.get(
                "/auth/callback",
                params={
                    "code": "test-code",
                    "state": "valid-state",
                },
                follow_redirects=False,
            )

        assert response.status_code == 302

        await db.refresh(token)

        assert token.encrypted_access_token != "old-encrypted-token"
        assert token.scope == "read:org,user:email"
        assert token.updated_at is not None
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_oauth_logout_with_invalid_session_cookie_redirects_and_deletes_cookie(
    db: AsyncSession,
):
    app.dependency_overrides[get_db] = lambda: db

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            client.cookies.set(SESSION_COOKIE_NAME, "invalid-session-cookie")

            response = await client.get(
                "/auth/logout",
                follow_redirects=False,
            )

        assert response.status_code == 302
        assert response.headers["location"] == "/"

        set_cookie = response.headers.get("set-cookie")
        assert SESSION_COOKIE_NAME in set_cookie
        assert "Max-Age=0" in set_cookie
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_oauth_login_dev_reuses_existing_user_account_and_relationships(
    db: AsyncSession,
):
    app.dependency_overrides[get_db] = lambda: db

    user = User(
        github_user_id=99999,
        github_login="demo_maintainer",
        github_email="old@hiero.local",
        avatar_url="old-avatar",
    )
    db.add(user)

    account = Account(
        github_installation_id=12345,
        github_account_id=67890,
        org_login="AnthropicBots",
        account_type="Organization",
        plan_tier="free",
    )
    db.add(account)

    await db.commit()
    await db.refresh(user)
    await db.refresh(account)

    account_user = AccountUser(
        account_id=account.id,
        user_id=user.id,
        authorized=True,
    )
    account_repo = AccountRepo(
        account_id=account.id,
        repo_name="hiero-bot-py",
    )
    db.add(account_user)
    db.add(account_repo)
    await db.commit()

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/auth/login",
                follow_redirects=False,
            )

        assert response.status_code == 302
        assert response.headers["location"] == "/"

        users = (
            await db.execute(
                select(User).where(User.github_login == "demo_maintainer")
            )
        ).scalars().all()
        assert len(users) == 1

        accounts = (
            await db.execute(
                select(Account).where(Account.org_login == "AnthropicBots")
            )
        ).scalars().all()
        assert len(accounts) == 1

        account_users = (
            await db.execute(
                select(AccountUser).where(
                    AccountUser.account_id == account.id,
                    AccountUser.user_id == user.id,
                )
            )
        ).scalars().all()
        assert len(account_users) == 1

        repos = (
            await db.execute(
                select(AccountRepo).where(
                    AccountRepo.account_id == account.id,
                    AccountRepo.repo_name == "hiero-bot-py",
                )
            )
        ).scalars().all()
        assert len(repos) == 1
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_oauth_login_dev_with_mock_client_id(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    app.dependency_overrides[get_db] = lambda: db
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(
        settings,
        "github_oauth_client_id",
        "mock-client-id",
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/auth/login",
                follow_redirects=False,
            )

        assert response.status_code == 302
        assert response.headers["location"] == "/"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_oauth_login_dev_direct_handler_creates_demo_data(
    db: AsyncSession,
):
    app.dependency_overrides[get_db] = lambda: db

    from fastapi import Request

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/auth/login",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 123),
        }
    )

    try:
        from app.auth.oauth import github_oauth_login

        response = await github_oauth_login(request, db)

        assert response.status_code == 302
        assert response.headers["location"] == "/"

        user_result = await db.execute(
            select(User).where(User.github_login == "demo_maintainer")
        )
        user = user_result.scalar_one()

        account_result = await db.execute(
            select(Account).where(Account.org_login == "AnthropicBots")
        )
        account = account_result.scalar_one()

        account_user_result = await db.execute(
            select(AccountUser).where(
                AccountUser.account_id == account.id,
                AccountUser.user_id == user.id,
            )
        )
        assert account_user_result.scalar_one_or_none() is not None

        repo_result = await db.execute(
            select(AccountRepo).where(
                AccountRepo.account_id == account.id,
                AccountRepo.repo_name == "hiero-bot-py",
            )
        )
        assert repo_result.scalar_one_or_none() is not None
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_oauth_callback_direct_handler_creates_new_user_token_and_session(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    from fastapi import Request

    from app.auth.oauth import github_oauth_callback

    app.dependency_overrides[get_db] = lambda: db

    mock_client = AsyncMock()
    mock_client.fetch_token = AsyncMock(
        return_value={
            "access_token": "direct-test-access-token",
            "scope": "read:org,user:email",
        }
    )
    mock_client.get = AsyncMock(
        return_value=Response(
            status_code=200,
            json={
                "id": 987654,
                "login": "direct-test-user",
                "email": "direct@example.com",
                "avatar_url": "https://avatars.githubusercontent.com/u/987654",
            },
            request=None,
        )
    )

    monkeypatch.setattr(
        "app.auth.oauth.AsyncOAuth2Client",
        lambda **kwargs: mock_client,
    )

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/auth/callback",
            "headers": [
                (b"cookie", b"oauth_state=valid-state"),
            ],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 123),
        }
    )

    try:
        response = await github_oauth_callback(
            request,
            code="direct-test-code",
            state="valid-state",
            db=db,
        )

        assert response.status_code == 302
        assert response.headers["location"] == "/"

        session_cookies = response.headers.getlist("set-cookie")
        assert any(
            SESSION_COOKIE_NAME in cookie for cookie in session_cookies
        )

        user_result = await db.execute(
            select(User).where(User.github_user_id == 987654)
        )
        user = user_result.scalar_one_or_none()

        assert user is not None
        assert user.github_login == "direct-test-user"

        from app.db.models import UserOAuthToken

        token_result = await db.execute(
            select(UserOAuthToken).where(UserOAuthToken.user_id == user.id)
        )
        token = token_result.scalar_one_or_none()

        assert token is not None
        assert token.encrypted_access_token != "direct-test-access-token"

        session_result = await db.execute(
            select(Session).where(Session.user_id == user.id)
        )
        session = session_result.scalar_one_or_none()

        assert session is not None
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_oauth_login_dev_direct_handler_reuses_existing_user_and_account(
    db: AsyncSession,
):
    from fastapi import Request

    from app.auth.oauth import github_oauth_login

    app.dependency_overrides[get_db] = lambda: db

    user = User(
        github_user_id=99999,
        github_login="demo_maintainer",
        github_email="maintainer@hiero.local",
        avatar_url="existing-avatar",
    )
    db.add(user)

    account = Account(
        github_installation_id=12345,
        github_account_id=67890,
        org_login="AnthropicBots",
        account_type="Organization",
        plan_tier="free",
    )
    db.add(account)

    await db.commit()
    await db.refresh(user)
    await db.refresh(account)

    db.add(
        AccountUser(
            account_id=account.id,
            user_id=user.id,
            authorized=True,
        )
    )
    db.add(
        AccountRepo(
            account_id=account.id,
            repo_name="hiero-bot-py",
        )
    )
    await db.commit()

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/auth/login",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 123),
        }
    )

    try:
        response = await github_oauth_login(request, db)

        assert response.status_code == 302
        assert response.headers["location"] == "/"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_oauth_callback_direct_handler_updates_existing_user_and_token(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    from fastapi import Request

    from app.auth.oauth import github_oauth_callback
    from app.db.models import UserOAuthToken

    app.dependency_overrides[get_db] = lambda: db

    user = User(
        github_user_id=987654,
        github_login="old-direct-user",
        github_email="old-direct@example.com",
        avatar_url="old-avatar",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    token = UserOAuthToken(
        user_id=user.id,
        encrypted_access_token="old-direct-token",
        scope="read:org",
    )
    db.add(token)
    await db.commit()
    await db.refresh(token)

    mock_client = AsyncMock()
    mock_client.fetch_token = AsyncMock(
        return_value={
            "access_token": "new-direct-token",
            "scope": "read:org,user:email",
        }
    )
    mock_client.get = AsyncMock(
        return_value=Response(
            status_code=200,
            json={
                "id": 987654,
                "login": "updated-direct-user",
                "email": "updated-direct@example.com",
                "avatar_url": "updated-avatar",
            },
            request=None,
        )
    )

    monkeypatch.setattr(
        "app.auth.oauth.AsyncOAuth2Client",
        lambda **kwargs: mock_client,
    )

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/auth/callback",
            "headers": [
                (b"cookie", b"oauth_state=valid-state"),
            ],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 123),
        }
    )

    try:
        response = await github_oauth_callback(
            request,
            code="direct-test-code",
            state="valid-state",
            db=db,
        )

        assert response.status_code == 302

        await db.refresh(user)
        await db.refresh(token)

        assert user.github_login == "updated-direct-user"
        assert user.github_email == "updated-direct@example.com"
        assert user.avatar_url == "updated-avatar"
        assert user.last_login_at is not None

        assert token.encrypted_access_token != "old-direct-token"
        assert token.scope == "read:org,user:email"
        assert token.updated_at is not None
    finally:
        app.dependency_overrides.pop(get_db, None)