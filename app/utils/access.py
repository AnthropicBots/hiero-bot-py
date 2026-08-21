# app/utils/access.py — Tenant isolation & access control logic

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.sync import get_user_authorized_accounts
from app.db.database import get_db
from app.db.models import User
from app.utils.logger import get_logger

log = get_logger("utils.access")


async def get_authorized_owners(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> set[str]:
    accounts = await get_user_authorized_accounts(user, db)
    return {acc["org_login"] for acc in accounts if acc.get("org_login")}


async def verify_owner_access(
    owner: str,
    user: User,
    db: AsyncSession,
) -> None:
    allowed_owners = await get_authorized_owners(user, db)
    if owner not in allowed_owners:
        log.warning("User @%s attempted unauthorized access to org '%s'", user.github_login, owner)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied to organization '{owner}'",
        )
