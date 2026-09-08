# tests/integration/test_stripe_webhooks.py

import hashlib
import hmac
import json
import time

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.billing.stripe_webhooks import settings as stripe_settings
from app.db.database import Base, get_db
from app.db.models import Account
from app.main import app

STRIPE_SECRET = "whsec_test_secret"


def sig_header(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    ts = timestamp if timestamp is not None else int(time.time())
    signed_payload = f"{ts}.".encode() + payload
    v1 = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={v1}"


def stripe_event(event_type: str, **data_obj_fields) -> bytes:
    return json.dumps({"type": event_type, "data": {"object": data_obj_fields}}).encode()


@pytest_asyncio.fixture
async def test_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def client(test_db, monkeypatch):
    monkeypatch.setattr(stripe_settings, "stripe_webhook_secret", STRIPE_SECRET)
    app.dependency_overrides[get_db] = lambda: test_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_checkout_completed_upgrades_account_to_premium(client, test_db):
    acc = Account(github_installation_id=501, org_login="acme", plan_tier="free")
    test_db.add(acc)
    await test_db.commit()

    body = stripe_event(
        "checkout.session.completed",
        metadata={"org_login": "acme", "installation_id": "501"},
    )
    r = await client.post(
        "/webhooks/stripe", content=body,
        headers={"Stripe-Signature": sig_header(body, STRIPE_SECRET)},
    )

    assert r.status_code == 200
    assert r.json() == {"status": "success"}
    await test_db.refresh(acc)
    assert acc.plan_tier == "premium"


@pytest.mark.asyncio
async def test_subscription_deleted_downgrades_account_to_free(client, test_db):
    acc = Account(github_installation_id=502, org_login="beta", plan_tier="premium")
    test_db.add(acc)
    await test_db.commit()

    body = stripe_event("customer.subscription.deleted", metadata={"org_login": "beta"})
    r = await client.post(
        "/webhooks/stripe", content=body,
        headers={"Stripe-Signature": sig_header(body, STRIPE_SECRET)},
    )

    assert r.status_code == 200
    await test_db.refresh(acc)
    assert acc.plan_tier == "free"


@pytest.mark.asyncio
async def test_missing_signature_returns_400(client):
    body = stripe_event("checkout.session.completed", metadata={"org_login": "acme"})

    r = await client.post("/webhooks/stripe", content=body)

    assert r.status_code == 400


@pytest.mark.asyncio
async def test_invalid_signature_returns_400(client):
    body = stripe_event("checkout.session.completed", metadata={"org_login": "acme"})

    r = await client.post(
        "/webhooks/stripe", content=body,
        headers={"Stripe-Signature": sig_header(body, "not-the-real-secret")},
    )

    assert r.status_code == 400


@pytest.mark.asyncio
async def test_unknown_org_login_is_handled_gracefully(client, test_db):
    body = stripe_event(
        "checkout.session.completed",
        metadata={"org_login": "org-that-does-not-exist"},
    )

    r = await client.post(
        "/webhooks/stripe", content=body,
        headers={"Stripe-Signature": sig_header(body, STRIPE_SECRET)},
    )

    assert r.status_code == 200
    assert r.json() == {"status": "success"}
