# app/auth/session.py — Session handling & OAuth token encryption

from __future__ import annotations

import base64

# Key derivation helper for Fernet token encryption
import secrets
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet
from itsdangerous import BadSignature, Signer
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Session
from app.utils.settings import settings

SESSION_COOKIE_NAME = "hiero_session"
SESSION_EXPIRE_SECONDS = 86400 * 7  # 7 days


def _get_fernet() -> Fernet:
    key = settings.token_encryption_key.encode("utf-8")
    if len(key) < 32:
        key = key.ljust(32, b"0")
    encoded_key = base64.urlsafe_b64encode(key[:32])
    return Fernet(encoded_key)


def encrypt_token(plain_token: str) -> str:
    if not plain_token:
        return ""
    f = _get_fernet()
    return f.encrypt(plain_token.encode("utf-8")).decode("utf-8")


def decrypt_token(encrypted_token: str) -> str:
    if not encrypted_token:
        return ""
    f = _get_fernet()
    return f.decrypt(encrypted_token.encode("utf-8")).decode("utf-8")


def _get_signer() -> Signer:
    return Signer(settings.session_secret_key)


def sign_session_id(session_id: str) -> str:
    signer = _get_signer()
    return signer.sign(session_id.encode("utf-8")).decode("utf-8")


def unsign_session_id(cookie_val: str) -> str | None:
    if not cookie_val:
        return None
    signer = _get_signer()
    try:
        return signer.unsign(cookie_val.encode("utf-8")).decode("utf-8")
    except BadSignature:
        return None


async def create_db_session(
    db: AsyncSession,
    user_id: int,
    ttl_seconds: int = SESSION_EXPIRE_SECONDS,
) -> tuple[str, str]:
    raw_session_id = secrets.token_hex(32)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)

    db_session = Session(
        id=raw_session_id,
        user_id=user_id,
        expires_at=expires_at,
    )
    db.add(db_session)
    await db.commit()

    cookie_val = sign_session_id(raw_session_id)
    return raw_session_id, cookie_val


async def get_db_session(db: AsyncSession, raw_session_id: str) -> Session | None:
    if not raw_session_id:
        return None
    now = datetime.now(timezone.utc)
    stmt = select(Session).where(Session.id == raw_session_id, Session.expires_at > now)
    res = await db.execute(stmt)
    return res.scalar_one_or_none()


async def delete_db_session(db: AsyncSession, raw_session_id: str) -> None:
    if not raw_session_id:
        return
    stmt = delete(Session).where(Session.id == raw_session_id)
    await db.execute(stmt)
    await db.commit()

