import asyncio
import hashlib
import hmac
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.requests import Request

from app import main
from app.db.database import Base, get_db
from app.db.models import WebhookDelivery
from app.github.replay_guard import is_replay, release_delivery
from app.github.webhooks import WebhookRouter
from app.utils.settings import settings

SECRET = "s3cret-webhook-key"
BODY = b'{"action":"opened","number":1}'


@pytest.mark.asyncio
async def test_first_delivery_is_not_a_replay(db):
    assert await is_replay(db, "delivery-1", "ping") is False


@pytest.mark.asyncio
async def test_same_delivery_id_twice_is_a_replay(db):
    assert await is_replay(db, "delivery-1", "ping") is False
    assert await is_replay(db, "delivery-1", "ping") is True


@pytest.mark.asyncio
async def test_replay_is_detected_many_times(db):
    await is_replay(db, "delivery-1", "ping")

    for _ in range(10):
        assert await is_replay(db, "delivery-1", "ping") is True


@pytest.mark.asyncio
async def test_distinct_deliveries_are_independent(db):
    assert await is_replay(db, "a", "ping") is False
    assert await is_replay(db, "b", "ping") is False
    assert await is_replay(db, "a", "ping") is True


@pytest.mark.asyncio
async def test_claimed_delivery_is_persisted_to_the_database(db):
    await is_replay(db, "delivery-persisted", "ping")
    row = await db.get(WebhookDelivery, "delivery-persisted")
    assert row is not None
    assert row.event == "ping"
    assert row.received_at.tzinfo == UTC
    assert row.received_at <= datetime.now(UTC)


