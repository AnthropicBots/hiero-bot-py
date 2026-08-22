# tests/unit/test_session_auth.py

from __future__ import annotations

from app.auth.session import (
    decrypt_token,
    encrypt_token,
    sign_session_id,
    unsign_session_id,
)


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

