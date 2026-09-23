# tests/unit/test_session_auth.py

from __future__ import annotations

from app.auth.dependencies import get_current_user_optional
from app.auth.session import (
    SESSION_COOKIE_NAME,
    create_db_session,
    decrypt_token,
    encrypt_token,
    sign_session_id,
    unsign_session_id,
)
from app.db.models import User
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request


def test_token_encryption_roundtrip():
    plain_token = "gho_1234567890abcdefghijklmnopqrstuvwxyz"
    encrypted = encrypt_token(plain_token)

    assert encrypted != plain_token
    assert len(encrypted) > 0

    decrypted = decrypt_token(encrypted)
    assert decrypted == plain_token


def test_signed_session_token_lifecycle():
    raw_sid = "0123456789abcdef0123456789abcdef"
    cookie_val = sign_session_id(raw_sid)
    assert cookie_val != raw_sid

    unsigned = unsign_session_id(cookie_val)
    assert unsigned == raw_sid


def test_invalid_signed_session_token():
    assert unsign_session_id("invalid.session.token") is None
    assert unsign_session_id("") is None


@pytest.mark.asyncio
async def test_signed_session_cookie_not_accepted_as_bearer(db: AsyncSession):
    user = User(
        github_user_id=123456,
        github_login="session-bearer-user",
        github_email="session-bearer@example.com",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    _, cookie_val = await create_db_session(db, user.id)

    # 1. Supply as Bearer token without cookie — must return None
    bearer_req = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [
                (b"authorization", f"Bearer {cookie_val}".encode("latin-1")),
            ],
        }
    )
    result_from_bearer = await get_current_user_optional(bearer_req, db)
    assert result_from_bearer is None

    # 2. Supply as session cookie — must authenticate successfully
    cookie_req = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [
                (b"cookie", f"{SESSION_COOKIE_NAME}={cookie_val}".encode("latin-1")),
            ],
        }
    )
    result_from_cookie = await get_current_user_optional(cookie_req, db)
    assert result_from_cookie is not None
    assert result_from_cookie.id == user.id

