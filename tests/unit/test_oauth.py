from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.oauth import (
    github_oauth_callback,
    github_oauth_login,
    github_oauth_logout,
)
from app.auth.session import (
    SESSION_COOKIE_NAME,
    SESSION_EXPIRE_SECONDS,
    create_db_session,
    decrypt_token,
    unsign_session_id,
)
from app.db.database import get_db
from app.db.models import (
    Account,
    AccountRepo,
    AccountUser,
    Session,
    User,
    UserOAuthToken,
)
from app.main import app
from app.utils.settings import settings


def make_request(
    *,
    method: str = "GET",
    path: str,
    cookies: dict[str, str] | None = None,
) -> Request:
    cookie_header = b""
    if cookies:
        cookie_header = "; ".join(
            f"{key}={value}" for key, value in cookies.items()
        ).encode()

    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": (
                [(b"cookie", cookie_header)] if cookie_header else []
            ),
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 123),
        }
    )


def set_dev_environment(
    monkeypatch: pytest.MonkeyPatch,
    client_id: str | None = None,
) -> None:
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "github_oauth_client_id", client_id)


def set_production_environment(
    monkeypatch: pytest.MonkeyPatch,
    client_id: str | None = "test-client-id",
) -> None:
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "github_oauth_client_id", client_id)
    monkeypatch.setattr(
        settings,
        "github_oauth_client_secret",
        "test-client-secret",
    )


