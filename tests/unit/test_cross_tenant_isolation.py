# tests/unit/test_cross_tenant_isolation.py — Tenant isolation & session unit tests


import pytest
from fastapi import HTTPException

from app.auth.session import (
    create_db_session,
    delete_db_session,
    get_db_session,
    unsign_session_id,
)
from app.db.models import Account, AccountUser, User
from app.utils.access import get_authorized_owners, verify_owner_access


@pytest.mark.asyncio
async def test_db_session_lifecycle(db):
    user = User(github_user_id=1001, github_login="testuser1")
    db.add(user)
    await db.commit()
    await db.refresh(user)

    raw_sid, cookie_val = await create_db_session(db, user.id)
    assert raw_sid is not None
    assert cookie_val is not None

    # Verify signature
    unsigned = unsign_session_id(cookie_val)
    assert unsigned == raw_sid

    # Fetch DB session
    sess_row = await get_db_session(db, raw_sid)
    assert sess_row is not None
    assert sess_row.user_id == user.id

    # Delete session
    await delete_db_session(db, raw_sid)
    deleted_row = await get_db_session(db, raw_sid)
    assert deleted_row is None


@pytest.mark.asyncio
async def test_cross_tenant_access_denial(db):
    user_a = User(github_user_id=2001, github_login="user_a")
    user_b = User(github_user_id=2002, github_login="user_b")
    db.add_all([user_a, user_b])
    await db.commit()
    await db.refresh(user_a)
    await db.refresh(user_b)

    acc_a = Account(github_installation_id=9001, org_login="org_a", plan_tier="free")
    acc_b = Account(github_installation_id=9002, org_login="org_b", plan_tier="premium")
    db.add_all([acc_a, acc_b])
    await db.commit()
    await db.refresh(acc_a)
    await db.refresh(acc_b)

    # Authorize user_a for org_a
    db.add(AccountUser(account_id=acc_a.id, user_id=user_a.id, authorized=True))
    # Authorize user_b for org_b
    db.add(AccountUser(account_id=acc_b.id, user_id=user_b.id, authorized=True))
    await db.commit()

    allowed_owners = await get_authorized_owners(user_a, db)

    # User A should pass for org_a
    verify_owner_access("org_a", user_a, allowed_owners)

    # User A must get 403 when trying to access org_b
    with pytest.raises(HTTPException) as exc_info:
        verify_owner_access("org_b", user_a, allowed_owners)
    assert exc_info.value.status_code == 403
    assert "Access denied to organization 'org_b'" in exc_info.value.detail
