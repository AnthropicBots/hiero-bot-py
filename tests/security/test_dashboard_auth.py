# tests/security/test_dashboard_auth.py — dashboard / API access control (#20)

import asyncio
import base64

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.auth.session import SESSION_COOKIE_NAME, create_db_session
from app.db.database import Base, get_db
from app.db.models import User
from app.main import app
from app.utils.auth import _auth_configured, _matches, require_dashboard_auth
from app.utils.settings import settings

USER = "maintainer"
PASSWORD = "correct-horse-battery-staple"


def build_app():
    app = FastAPI()

    @app.get("/protected")
    async def protected(_: None = Depends(require_dashboard_auth)):
        return {"ok": True}

    return app


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=build_app()), base_url="http://test"
    ) as c:
        yield c


@pytest.fixture
def credentials_set(monkeypatch):
    monkeypatch.setattr(settings, "legacy_basic_auth_enabled", True)
    monkeypatch.setattr(settings, "dashboard_username", USER)
    monkeypatch.setattr(settings, "dashboard_password", PASSWORD)


@pytest_asyncio.fixture
async def dashboard_db():
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with factory() as session:
        yield session

    await engine.dispose()


@pytest_asyncio.fixture
async def real_dashboard_client(dashboard_db):
    user = User(
        github_user_id=987654321,
        github_login=USER,
        github_email="maintainer@example.com",
    )

    dashboard_db.add(user)
    await dashboard_db.commit()
    await dashboard_db.refresh(user)

    _, cookie_value = await create_db_session(dashboard_db, user.id)

    app.dependency_overrides[get_db] = lambda: dashboard_db

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={SESSION_COOKIE_NAME: cookie_value},
    ) as c:
        yield c

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def unauthenticated_dashboard_client(dashboard_db):
    app.dependency_overrides[get_db] = lambda: dashboard_db

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c

    app.dependency_overrides.clear()


def basic(user, password):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


# ── Legacy Basic Auth dependency ──────────────────────────────


@pytest.mark.asyncio
async def test_correct_credentials_are_accepted(client, credentials_set):
    response = await client.get("/protected", headers=basic(USER, PASSWORD))
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_no_credentials_are_rejected(client, credentials_set):
    response = await client.get("/protected")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Basic"


@pytest.mark.asyncio
async def test_wrong_password_is_rejected(client, credentials_set):
    response = await client.get("/protected", headers=basic(USER, "guess"))
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_wrong_username_is_rejected(client, credentials_set):
    response = await client.get("/protected", headers=basic("intruder", PASSWORD))
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_empty_credentials_are_rejected(client, credentials_set):
    response = await client.get("/protected", headers=basic("", ""))
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_password_prefix_is_rejected(client, credentials_set):
    response = await client.get("/protected", headers=basic(USER, PASSWORD[:-1]))
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_malformed_authorization_header_is_rejected(
    client,
    credentials_set,
):
    response = await client.get(
        "/protected",
        headers={"Authorization": "Basic not-base64"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_bearer_token_is_not_accepted(client, credentials_set):
    response = await client.get(
        "/protected",
        headers={"Authorization": f"Bearer {PASSWORD}"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_non_ascii_username_is_rejected_not_crashed(
    client,
    credentials_set,
):
    response = await client.get(
        "/protected",
        headers=basic("maintaıner", PASSWORD),
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_non_ascii_password_is_rejected_not_crashed(
    client,
    credentials_set,
):
    response = await client.get(
        "/protected",
        headers=basic(USER, "pässwörd"),
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_non_ascii_credentials_are_unusable_end_to_end(
    client,
    credentials_set,
):
    """
    FastAPI's HTTPBasic rejects non-ASCII credentials before they reach
    _matches(), so non-ASCII legacy Basic Auth credentials are unusable.
    """
    response = await client.get(
        "/protected",
        headers=basic("wächter", "pässwörd"),
    )

    assert response.status_code == 401


# ── Real dashboard integration ────────────────────────────────


@pytest.mark.asyncio
async def test_real_dashboard_without_session_shows_login(
    unauthenticated_dashboard_client,
):
    response = await unauthenticated_dashboard_client.get("/")

    assert response.status_code == 200
    assert "login" in response.text.lower()


@pytest.mark.asyncio
async def test_real_dashboard_with_valid_session_is_accessible(
    real_dashboard_client,
):
    response = await real_dashboard_client.get("/")

    assert response.status_code == 200
    assert "dashboard" in response.text.lower()


@pytest.mark.asyncio
async def test_real_dashboard_rejects_invalid_bearer_session(
    unauthenticated_dashboard_client,
):
    response = await unauthenticated_dashboard_client.get(
        "/",
        headers={"Authorization": "Bearer invalid-session-token"},
    )

    assert response.status_code == 200
    assert "login" in response.text.lower()


# ── Legacy Basic Auth configuration ───────────────────────────


@pytest.mark.asyncio
async def test_unset_credentials_leave_legacy_endpoint_open(
    client,
    monkeypatch,
):
    monkeypatch.setattr(settings, "legacy_basic_auth_enabled", True)
    monkeypatch.setattr(settings, "dashboard_username", None)
    monkeypatch.setattr(settings, "dashboard_password", None)

    response = await client.get("/protected")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_password_without_username_does_not_half_enable_auth(
    client,
    monkeypatch,
):
    monkeypatch.setattr(settings, "legacy_basic_auth_enabled", True)
    monkeypatch.setattr(settings, "dashboard_username", None)
    monkeypatch.setattr(settings, "dashboard_password", PASSWORD)

    assert _auth_configured() is False
    assert (await client.get("/protected")).status_code == 200


@pytest.mark.asyncio
async def test_username_without_password_does_not_half_enable_auth(
    client,
    monkeypatch,
):
    monkeypatch.setattr(settings, "legacy_basic_auth_enabled", True)
    monkeypatch.setattr(settings, "dashboard_username", USER)
    monkeypatch.setattr(settings, "dashboard_password", None)

    assert _auth_configured() is False
    assert (await client.get("/protected")).status_code == 200


@pytest.mark.asyncio
async def test_empty_string_credentials_count_as_unset(
    client,
    monkeypatch,
):
    monkeypatch.setattr(settings, "legacy_basic_auth_enabled", True)
    monkeypatch.setattr(settings, "dashboard_username", "")
    monkeypatch.setattr(settings, "dashboard_password", "")

    assert _auth_configured() is False


# ── Comparison primitive ──────────────────────────────────────


def test_matches_is_exact():
    assert _matches("abc", "abc") is True
    assert _matches("abc", "abd") is False
    assert _matches("abc", "abcd") is False
    assert _matches("", "") is True


def test_matches_handles_non_ascii_without_raising():
    assert _matches("üser", "user") is False
    assert _matches("üser", "üser") is True


def test_dependency_raises_http_exception_not_a_bare_error(monkeypatch):
    monkeypatch.setattr(settings, "legacy_basic_auth_enabled", True)
    monkeypatch.setattr(settings, "dashboard_username", USER)
    monkeypatch.setattr(settings, "dashboard_password", PASSWORD)

    with pytest.raises(HTTPException):
        asyncio.run(require_dashboard_auth(credentials=None))