@pytest.mark.asyncio
async def test_oauth_login_dev_creates_demo_data_and_session(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    set_dev_environment(monkeypatch)

    request = make_request(
        path="/auth/login",
    )

    response = await github_oauth_login(request, db)

    assert response.status_code == 302
    assert response.headers["location"] == "/"

    user = (
        await db.execute(
            select(User).where(User.github_login == "demo_maintainer")
        )
    ).scalar_one()

    assert user.github_user_id == 99999
    assert user.github_email == "maintainer@hiero.local"
    assert user.avatar_url == (
        "https://avatars.githubusercontent.com/u/185269030?v=4"
    )
    assert user.last_login_at is not None

    account = (
        await db.execute(
            select(Account).where(Account.org_login == "AnthropicBots")
        )
    ).scalar_one()

    assert account.github_installation_id == 12345
    assert account.github_account_id == 67890
    assert account.account_type == "Organization"
    assert account.plan_tier == "free"

    account_user = (
        await db.execute(
            select(AccountUser).where(
                AccountUser.account_id == account.id,
                AccountUser.user_id == user.id,
            )
        )
    ).scalar_one()

    assert account_user.authorized is True

    repo = (
        await db.execute(
            select(AccountRepo).where(
                AccountRepo.account_id == account.id,
                AccountRepo.repo_name == "hiero-bot-py",
            )
        )
    ).scalar_one()

    assert repo.repo_name == "hiero-bot-py"

    session = (
        await db.execute(
            select(Session).where(Session.user_id == user.id)
        )
    ).scalar_one()

    assert session.user_id == user.id

    session_cookie = next(
        cookie
        for cookie in response.headers.getlist("set-cookie")
        if cookie.startswith(f"{SESSION_COOKIE_NAME}=")
    )

    assert f"Max-Age={SESSION_EXPIRE_SECONDS}" in session_cookie
    assert "HttpOnly" in session_cookie
    assert "SameSite=lax" in session_cookie
    assert "Secure" not in session_cookie


@pytest.mark.asyncio
async def test_oauth_login_dev_reuses_existing_data(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    set_dev_environment(monkeypatch)

    user = User(
        github_user_id=99999,
        github_login="demo_maintainer",
        github_email="old@hiero.local",
        avatar_url="old-avatar",
    )
    account = Account(
        github_installation_id=12345,
        github_account_id=67890,
        org_login="AnthropicBots",
        account_type="Organization",
        plan_tier="free",
    )

    db.add_all([user, account])
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

    db.add_all([account_user, account_repo])
    await db.commit()

    request = make_request(
        path="/auth/login",
    )

    response = await github_oauth_login(request, db)

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

    sessions = (
        await db.execute(
            select(Session).where(Session.user_id == user.id)
        )
    ).scalars().all()
    assert len(sessions) == 1


@pytest.mark.asyncio
async def test_oauth_login_dev_mock_client_id_uses_dev_flow(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    set_dev_environment(monkeypatch, "mock-client-id")

    request = make_request(
        path="/auth/login",
    )

    response = await github_oauth_login(request, db)

    assert response.status_code == 302
    assert response.headers["location"] == "/"

    user = (
        await db.execute(
            select(User).where(User.github_login == "demo_maintainer")
        )
    ).scalar_one()

    assert user.github_user_id == 99999


@pytest.mark.asyncio
async def test_oauth_login_production_creates_redirect_and_state_cookie(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    set_production_environment(monkeypatch)

    expected_state = "expected-oauth-state"
    expected_uri = (
        "https://github.com/login/oauth/authorize"
        "?client_id=test-client-id"
        "&state=expected-oauth-state"
    )

    mock_client = Mock()
    mock_client.create_authorization_url.return_value = (
        expected_uri,
        expected_state,
    )

    captured_kwargs = {}

    def make_client(**kwargs):
        captured_kwargs.update(kwargs)
        return mock_client

    monkeypatch.setattr(
        "app.auth.oauth.AsyncOAuth2Client",
        make_client,
    )

    request = make_request(
        path="/auth/login",
    )

    response = await github_oauth_login(request, db)

    assert response.status_code == 307
    assert response.headers["location"] == expected_uri

    assert captured_kwargs == {
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
        "scope": "read:org,user:email",
    }

    mock_client.create_authorization_url.assert_called_once_with(
        "https://github.com/login/oauth/authorize"
    )

    state_cookie = next(
        cookie
        for cookie in response.headers.getlist("set-cookie")
        if cookie.startswith("oauth_state=")
    )

    assert "oauth_state=expected-oauth-state" in state_cookie
    assert "Max-Age=600" in state_cookie
    assert "HttpOnly" in state_cookie
    assert "SameSite=lax" in state_cookie
    assert "Secure" in state_cookie


@pytest.mark.asyncio
async def test_oauth_login_without_client_id_returns_501(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    set_production_environment(monkeypatch, None)

    request = make_request(
        path="/auth/login",
    )

    with pytest.raises(Exception) as exc_info:
        await github_oauth_login(request, db)

    assert exc_info.value.status_code == 501
    assert exc_info.value.detail == (
        "GitHub OAuth Client ID is not configured on this server."
    )


@pytest.mark.parametrize(
    "cookie_state, query_state",
    [
        (None, "saved-state"),
        ("saved-state", None),
        ("saved-state", "different-state"),
    ],
    ids=[
        "missing-cookie",
        "missing-query-state",
        "mismatched-state",
    ],
)
@pytest.mark.asyncio
async def test_oauth_callback_rejects_invalid_state(
    db: AsyncSession,
    cookie_state: str | None,
    query_state: str | None,
):
    request = make_request(
        path="/auth/callback",
        cookies=(
            {"oauth_state": cookie_state}
            if cookie_state is not None
            else None
        ),
    )

    with pytest.raises(Exception) as exc_info:
        await github_oauth_callback(
            request=request,
            code="test-code",
            state=query_state,
            db=db,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Invalid OAuth state parameter"


@pytest.mark.asyncio
async def test_oauth_callback_rejects_missing_code(
    db: AsyncSession,
):
    request = make_request(
        path="/auth/callback",
        cookies={"oauth_state": "valid-state"},
    )

    with pytest.raises(Exception) as exc_info:
        await github_oauth_callback(
            request=request,
            code=None,
            state="valid-state",
            db=db,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Missing code parameter"


@pytest.mark.asyncio
async def test_oauth_callback_handles_token_exchange_failure(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    mock_client = AsyncMock()
    mock_client.fetch_token.side_effect = Exception(
        "token exchange failed"
    )

    monkeypatch.setattr(
        "app.auth.oauth.AsyncOAuth2Client",
        lambda **kwargs: mock_client,
    )

    request = make_request(
        path="/auth/callback",
        cookies={"oauth_state": "valid-state"},
    )

    with pytest.raises(Exception) as exc_info:
        await github_oauth_callback(
            request=request,
            code="test-code",
            state="valid-state",
            db=db,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == (
        "GitHub OAuth error: token exchange failed"
    )


@pytest.mark.asyncio
async def test_oauth_callback_rejects_missing_access_token(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    mock_client = AsyncMock()
    mock_client.fetch_token.return_value = {
        "token_type": "bearer",
    }

    monkeypatch.setattr(
        "app.auth.oauth.AsyncOAuth2Client",
        lambda **kwargs: mock_client,
    )

    request = make_request(
        path="/auth/callback",
        cookies={"oauth_state": "valid-state"},
    )

    with pytest.raises(Exception) as exc_info:
        await github_oauth_callback(
            request=request,
            code="test-code",
            state="valid-state",
            db=db,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == (
        "No access token returned from GitHub"
    )


@pytest.mark.asyncio
async def test_oauth_callback_handles_github_profile_failure(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    mock_client = AsyncMock()
    mock_client.fetch_token.return_value = {
        "access_token": "test-access-token",
    }
    mock_client.get.return_value = Response(
        status_code=500,
        json={"message": "Internal Server Error"},
        request=None,
    )

    monkeypatch.setattr(
        "app.auth.oauth.AsyncOAuth2Client",
        lambda **kwargs: mock_client,
    )

    request = make_request(
        path="/auth/callback",
        cookies={"oauth_state": "valid-state"},
    )

    with pytest.raises(Exception) as exc_info:
        await github_oauth_callback(
            request=request,
            code="test-code",
            state="valid-state",
            db=db,
        )

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == (
        "Failed to fetch user profile from GitHub"
    )


@pytest.mark.asyncio
async def test_oauth_callback_creates_new_user_token_and_session(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    mock_client = AsyncMock()
    mock_client.fetch_token.return_value = {
        "access_token": "test-access-token",
        "scope": "read:org,user:email",
    }
    mock_client.get.return_value = Response(
        status_code=200,
        json={
            "id": 123456,
            "login": "test-user",
            "email": "test@example.com",
            "avatar_url": (
                "https://avatars.githubusercontent.com/u/123456"
            ),
        },
        request=None,
    )

    monkeypatch.setattr(
        "app.auth.oauth.AsyncOAuth2Client",
        lambda **kwargs: mock_client,
    )

    request = make_request(
        path="/auth/callback",
        cookies={"oauth_state": "valid-state"},
    )

    response = await github_oauth_callback(
        request=request,
        code="test-code",
        state="valid-state",
        db=db,
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

    user = (
        await db.execute(
            select(User).where(User.github_user_id == 123456)
        )
    ).scalar_one()

    assert user.github_login == "test-user"
    assert user.github_email == "test@example.com"
    assert user.avatar_url == (
        "https://avatars.githubusercontent.com/u/123456"
    )
    assert user.last_login_at is not None

    token = (
        await db.execute(
            select(UserOAuthToken).where(
                UserOAuthToken.user_id == user.id
            )
        )
    ).scalar_one()

    assert token.encrypted_access_token != "test-access-token"
    assert (
        decrypt_token(token.encrypted_access_token)
        == "test-access-token"
    )
    assert token.scope == "read:org,user:email"

    session = (
        await db.execute(
            select(Session).where(Session.user_id == user.id)
        )
    ).scalar_one()

    assert session.user_id == user.id

    session_cookie = next(
        cookie
        for cookie in response.headers.getlist("set-cookie")
        if cookie.startswith(f"{SESSION_COOKIE_NAME}=")
    )
    signed_session_id = (
        session_cookie.split(";", 1)[0].split("=", 1)[1]
    )

    assert unsign_session_id(signed_session_id) == session.id

    assert any(
        cookie.startswith("oauth_state=")
        and "Max-Age=0" in cookie
        for cookie in response.headers.getlist("set-cookie")
    )


@pytest.mark.asyncio
async def test_oauth_callback_updates_existing_user_and_token(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
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
    mock_client.fetch_token.return_value = {
        "access_token": "new-access-token",
        "scope": "read:org,user:email",
    }
    mock_client.get.return_value = Response(
        status_code=200,
        json={
            "id": 123456,
            "login": "updated-user",
            "email": "updated@example.com",
            "avatar_url": "new-avatar",
        },
        request=None,
    )

    monkeypatch.setattr(
        "app.auth.oauth.AsyncOAuth2Client",
        lambda **kwargs: mock_client,
    )

    request = make_request(
        path="/auth/callback",
        cookies={"oauth_state": "valid-state"},
    )

    response = await github_oauth_callback(
        request=request,
        code="test-code",
        state="valid-state",
        db=db,
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/"

    await db.refresh(user)
    await db.refresh(token)

    assert user.github_login == "updated-user"
    assert user.github_email == "updated@example.com"
    assert user.avatar_url == "new-avatar"
    assert user.last_login_at is not None

    assert token.encrypted_access_token != "old-encrypted-token"
    assert (
        decrypt_token(token.encrypted_access_token)
        == "new-access-token"
    )
    assert token.scope == "read:org,user:email"
    assert token.updated_at is not None

    session = (
        await db.execute(
            select(Session).where(Session.user_id == user.id)
        )
    ).scalar_one()

    assert session.user_id == user.id


@pytest.mark.asyncio
async def test_oauth_callback_uses_default_scope(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    mock_client = AsyncMock()
    mock_client.fetch_token.return_value = {
        "access_token": "default-scope-token",
    }
    mock_client.get.return_value = Response(
        status_code=200,
        json={
            "id": 654321,
            "login": "default-scope-user",
            "email": "default-scope@example.com",
            "avatar_url": "",
        },
        request=None,
    )

    monkeypatch.setattr(
        "app.auth.oauth.AsyncOAuth2Client",
        lambda **kwargs: mock_client,
    )

    request = make_request(
        path="/auth/callback",
        cookies={"oauth_state": "valid-state"},
    )

    response = await github_oauth_callback(
        request=request,
        code="test-code",
        state="valid-state",
        db=db,
    )

    assert response.status_code == 302

    user = (
        await db.execute(
            select(User).where(User.github_user_id == 654321)
        )
    ).scalar_one()

    token = (
        await db.execute(
            select(UserOAuthToken).where(
                UserOAuthToken.user_id == user.id
            )
        )
    ).scalar_one()

    assert token.scope == "read:org"
    assert (
        decrypt_token(token.encrypted_access_token)
        == "default-scope-token"
    )


@pytest.mark.asyncio
async def test_oauth_logout_deletes_valid_session(
    db: AsyncSession,
):
    user = User(
        github_user_id=123456,
        github_login="logout-user",
        github_email="logout@example.com",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    session_id, cookie_val = await create_db_session(db, user.id)

    request = make_request(
        path="/auth/logout",
        cookies={SESSION_COOKIE_NAME: cookie_val},
    )

    response = await github_oauth_logout(request, db)

    assert response.status_code == 302
    assert response.headers["location"] == "/"

    session = (
        await db.execute(
            select(Session).where(Session.id == session_id)
        )
    ).scalar_one_or_none()

    assert session is None

    assert any(
        cookie.startswith(f"{SESSION_COOKIE_NAME}=")
        and "Max-Age=0" in cookie
        for cookie in response.headers.getlist("set-cookie")
    )


@pytest.mark.parametrize(
    "session_cookie",
    [None, "invalid-session-cookie"],
    ids=["missing-cookie", "invalid-cookie"],
)
@pytest.mark.asyncio
async def test_oauth_logout_without_valid_session_cookie(
    db: AsyncSession,
    session_cookie: str | None,
):
    cookies = (
        {SESSION_COOKIE_NAME: session_cookie}
        if session_cookie is not None
        else None
    )

    request = make_request(
        path="/auth/logout",
        cookies=cookies,
    )

    response = await github_oauth_logout(request, db)

    assert response.status_code == 302
    assert response.headers["location"] == "/"

    assert any(
        cookie.startswith(f"{SESSION_COOKIE_NAME}=")
        and "Max-Age=0" in cookie
        for cookie in response.headers.getlist("set-cookie")
    )


@pytest.mark.asyncio
async def test_oauth_logout_supports_post_route(
    db: AsyncSession,
):
    app.dependency_overrides[get_db] = lambda: db

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/auth/logout",
                follow_redirects=False,
            )

        assert response.status_code == 302
        assert response.headers["location"] == "/"

        assert any(
            cookie.startswith(f"{SESSION_COOKIE_NAME}=")
            and "Max-Age=0" in cookie
            for cookie in response.headers.get_list("set-cookie")
        )
    finally:
        app.dependency_overrides.pop(get_db, None)