@pytest.mark.asyncio
async def test_dedup_state_is_shared_across_sessions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as worker_a_session:
        assert await is_replay(worker_a_session, "shared-delivery", "ping") is False

    async with factory() as worker_b_session:
        assert await is_replay(worker_b_session, "shared-delivery", "ping") is True

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
            return await is_replay(session, "race-delivery", "ping")

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
    event: str = "ping",
):
    scope = {
        "type": "http",
        "headers": [
            (b"x-github-delivery", delivery_id.encode()),
            (b"x-hub-signature-256", signature.encode()),
            (b"x-github-event", event.encode()),
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


@pytest.fixture
async def webhook_client(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'webhooks.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)

        async def override_db():
            async with factory() as db:
                yield db

        loader = AsyncMock()
        loader.load.return_value = object()
        workflow = AsyncMock()
        monkeypatch.setattr(main, "_gh", AsyncMock())
        monkeypatch.setattr(main, "_config_loader", loader)
        monkeypatch.setattr(WebhookRouter, "_handle_issues", workflow)
        monkeypatch.setitem(main.app.dependency_overrides, get_db, override_db)

        async with AsyncClient(
            transport=ASGITransport(app=main.app),
            base_url="http://test",
        ) as client:
            yield client, factory, workflow
    finally:
        await engine.dispose()


ISSUE_BODY = (
    b'{"action":"opened","repository":{"owner":{"login":"hiero"},'
    b'"name":"sdk-js"},"installation":{"id":42}}'
)


def issue_headers(delivery_id, signature=None):
    headers = {
        "X-GitHub-Event": "issues",
        "X-Hub-Signature-256": signature or sign(SECRET, ISSUE_BODY),
        "Content-Type": "application/json",
    }
    if delivery_id is not None:
        headers["X-GitHub-Delivery"] = delivery_id
    return headers


async def test_duplicate_returns_200_and_skips_workflow(webhook_client):
    client, factory, workflow = webhook_client
    headers = issue_headers("same-delivery")

    first = await client.post("/webhook", content=ISSUE_BODY, headers=headers)
    duplicate = await client.post("/webhook", content=ISSUE_BODY, headers=headers)

    assert first.status_code == 200
    assert first.json() == {"ok": True}
    assert duplicate.status_code == 200
    assert duplicate.json() == {"status": "duplicate"}
    workflow.assert_awaited_once()
    async with factory() as db:
        row = await db.get(WebhookDelivery, "same-delivery")
        assert row.event == "issues"


async def test_concurrent_webhooks_dispatch_once(webhook_client):
    client, _, workflow = webhook_client
    headers = issue_headers("concurrent-delivery")
    dispatched = asyncio.Event()
    finish = asyncio.Event()

    async def wait_in_workflow(*args):
        dispatched.set()
        await finish.wait()

    workflow.side_effect = wait_in_workflow
    first = asyncio.create_task(client.post("/webhook", content=ISSUE_BODY, headers=headers))
    try:
        await asyncio.wait_for(dispatched.wait(), timeout=5)
        # The duplicate must return while the original workflow is still running.
        duplicate = await asyncio.wait_for(
            client.post("/webhook", content=ISSUE_BODY, headers=headers), timeout=5
        )
    finally:
        finish.set()
        accepted = await asyncio.wait_for(first, timeout=5)

    assert accepted.status_code == 200
    assert accepted.json() == {"ok": True}
    assert duplicate.status_code == 200
    assert duplicate.json() == {"status": "duplicate"}
    workflow.assert_awaited_once()


@pytest.mark.parametrize("delivery_id", [None, "", " "])
async def test_missing_or_blank_delivery_is_rejected(webhook_client, delivery_id):
    client, factory, workflow = webhook_client
    response = await client.post(
        "/webhook", content=ISSUE_BODY, headers=issue_headers(delivery_id)
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Missing X-GitHub-Delivery header"}
    workflow.assert_not_awaited()
    async with factory() as db:
        assert await db.scalar(select(WebhookDelivery.id)) is None


async def test_invalid_signature_cannot_claim_delivery(webhook_client):
    client, factory, workflow = webhook_client
    delivery_id = "invalid-signature-delivery"
    response = await client.post(
        "/webhook",
        content=ISSUE_BODY,
        headers=issue_headers(delivery_id, sign("wrong-secret", ISSUE_BODY)),
    )

    assert response.status_code == 401
    workflow.assert_not_awaited()
    async with factory() as db:
        assert await db.get(WebhookDelivery, delivery_id) is None

    valid = await client.post("/webhook", content=ISSUE_BODY, headers=issue_headers(delivery_id))
    assert valid.status_code == 200
    assert valid.json() == {"ok": True}
    workflow.assert_awaited_once()


@pytest.mark.asyncio
async def test_installation_repositories_delivery_can_be_retried_after_409(db):
    router, _, _ = make_router()
    delivery_id = "installation-retry-test"

    body = (
        b'{"action":"added","installation":{"id":999999},'
        b'"repositories_added":[{"name":"test-repo"}]}'
    )
    signature = sign(SECRET, body)

    first_request = request_with(
        delivery_id,
        signature,
        body,
        event="installation_repositories",
    )

    with pytest.raises(HTTPException) as exc:
        await router.handle(first_request, db)

    assert exc.value.status_code == 409
    assert await db.get(WebhookDelivery, delivery_id) is None

    retry_request = request_with(
        delivery_id,
        signature,
        body,
        event="installation_repositories",
    )

    with pytest.raises(HTTPException) as retry_exc:
        await router.handle(retry_request, db)

    assert retry_exc.value.status_code == 409
    assert retry_exc.value.detail == "Installation account not found; retry delivery"


@pytest.mark.asyncio
async def test_releasing_delivery_does_not_rollback_pending_session_changes(db):
    from app.db.models import Account

    delivery_id = "delivery-release-preserves-session"

    assert await is_replay(db, delivery_id, "ping") is False

    account = Account(
        github_installation_id=777777,
        org_login="pending-account",
        plan_tier="free",
    )
    db.add(account)

    await release_delivery(db, delivery_id)

    persisted_account = await db.scalar(
        select(Account).where(
            Account.github_installation_id == 777777
        )
    )

    assert persisted_account is not None
    assert await db.get(WebhookDelivery, delivery_id) is None


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