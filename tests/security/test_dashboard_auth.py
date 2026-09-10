# tests/security/test_dashboard_auth.py — dashboard / API access control (#20)

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.auth.session import SESSION_COOKIE_NAME, create_db_session
from app.db.database import Base, get_db
from app.db.models import User
from app.main import app

USER = "maintainer"


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
