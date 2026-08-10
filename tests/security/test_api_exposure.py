# tests/security/test_api_exposure.py — REST API attack surface (#20)

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.routes import router as api_router
from app.db.database import Base, get_db
from app.db.models import AuditLog
from app.main import app


@pytest_asyncio.fixture
async def api_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(
            AuditLog(
                action="issue.assigned",
                owner="hiero",
                repo="sdk-js",
                reason="seed",
                target_login="alice",
            )
        )
        await session.commit()
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def client(api_db):
    app.dependency_overrides[get_db] = lambda: api_db
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    app.dependency_overrides.clear()


# ── The API is read-only ──────────────────────────────────────


def test_no_mutating_routes_are_registered():
    """
    Nothing under /api/v1 may create, change or delete state. A read-only
    surface is the reason an unauthenticated deployment is merely an
    information leak rather than a takeover.
    """
    methods = {
        method
        for route in api_router.routes
        for method in route.methods
        if method not in {"HEAD", "OPTIONS"}
    }

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
async def test_hostile_filter_values_return_no_rows(client, value):
    response = await client.get("/api/v1/audit", params={"owner": value})

    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_the_table_still_exists_after_injection_attempts(client):
    await client.get("/api/v1/audit", params={"owner": "'; DROP TABLE audit_logs; --"})

    response = await client.get("/api/v1/audit", params={"owner": "hiero"})
    assert len(response.json()) == 1


@pytest.mark.asyncio
async def test_legitimate_filters_still_work(client):
    response = await client.get(
        "/api/v1/audit", params={"owner": "hiero", "repo": "sdk-js"}
    )
    assert len(response.json()) == 1


# ── Resource bounds ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_limit_is_capped(client):
    """An unbounded limit turns one request into a full table scan."""
    response = await client.get("/api/v1/audit", params={"limit": 100_000})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_pr_health_limit_is_capped(client):
    response = await client.get("/api/v1/pr-health", params={"limit": 100_000})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_contributors_limit_is_capped(client):
    response = await client.get("/api/v1/contributors", params={"limit": 100_000})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_negative_offset_is_refused(client):
    response = await client.get("/api/v1/audit", params={"offset": -1})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_non_numeric_limit_is_refused(client):
    response = await client.get("/api/v1/audit", params={"limit": "all"})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_stats_endpoints_require_their_scope(client):
    """Aggregates must be scoped to one repo, not computed over everything."""
    assert (await client.get("/api/v1/pr-health/stats")).status_code == 422
    assert (await client.get("/api/v1/repos/stats")).status_code == 422


# ── Responses leak nothing sensitive ──────────────────────────


@pytest.mark.asyncio
async def test_audit_response_carries_no_credentials(client):
    body = (await client.get("/api/v1/audit")).text.lower()

    for secret_marker in ["private_key", "webhook_secret", "authorization", "bearer "]:
        assert secret_marker not in body


@pytest.mark.asyncio
async def test_health_endpoint_exposes_no_configuration(client):
    payload = (await client.get("/api/v1/health")).json()

    assert set(payload) == {"status", "service"}
