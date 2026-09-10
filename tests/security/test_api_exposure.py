# tests/security/test_api_exposure.py — REST API attack surface (#20)

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.routes import router as api_router
from app.auth.session import SESSION_COOKIE_NAME, create_db_session
from app.db.database import Base, get_db
from app.db.models import Account, AccountUser, AuditLog, User
from app.main import app


@pytest_asyncio.fixture
async def api_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with factory() as session:
        session.add_all(
            [
                AuditLog(
                    action="issue.assigned",
                    owner="hiero",
                    repo="sdk-js",
                    reason="seed",
                    target_login="alice",
                ),
                AuditLog(
                    action="issue.assigned",
                    owner="other",
                    repo="other-repo",
                    reason="second tenant",
                    target_login="bob",
                ),
            ]
        )
        await session.commit()

        yield session

    await engine.dispose()


@pytest_asyncio.fixture
async def api_user(api_db):
    user = User(
        github_user_id=123456789,
        github_login="maintainer",
        github_email="maintainer@example.com",
    )

    account = Account(
        github_installation_id=987654321,
        github_account_id=987654321,
        org_login="hiero",
        account_type="Organization",
        plan_tier="free",
    )

    api_db.add_all([user, account])
    await api_db.commit()
    await api_db.refresh(user)
    await api_db.refresh(account)

    account_user = AccountUser(
        account_id=account.id,
        user_id=user.id,
        authorized=True,
    )

    api_db.add(account_user)
    await api_db.commit()

    return user


@pytest_asyncio.fixture
async def client(api_db, api_user):
    _, cookie_value = await create_db_session(api_db, api_user.id)

    app.dependency_overrides[get_db] = lambda: api_db

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={SESSION_COOKIE_NAME: cookie_value},
    ) as c:
        yield c

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def unauthenticated_client(api_db):
    app.dependency_overrides[get_db] = lambda: api_db

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c

    app.dependency_overrides.clear()


# ── Authentication ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_api_requires_authentication(unauthenticated_client):
    response = await unauthenticated_client.get("/api/v1/audit")

    assert response.status_code == 401
    assert response.json()["detail"] == "Authentication required"


@pytest.mark.asyncio
async def test_api_rejects_invalid_bearer_token(unauthenticated_client):
    response = await unauthenticated_client.get(
        "/api/v1/audit",
        headers={"Authorization": "Bearer invalid-session-token"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Authentication required"


@pytest.mark.asyncio
async def test_api_accepts_valid_session(client):
    response = await client.get("/api/v1/audit")

    assert response.status_code == 200


# ── The API is read-only ──────────────────────────────────────


def _registered_methods(router) -> set[str]:
    methods: set[str] = set()

    for route in router.routes:
        if hasattr(route, "methods"):
            methods.update(route.methods)
        elif hasattr(route, "routes"):
            methods.update(_registered_methods(route))

    return methods


def test_no_mutating_routes_are_registered():
    """
    Nothing under /api/v1 may create, change or delete state.
    """
    methods = _registered_methods(api_router)
    methods.difference_update({"HEAD", "OPTIONS"})

    assert methods == {"GET"}


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
async def test_write_methods_are_refused(client, method):
    response = await getattr(client, method)("/api/v1/audit")

    assert response.status_code == 405


# ── Injection attempts ────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        "hiero' OR '1'='1",
        "'; DROP TABLE audit_logs; --",
        "hiero%' UNION SELECT * FROM audit_logs --",
        "../../etc/passwd",
        "<script>alert(1)</script>",
        "\x00null-byte",
    ],
)
@pytest.mark.asyncio
async def test_hostile_filter_values_are_rejected(client, value):
    response = await client.get(
        "/api/v1/audit",
        params={"owner": value},
    )

    assert response.status_code == 403
    assert "Access denied" in response.json()["detail"]


@pytest.mark.asyncio
async def test_injection_attempt_does_not_destroy_the_table(client):
    malicious_owner = "'; DROP TABLE audit_logs; --"

    malicious_response = await client.get(
        "/api/v1/audit",
        params={"owner": malicious_owner},
    )

    assert malicious_response.status_code == 403

    response = await client.get(
        "/api/v1/audit",
        params={"owner": "hiero"},
    )

    assert response.status_code == 200

    rows = response.json()

    assert len(rows) == 1
    assert rows[0]["owner"] == "hiero"
    assert rows[0]["repo"] == "sdk-js"


@pytest.mark.asyncio
async def test_legitimate_filters_still_work(client):
    response = await client.get(
        "/api/v1/audit",
        params={"owner": "hiero", "repo": "sdk-js"},
    )

    assert response.status_code == 200

    rows = response.json()

    assert len(rows) == 1
    assert rows[0]["owner"] == "hiero"
    assert rows[0]["repo"] == "sdk-js"


# ── Resource bounds ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_limit_is_capped(client):
    """An unbounded limit must be rejected."""
    response = await client.get(
        "/api/v1/audit",
        params={"limit": 100_000},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_pr_health_limit_is_capped(client):
    response = await client.get(
        "/api/v1/pr-health",
        params={"limit": 100_000},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_contributors_limit_is_capped(client):
    response = await client.get(
        "/api/v1/contributors",
        params={"limit": 100_000},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_negative_offset_is_refused(client):
    response = await client.get(
        "/api/v1/audit",
        params={"offset": -1},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_non_numeric_limit_is_refused(client):
    response = await client.get(
        "/api/v1/audit",
        params={"limit": "all"},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_stats_endpoints_require_their_scope(client):
    """Aggregate endpoints require owner and repo parameters."""
    assert (
        await client.get("/api/v1/pr-health/stats")
    ).status_code == 422

    assert (
        await client.get("/api/v1/repos/stats")
    ).status_code == 422


# ── Responses leak nothing sensitive ──────────────────────────


@pytest.mark.asyncio
async def test_audit_response_carries_no_credentials(client):
    body = (
        await client.get("/api/v1/audit")
    ).text.lower()

    for secret_marker in [
        "private_key",
        "webhook_secret",
        "authorization",
        "bearer ",
    ]:
        assert secret_marker not in body


@pytest.mark.asyncio
async def test_health_endpoint_exposes_no_configuration(client):
    payload = (
        await client.get("/api/v1/health")
    ).json()

    assert set(payload) == {"status", "service"}
