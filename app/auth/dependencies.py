# app/auth/dependencies.py — Auth dependencies for FastAPI routes

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.session import (
    SESSION_COOKIE_NAME,
    get_db_session,
    unsign_session_id,
)
from app.db.database import get_db
from app.db.models import User


async def get_current_user_optional(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User | None:
    cookie_val = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie_val:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            cookie_val = auth_header.split(" ", 1)[1]

    if not cookie_val:
        return None

    raw_session_id = unsign_session_id(cookie_val)
    if not raw_session_id:
        return None

    session_row = await get_db_session(db, raw_session_id)
    if not session_row:
        return None

    result = await db.execute(select(User).where(User.id == session_row.user_id))
    return result.scalar_one_or_none()


async def get_current_user(
    user: User | None = Depends(get_current_user_optional),
) -> User:
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    return user
