import hashlib
import hmac

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.github.webhooks import WebhookRouter
from app.utils.settings import settings


def make_request(signature: str | None):
    headers = []
    if signature:
        headers.append((b"x-hub-signature-256", signature.encode()))

    scope = {
        "type": "http",
        "headers": headers,
    }

    async def receive():
        return {
            "type": "http.request",
            "body": b"",
            "more_body": False,
        }

    return Request(scope, receive)


def sign(secret: str, body: bytes) -> str:
    return (
        "sha256="
        + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    )


def test_missing_signature():
    request = make_request(None)

    with pytest.raises(HTTPException) as exc:
        WebhookRouter._verify_signature(request, b"payload")

    assert exc.value.status_code == 401
    assert exc.value.detail == "Missing signature"


def test_valid_current_secret(monkeypatch):
    body = b"payload"

    monkeypatch.setattr(settings, "github_webhook_secret", "current")
    monkeypatch.setattr(settings, "github_webhook_secret_old", None)

    request = make_request(sign("current", body))

    WebhookRouter._verify_signature(request, body)


def test_invalid_signature(monkeypatch):
    body = b"payload"

    monkeypatch.setattr(settings, "github_webhook_secret", "current")
    monkeypatch.setattr(settings, "github_webhook_secret_old", None)

    request = make_request(sign("wrong", body))

    with pytest.raises(HTTPException):
        WebhookRouter._verify_signature(request, body)


def test_old_secret_accepted(monkeypatch):
    body = b"payload"

    monkeypatch.setattr(settings, "github_webhook_secret", "new")
    monkeypatch.setattr(settings, "github_webhook_secret_old", "old")

    request = make_request(sign("old", body))

    WebhookRouter._verify_signature(request, body)


def test_old_secret_rejected_when_removed(monkeypatch):
    body = b"payload"

    monkeypatch.setattr(settings, "github_webhook_secret", "new")
    monkeypatch.setattr(settings, "github_webhook_secret_old", None)

    request = make_request(sign("old", body))

    with pytest.raises(HTTPException):
        WebhookRouter._verify_signature(request, body)