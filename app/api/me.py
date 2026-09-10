# app/api/me.py — User-scoped API endpoints

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.sync import get_user_authorized_accounts
from app.db.database import get_db
from app.db.models import User

router = APIRouter(prefix="/me", tags=["me"])


@router.get("/accounts")
async def get_my_accounts(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return list of installed accounts & repositories authorized for the current user."""
    accounts = await get_user_authorized_accounts(current_user, db)
    return accounts
