import asyncio
import hashlib
import hmac
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.requests import Request

from app.db.database import Base
from app.db.models import WebhookDelivery
from app.github.replay_guard import is_replay
from app.github.webhooks import WebhookRouter
from app.utils.settings import settings

SECRET = "s3cret-webhook-key"
BODY = b'{"action":"opened","number":1}'


@pytest.mark.asyncio
async def test_first_delivery_is_not_a_replay(db):
    assert await is_replay(db, "delivery-1") is False


@pytest.mark.asyncio
async def test_same_delivery_id_twice_is_a_replay(db):
    assert await is_replay(db, "delivery-1") is False
    assert await is_replay(db, "delivery-1") is True


@pytest.mark.asyncio
async def test_replay_is_detected_many_times(db):
    await is_replay(db, "delivery-1")

    for _ in range(10):
        assert await is_replay(db, "delivery-1") is True


@pytest.mark.asyncio
async def test_distinct_deliveries_are_independent(db):
    assert await is_replay(db, "a") is False
    assert await is_replay(db, "b") is False
    assert await is_replay(db, "a") is True


@pytest.mark.asyncio
async def test_missing_delivery_id_is_not_treated_as_a_replay(db):
    assert await is_replay(db, "") is False
    assert await is_replay(db, "") is False


@pytest.mark.asyncio
async def test_claimed_delivery_is_persisted_to_the_database(db):
    await is_replay(db, "delivery-persisted")
    await db.commit()

    row = await db.get(WebhookDelivery, "delivery-persisted")
    assert row is not None


@pytest.mark.asyncio
async def test_dedup_state_is_shared_across_sessions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as worker_a_session:
        assert await is_replay(worker_a_session, "shared-delivery") is False
        await worker_a_session.commit()

    async with factory() as worker_b_session:
        assert await is_replay(worker_b_session, "shared-delivery") is True

    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_claims_of_the_same_delivery_are_serialized(tmp_path):
    db_path = tmp_path / "replay_race.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def claim():
        async with factory() as session:
            result = await is_replay(session, "race-delivery")
            await session.commit()
            return result

    results = await asyncio.gather(claim(), claim())

    assert sorted(results) == [False, True]

    await engine.dispose()


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
async def test_webhook_handler_rejects_replayed_delivery(db):
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

    first_result = await router.handle(first_request, db)
    await db.commit()

    assert first_result == {
        "ok": True,
        "skipped": "no repo/installation",
    }

    with pytest.raises(HTTPException) as exc:
        await router.handle(second_request, db)

    assert exc.value.status_code == 409
    assert exc.value.detail == "Duplicate delivery"


@pytest.mark.asyncio
async def test_different_delivery_ids_are_both_accepted(db):
    router, _, config_loader = make_router()
    config_loader.load.return_value = None

    signature = sign(SECRET, BODY)

    first_result = await router.handle(
        request_with("delivery-independent-1", signature),
        db,
    )
    await db.commit()

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