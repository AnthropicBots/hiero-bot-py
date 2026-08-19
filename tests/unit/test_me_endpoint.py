# tests/unit/test_me_endpoint.py

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.session import SESSION_COOKIE_NAME, create_db_session
from app.auth.sync import _SYNC_CACHE
from app.db.database import get_db
from app.db.models import Account, AccountRepo, AccountUser, User
from app.main import app


# ... inside test_me_accounts_tenant_isolation
@pytest.mark.asyncio
async def test_me_accounts_tenant_isolation(db: AsyncSession):
    _SYNC_CACHE.clear()
    async def _get_db_override():
        yield db

    app.dependency_overrides[get_db] = _get_db_override

    try:
        # Create two users and two accounts
        acc_a = Account(github_installation_id=101, org_login="OrgA", plan_tier="free")
        acc_b = Account(github_installation_id=102, org_login="OrgB", plan_tier="free")
        
        user_a = User(github_user_id=1, github_login="user_a")
        user_b = User(github_user_id=2, github_login="user_b")
        
        db.add_all([acc_a, acc_b, user_a, user_b])
        await db.commit()
        await db.refresh(acc_a)
        await db.refresh(acc_b)
        await db.refresh(user_a)
        await db.refresh(user_b)

        # Authorize user_a -> acc_a, user_b -> acc_b
        au_a = AccountUser(account_id=acc_a.id, user_id=user_a.id, authorized=True)
        au_b = AccountUser(account_id=acc_b.id, user_id=user_b.id, authorized=True)
        
        repo_a = AccountRepo(account_id=acc_a.id, repo_name="repo-a")
        repo_b = AccountRepo(account_id=acc_b.id, repo_name="repo-b")
        
        db.add_all([au_a, au_b, repo_a, repo_b])
        await db.commit()

        # Generate session for user_a
        _, cookie_val_a = await create_db_session(db, user_a.id)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            client.cookies.set(SESSION_COOKIE_NAME, cookie_val_a)
            response = await client.get("/api/v1/me/accounts")
            
            assert response.status_code == 200
            data = response.json()
            assert len(data) == 1
            assert data[0]["org_login"] == "OrgA"
            assert data[0]["repos"] == ["repo-a"]
            # Ensure user_a NEVER sees OrgB or repo-b
            assert "OrgB" not in [d["org_login"] for d in data]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_me_accounts_unauthenticated():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/me/accounts")
        assert response.status_code == 401
