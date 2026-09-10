# tests/unit/test_stripe_webhook_handler.py

import hashlib
import hmac
import json
import time

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.billing.stripe_webhooks import stripe_webhook
from app.db.models import Account
from app.utils.settings import settings

SECRET = "whsec_test_secret"


def sign(secret: str, payload: bytes, timestamp: int) -> str:
    signed_payload = f"{timestamp}.".encode() + payload
    v1 = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={v1}"


def make_request(body: bytes, sig_header: str | None) -> Request:
    headers = []
    if sig_header is not None:
        headers.append((b"stripe-signature", sig_header.encode()))
    scope = {"type": "http", "headers": headers}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def stripe_event(
    event_type: str,
    event_id: str = "evt_test_123",
    **data_obj_fields,
) -> bytes:
    return json.dumps(
        {
            "id": event_id,
            "type": event_type,
            "data": {"object": data_obj_fields},
        }
    ).encode()


@pytest.mark.asyncio
async def test_checkout_completed_upgrades_account(db, monkeypatch):
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    acc = Account(github_installation_id=601, org_login="acme-direct", plan_tier="free")
    db.add(acc)
    await db.commit()

    body = stripe_event("checkout.session.completed", metadata={"org_login": "acme-direct"})
    request = make_request(body, sign(SECRET, body, int(time.time())))

    result = await stripe_webhook(request, db)

    assert result == {"status": "success"}
    await db.refresh(acc)
    assert acc.plan_tier == "premium"


@pytest.mark.asyncio
async def test_duplicate_event_id_is_ignored(db, monkeypatch):
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    acc = Account(
        github_installation_id=605,
        org_login="duplicate-org",
        plan_tier="free",
    )
    db.add(acc)
    await db.commit()

    body = stripe_event(
        "checkout.session.completed",
        event_id="evt_duplicate_test",
        metadata={"org_login": "duplicate-org"},
    )
    signature = sign(SECRET, body, int(time.time()))

    first_result = await stripe_webhook(
        make_request(body, signature),
        db,
    )

    await db.refresh(acc)
    assert first_result == {"status": "success"}
    assert acc.plan_tier == "premium"

    acc.plan_tier = "free"
    await db.commit()

    second_result = await stripe_webhook(
        make_request(body, signature),
        db,
    )

    await db.refresh(acc)
    assert second_result == {"status": "success"}
    assert acc.plan_tier == "free"


@pytest.mark.asyncio
async def test_distinct_event_ids_are_processed_independently(db, monkeypatch):
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    acc = Account(
        github_installation_id=606,
        org_login="distinct-events-org",
        plan_tier="free",
    )
    db.add(acc)
    await db.commit()

    first_body = stripe_event(
        "checkout.session.completed",
        event_id="evt_distinct_1",
        metadata={"org_login": "distinct-events-org"},
    )
    first_signature = sign(SECRET, first_body, int(time.time()))

    first_result = await stripe_webhook(
        make_request(first_body, first_signature),
        db,
    )

    await db.refresh(acc)
    assert first_result == {"status": "success"}
    assert acc.plan_tier == "premium"

    second_body = stripe_event(
        "customer.subscription.deleted",
        event_id="evt_distinct_2",
        metadata={"org_login": "distinct-events-org"},
    )
    second_signature = sign(SECRET, second_body, int(time.time()))

    second_result = await stripe_webhook(
        make_request(second_body, second_signature),
        db,
    )

    await db.refresh(acc)
    assert second_result == {"status": "success"}
    assert acc.plan_tier == "free"


@pytest.mark.asyncio
async def test_subscription_deleted_downgrades_account(db, monkeypatch):
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    acc = Account(github_installation_id=602, org_login="beta-direct", plan_tier="premium")
    db.add(acc)
    await db.commit()

    body = stripe_event("customer.subscription.deleted", metadata={"org_login": "beta-direct"})
    request = make_request(body, sign(SECRET, body, int(time.time())))

    result = await stripe_webhook(request, db)

    assert result == {"status": "success"}
    await db.refresh(acc)
    assert acc.plan_tier == "free"


@pytest.mark.asyncio
async def test_checkout_completed_matches_by_installation_id(db, monkeypatch):
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    acc = Account(github_installation_id=603, org_login="gamma-direct", plan_tier="free")
    db.add(acc)
    await db.commit()

    body = stripe_event(
        "checkout.session.completed",
        metadata={"installation_id": "603"},
    )
    request = make_request(body, sign(SECRET, body, int(time.time())))

    result = await stripe_webhook(request, db)

    assert result == {"status": "success"}
    await db.refresh(acc)
    assert acc.plan_tier == "premium"


@pytest.mark.asyncio
async def test_non_numeric_installation_id_falls_back_to_org_login(db, monkeypatch):
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    acc = Account(github_installation_id=604, org_login="delta-direct", plan_tier="premium")
    db.add(acc)
    await db.commit()

    body = stripe_event(
        "customer.subscription.deleted",
        metadata={"installation_id": "not-a-number", "org_login": "delta-direct"},
    )
    request = make_request(body, sign(SECRET, body, int(time.time())))

    result = await stripe_webhook(request, db)

    assert result == {"status": "success"}
    await db.refresh(acc)
    assert acc.plan_tier == "free"


@pytest.mark.asyncio
async def test_missing_signature_returns_400(db, monkeypatch):
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    body = stripe_event("checkout.session.completed", metadata={"org_login": "acme-direct"})
    request = make_request(body, None)

    with pytest.raises(HTTPException) as exc:
        await stripe_webhook(request, db)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_invalid_signature_returns_400(db, monkeypatch):
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    body = stripe_event("checkout.session.completed", metadata={"org_login": "acme-direct"})
    request = make_request(body, sign("wrong-secret", body, int(time.time())))

    with pytest.raises(HTTPException) as exc:
        await stripe_webhook(request, db)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_unknown_org_login_handled_gracefully(db, monkeypatch):
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    body = stripe_event("checkout.session.completed", metadata={"org_login": "ghost-org"})
    request = make_request(body, sign(SECRET, body, int(time.time())))

    result = await stripe_webhook(request, db)

    assert result == {"status": "success"}


@pytest.mark.asyncio
async def test_missing_secret_in_production_returns_500(db, monkeypatch):
    monkeypatch.setattr(settings, "stripe_webhook_secret", None)
    monkeypatch.setattr(settings, "environment", "production")
    body = stripe_event("checkout.session.completed", metadata={"org_login": "acme-direct"})
    request = make_request(body, None)

    with pytest.raises(HTTPException) as exc:
        await stripe_webhook(request, db)
    assert exc.value.status_code == 500


@pytest.mark.asyncio
async def test_invalid_json_payload_returns_400(db, monkeypatch):
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    body = b"not-json"
    request = make_request(body, sign(SECRET, body, int(time.time())))

    with pytest.raises(HTTPException) as exc:
        await stripe_webhook(request, db)
    assert exc.value.status_code == 400
