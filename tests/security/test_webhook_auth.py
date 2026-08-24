# tests/security/test_webhook_auth.py — webhook delivery authentication (#20)

import hashlib
import hmac
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.github.webhooks import WebhookRouter
from app.utils.settings import settings

SECRET = "s3cret-webhook-key"
BODY = b'{"action":"opened","number":1}'


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(
        secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()


def request_with(
    headers: dict[str, str],
    body: bytes = b"",
):
    scope = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }

    async def receive():
        return {
            "type": "http.request",
            "body": body,
            "more_body": False,
        }

    return Request(scope, receive)


@pytest.fixture(autouse=True)
def one_secret(monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", SECRET)
    monkeypatch.setattr(settings, "github_webhook_secret_old", None)


def verify(signature: str | None, body: bytes = BODY):
    headers = {"X-Hub-Signature-256": signature} if signature is not None else {}
    WebhookRouter._verify_signature(request_with(headers), body)


def assert_rejected(signature: str | None, body: bytes = BODY):
    with pytest.raises(HTTPException) as exc:
        verify(signature, body)

    assert exc.value.status_code == 401
    return exc.value


# ── Forgery ───────────────────────────────────────────────────


def test_correct_signature_is_accepted():
    verify(sign(SECRET, BODY))


def test_unsigned_delivery_is_rejected():
    assert assert_rejected(None).detail == "Missing signature"


def test_empty_signature_header_is_rejected():
    assert_rejected("")


def test_signature_for_a_different_body_is_rejected():
    assert_rejected(sign(SECRET, b'{"action":"closed"}'))


def test_body_tampered_after_signing_is_rejected():
    signature = sign(SECRET, BODY)
    assert_rejected(signature, BODY + b" ")


def test_signature_from_the_wrong_secret_is_rejected():
    assert_rejected(sign("attacker-guess", BODY))


def test_digest_without_the_sha256_prefix_is_rejected():
    raw = hmac.new(
        SECRET.encode(),
        BODY,
        hashlib.sha256,
    ).hexdigest()

    assert_rejected(raw)


def test_sha1_style_header_is_rejected():
    digest = hmac.new(
        SECRET.encode(),
        BODY,
        hashlib.sha1,
    ).hexdigest()

    assert_rejected("sha1=" + digest)


def test_uppercase_digest_is_rejected():
    signature = sign(SECRET, BODY)
    digest = signature.removeprefix("sha256=")

    assert_rejected("sha256=" + digest.upper())


def test_truncated_digest_is_rejected():
    assert_rejected(sign(SECRET, BODY)[:-4])


def test_garbage_header_is_rejected():
    assert_rejected("sha256=" + "z" * 64)


def test_empty_body_is_still_verified():
    verify(sign(SECRET, b""), b"")
    assert_rejected(sign("wrong", b""), b"")


def test_unicode_body_is_verified_over_raw_bytes():
    body = "🎉 unicode in the payload".encode()

    verify(sign(SECRET, body), body)


def test_large_body_is_verified():
    body = b"x" * (1024 * 1024)

    verify(sign(SECRET, body), body)


# ── Secret rotation ───────────────────────────────────────────


def test_both_secrets_are_accepted_during_rotation(monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", "new")
    monkeypatch.setattr(settings, "github_webhook_secret_old", "old")

    verify(sign("new", BODY))
    verify(sign("old", BODY))


def test_a_third_secret_is_never_accepted(monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", "new")
    monkeypatch.setattr(settings, "github_webhook_secret_old", "old")

    assert_rejected(sign("older-still", BODY))


def test_retired_secret_stops_working_once_cleared(monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", "new")
    monkeypatch.setattr(settings, "github_webhook_secret_old", None)

    assert_rejected(sign("old", BODY))


def test_blank_old_secret_is_not_treated_as_a_valid_key(monkeypatch):
    """An empty string must never sign anything — it is 'unset', not a secret."""
    monkeypatch.setattr(settings, "github_webhook_secret", "new")
    monkeypatch.setattr(settings, "github_webhook_secret_old", "")

    assert_rejected(sign("", BODY))


# ── Webhook handler integration ──────────────────────────────


def make_router():
    gh = AsyncMock()
    config_loader = AsyncMock()

    return WebhookRouter(gh, config_loader), gh, config_loader


@pytest.mark.asyncio
async def test_webhook_handler_rejects_invalid_signature():
    router, _, config_loader = make_router()

    request = request_with(
        {
            "X-Hub-Signature-256": sign("attacker-guess", BODY),
            "X-GitHub-Delivery": "security-test-invalid-signature",
            "X-GitHub-Event": "ping",
            "Content-Type": "application/json",
        },
        BODY,
    )

    db = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await router.handle(request, db)

    assert exc.value.status_code == 401
    assert exc.value.detail == "Invalid signature"
    config_loader.load.assert_not_awaited()


@pytest.mark.asyncio
async def test_webhook_handler_accepts_valid_signature(monkeypatch):
    router, _, config_loader = make_router()

    config_loader.load.return_value = None

    request = request_with(
        {
            "X-Hub-Signature-256": sign(SECRET, BODY),
            "X-GitHub-Delivery": "security-test-valid-signature",
            "X-GitHub-Event": "ping",
            "Content-Type": "application/json",
        },
        BODY,
    )

    db = AsyncMock()

    result = await router.handle(request, db)

    assert result == {"ok": True, "skipped": "no repo/installation"}
    config_loader.load.assert_not_awaited()
