# tests/security/test_replay_protection.py — delivery replay guard (#20)

import hashlib
import hmac
import time
from unittest.mock import AsyncMock

import pytest
from cachetools import TTLCache
from fastapi import HTTPException
from starlette.requests import Request

from app.github import replay_guard
from app.github.replay_guard import is_replay
from app.github.webhooks import WebhookRouter
from app.utils.settings import settings

SECRET = "s3cret-webhook-key"
BODY = b'{"action":"opened","number":1}'


def fresh_cache(monkeypatch, maxsize=10_000, ttl=600):
    monkeypatch.setattr(
        replay_guard,
        "_seen_deliveries",
        TTLCache(maxsize=maxsize, ttl=ttl),
    )


def test_first_delivery_is_not_a_replay(monkeypatch):
    fresh_cache(monkeypatch)

    assert is_replay("delivery-1") is False


def test_same_delivery_id_twice_is_a_replay(monkeypatch):
    fresh_cache(monkeypatch)

    assert is_replay("delivery-1") is False
    assert is_replay("delivery-1") is True


def test_replay_is_detected_many_times(monkeypatch):
    fresh_cache(monkeypatch)
    is_replay("delivery-1")

    assert all(is_replay("delivery-1") for _ in range(10))


def test_distinct_deliveries_are_independent(monkeypatch):
    fresh_cache(monkeypatch)

    assert is_replay("a") is False
    assert is_replay("b") is False
    assert is_replay("a") is True


def test_missing_delivery_id_is_not_treated_as_a_replay(monkeypatch):
    """GitHub always sends one; absence must not wedge the endpoint shut."""
    fresh_cache(monkeypatch)

    assert is_replay("") is False
    assert is_replay("") is False


def test_ids_expire_after_the_ttl(monkeypatch):
    fresh_cache(monkeypatch, ttl=0.05)
    is_replay("delivery-1")

    time.sleep(0.1)

    assert is_replay("delivery-1") is False


def test_cache_is_bounded(monkeypatch):
    """An attacker replaying unique IDs must not grow memory without limit."""
    fresh_cache(monkeypatch, maxsize=100)

    for i in range(1000):
        is_replay(f"delivery-{i}")

    assert len(replay_guard._seen_deliveries) <= 100


def test_old_delivery_is_evicted_when_cache_is_full(monkeypatch):
    fresh_cache(monkeypatch, maxsize=10)

    for i in range(10):
        assert is_replay(f"delivery-{i}") is False

    assert is_replay("delivery-0") is True

    assert is_replay("delivery-10") is False

    assert is_replay("delivery-0") is False
    assert is_replay("delivery-10") is True


def test_default_guard_has_a_bounded_size_and_ttl():
    assert replay_guard._seen_deliveries.maxsize == 10_000
    assert replay_guard._DELIVERY_TTL_SECONDS == 600


# ── Webhook handler integration ──────────────────────────────


def make_router():
    gh = AsyncMock()
    config_loader = AsyncMock()

    return WebhookRouter(gh, config_loader), gh, config_loader


def request_with(
    delivery_id: str,
    signature: str,
    body: bytes = BODY,
):
    scope = {
        "type": "http",
        "headers": [
            (b"x-github-delivery", delivery_id.encode()),
            (b"x-hub-signature-256", signature.encode()),
            (b"x-github-event", b"ping"),
            (b"content-type", b"application/json"),
        ],
    }

    async def receive():
        return {
            "type": "http.request",
            "body": body,
            "more_body": False,
        }

    return Request(scope, receive)


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(
        secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()


@pytest.fixture(autouse=True)
def webhook_secret(monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", SECRET)
    monkeypatch.setattr(settings, "github_webhook_secret_old", None)


@pytest.mark.asyncio
async def test_webhook_handler_rejects_replayed_delivery(monkeypatch):
    fresh_cache(monkeypatch)

    router, _, config_loader = make_router()
    config_loader.load.return_value = None

    signature = sign(SECRET, BODY)

    first_request = request_with(
        "delivery-replay-test",
        signature,
    )

    second_request = request_with(
        "delivery-replay-test",
        signature,
    )

    db = AsyncMock()

    first_result = await router.handle(first_request, db)

    assert first_result == {
        "ok": True,
        "skipped": "no repo/installation",
    }

    with pytest.raises(HTTPException) as exc:
        await router.handle(second_request, db)

    assert exc.value.status_code == 409
    assert exc.value.detail == "Duplicate delivery"


@pytest.mark.asyncio
async def test_different_delivery_ids_are_both_accepted(monkeypatch):
    fresh_cache(monkeypatch)

    router, _, config_loader = make_router()
    config_loader.load.return_value = None

    signature = sign(SECRET, BODY)
    db = AsyncMock()

    first_result = await router.handle(
        request_with("delivery-independent-1", signature),
        db,
    )

    second_result = await router.handle(
        request_with("delivery-independent-2", signature),
        db,
    )

    assert first_result == {
        "ok": True,
        "skipped": "no repo/installation",
    }

    assert second_result == {
        "ok": True,
        "skipped": "no repo/installation",
    }
