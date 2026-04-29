# tests/integration/test_api.py

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from app.db.database import Base, get_db
from app.db.models import AuditLog, PRHealthScore, ContributorSnapshot
from app.main import app


@pytest_asyncio.fixture
async def test_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def client(test_db):
    app.dependency_overrides[get_db] = lambda: test_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_api_health(client):
    r = await client.get("/api/v1/health")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_audit_log_empty(client):
    r = await client.get("/api/v1/audit")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_audit_log_with_data(client, test_db):
    test_db.add(AuditLog(
        action="issue.assigned", owner="hiero", repo="sdk",
        reason="Self-assigned", target_login="alice", target_number=1,
    ))
    await test_db.commit()
    r = await client.get("/api/v1/audit")
    data = r.json()
    assert len(data) == 1
    assert data[0]["action"] == "issue.assigned"


@pytest.mark.asyncio
async def test_audit_filtered_by_owner(client, test_db):
    for owner in ["hiero", "other"]:
        test_db.add(AuditLog(action="pr.labeled", owner=owner, repo="sdk", reason="test"))
    await test_db.commit()
    r = await client.get("/api/v1/audit?owner=hiero")
    assert all(e["owner"] == "hiero" for e in r.json())


@pytest.mark.asyncio
async def test_pr_health_empty(client):
    r = await client.get("/api/v1/pr-health")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_pr_health_with_data(client, test_db):
    test_db.add(PRHealthScore(
        owner="hiero", repo="sdk", pr_number=7, pr_author="bob",
        score=82.5, has_tests=True, has_linked_issue=True,
        has_description=True, dco_signed=True, review_count=1,
        files_changed=3, label_applied="health: healthy",
    ))
    await test_db.commit()
    r = await client.get("/api/v1/pr-health?owner=hiero")
    data = r.json()
    assert len(data) == 1
    assert data[0]["score"] == 82.5


@pytest.mark.asyncio
async def test_pr_health_stats(client, test_db):
    for score in [60.0, 80.0, 90.0]:
        test_db.add(PRHealthScore(
            owner="hiero", repo="sdk", pr_number=int(score),
            pr_author="alice", score=score, has_tests=True,
            has_linked_issue=True, has_description=True,
            dco_signed=True, review_count=1, files_changed=2,
        ))
    await test_db.commit()
    r = await client.get("/api/v1/pr-health/stats?owner=hiero&repo=sdk")
    assert r.status_code == 200
    data = r.json()
    assert data["total_prs"] == 3
    assert data["min_score"] == 60.0
    assert data["max_score"] == 90.0


@pytest.mark.asyncio
async def test_contributors_endpoint(client, test_db):
    test_db.add(ContributorSnapshot(
        owner="hiero", repo="sdk", login="charlie",
        merged_prs=5, reviews_given=3, months_active=2,
        current_role="contributor", eligible_for="junior-committer",
    ))
    await test_db.commit()
    r = await client.get("/api/v1/contributors?owner=hiero")
    data = r.json()
    assert len(data) == 1
    assert data[0]["eligible_for"] == "junior-committer"


@pytest.mark.asyncio
async def test_repo_stats(client, test_db):
    test_db.add(PRHealthScore(
        owner="hiero", repo="sdk", pr_number=1, pr_author="dave",
        score=75.0, has_tests=True, has_linked_issue=False,
        has_description=True, dco_signed=True, review_count=1, files_changed=2,
    ))
    test_db.add(AuditLog(
        action="contributor.welcomed", owner="hiero", repo="sdk", reason="First",
    ))
    await test_db.commit()
    r = await client.get("/api/v1/repos/stats?owner=hiero&repo=sdk")
    assert r.status_code == 200
    d = r.json()
    assert d["total_prs_scored"] == 1
    assert d["total_contributors_welcomed"] == 1


@pytest.mark.asyncio
async def test_dashboard_renders(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert "Hiero" in r.text
    assert "chart.js" in r.text.lower() or "Chart" in r.text
