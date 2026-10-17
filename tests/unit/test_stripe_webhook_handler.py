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


@pytest.fixture(autouse=True)
def mock_stripe_subscription_created():
    return {
        "sub_test_123": 2_000,
        "sub_old_123": 1_000,
        "sub_new_456": 3_000,
    }


@pytest.fixture(autouse=True)
def mock_stripe_subscription_status(
    monkeypatch,
    mock_stripe_subscription_created,
):
    statuses = {"sub_test_123": "active"}

    async def retrieve_subscription_details(
        subscription_id: str,
    ) -> tuple[str, int]:
        return (
            statuses.get(subscription_id, "active"),
            mock_stripe_subscription_created.get(subscription_id, 1_500),
        )

    monkeypatch.setattr(
        "app.billing.stripe_webhooks._retrieve_subscription_details",
        retrieve_subscription_details,
    )

    return statuses


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
    created: int | None = 1_700_000_000,
    **data_obj_fields,
) -> bytes:
    if event_type.startswith("customer.subscription."):
        data_obj_fields.setdefault("id", "sub_test_123")

    payload = {
        "id": event_id,
        "type": event_type,
        "data": {"object": data_obj_fields},
    }
    if created is not None:
        payload["created"] = created
    return json.dumps(payload).encode()


@pytest.mark.asyncio
async def test_checkout_completed_does_not_grant_premium(db, monkeypatch):
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    acc = Account(github_installation_id=601, org_login="acme-direct", plan_tier="free")
    db.add(acc)
    await db.commit()

    body = stripe_event(
        "checkout.session.completed",
        status="complete",
        payment_status="paid",
        metadata={"org_login": "acme-direct"},
    )
    request = make_request(body, sign(SECRET, body, int(time.time())))

    result = await stripe_webhook(request, db)

    assert result == {"status": "success"}
    await db.refresh(acc)
    assert acc.plan_tier == "free"
    assert acc.stripe_status_event_created is None


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
        "customer.subscription.updated",
        event_id="evt_duplicate_test",
        status="active",
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
async def test_distinct_event_ids_are_processed_independently(
    db,
    monkeypatch,
    mock_stripe_subscription_status,
):
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    acc = Account(
        github_installation_id=606,
        org_login="distinct-events-org",
        plan_tier="free",
    )
    db.add(acc)
    await db.commit()

    first_body = stripe_event(
        "customer.subscription.created",
        event_id="evt_distinct_1",
        created=1_700_000_000,
        status="active",
        metadata={"org_login": "distinct-events-org"},
    )
    first_result = await stripe_webhook(
        make_request(
            first_body,
            sign(SECRET, first_body, int(time.time())),
        ),
        db,
    )

    await db.refresh(acc)
    assert first_result == {"status": "success"}
    assert acc.plan_tier == "premium"

    mock_stripe_subscription_status["sub_test_123"] = "canceled"

    second_body = stripe_event(
        "customer.subscription.deleted",
        event_id="evt_distinct_2",
        created=1_700_000_100,
        metadata={"org_login": "distinct-events-org"},
    )
    second_result = await stripe_webhook(
        make_request(
            second_body,
            sign(SECRET, second_body, int(time.time())),
        ),
        db,
    )

    await db.refresh(acc)
    assert second_result == {"status": "success"}
    assert acc.plan_tier == "free"


@pytest.mark.asyncio
async def test_subscription_deleted_downgrades_account(
    db,
    monkeypatch,
    mock_stripe_subscription_status,
):
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    mock_stripe_subscription_status["sub_test_123"] = "canceled"

    acc = Account(
        github_installation_id=602,
        org_login="beta-direct",
        plan_tier="premium",
    )
    db.add(acc)
    await db.commit()

    body = stripe_event(
        "customer.subscription.deleted",
        metadata={"org_login": "beta-direct"},
    )
    request = make_request(body, sign(SECRET, body, int(time.time())))

    result = await stripe_webhook(request, db)

    assert result == {"status": "success"}
    await db.refresh(acc)
    assert acc.plan_tier == "free"
    assert acc.stripe_subscription_id == "sub_test_123"


@pytest.mark.asyncio
async def test_newer_subscription_replaces_existing_subscription(
    db,
    monkeypatch,
):
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)

    acc = Account(
        github_installation_id=6051,
        org_login="replacement-org",
        plan_tier="free",
        stripe_subscription_id="sub_old_123",
        stripe_subscription_created=1_000,
    )
    db.add(acc)
    await db.commit()

    body = stripe_event(
        "customer.subscription.created",
        event_id="evt_new_subscription",
        created=200,
        metadata={"org_login": "replacement-org"},
    )
    result = await stripe_webhook(
        make_request(body, sign(SECRET, body, int(time.time()))),
        db,
    )

    await db.refresh(acc)
    assert result == {"status": "success"}
    assert acc.stripe_subscription_id == "sub_test_123"
    assert acc.plan_tier == "premium"
    assert acc.stripe_status_event_created == 200


@pytest.mark.asyncio
async def test_old_subscription_event_cannot_replace_newer_subscription(
    db,
    monkeypatch,
):
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)

    acc = Account(
        github_installation_id=6052,
        org_login="delayed-old-subscription-org",
        plan_tier="premium",
        stripe_subscription_id="sub_new_456",
        stripe_subscription_created=3_000,
        stripe_status_event_created=300,
    )
    db.add(acc)
    await db.commit()

    body = stripe_event(
        "customer.subscription.deleted",
        event_id="evt_delayed_old_subscription",
        created=400,
        metadata={"org_login": "delayed-old-subscription-org"},
    )
    result = await stripe_webhook(
        make_request(body, sign(SECRET, body, int(time.time()))),
        db,
    )

    await db.refresh(acc)
    assert result == {"status": "success"}
    assert acc.stripe_subscription_id == "sub_new_456"
    assert acc.plan_tier == "premium"
    assert acc.stripe_status_event_created == 300


@pytest.mark.asyncio
async def test_subscription_created_matches_by_installation_id(db, monkeypatch):
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    acc = Account(github_installation_id=603, org_login="gamma-direct", plan_tier="free")
    db.add(acc)
    await db.commit()

    body = stripe_event(
        "customer.subscription.created",
        status="active",
        metadata={"installation_id": "603"},
    )
    request = make_request(body, sign(SECRET, body, int(time.time())))

    result = await stripe_webhook(request, db)

    assert result == {"status": "success"}
    await db.refresh(acc)
    assert acc.plan_tier == "premium"


@pytest.mark.asyncio
async def test_non_numeric_installation_id_falls_back_to_org_login(
    db,
    monkeypatch,
    mock_stripe_subscription_status,
):
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    mock_stripe_subscription_status["sub_test_123"] = "canceled"

    acc = Account(
        github_installation_id=604,
        org_login="delta-direct",
        plan_tier="premium",
    )
    db.add(acc)
    await db.commit()

    body = stripe_event(
        "customer.subscription.deleted",
        metadata={
            "installation_id": "not-a-number",
            "org_login": "delta-direct",
        },
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


SUBSCRIPTION_STATUSES = [
    "incomplete",
    "incomplete_expired",
    "trialing",
    "active",
    "past_due",
    "canceled",
    "unpaid",
    "paused",
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type",
    ["customer.subscription.created", "customer.subscription.updated"],
)
@pytest.mark.parametrize("subscription_status", SUBSCRIPTION_STATUSES)
async def test_subscription_status_sets_plan_tier(
    db,
    monkeypatch,
    mock_stripe_subscription_status,
    event_type,
    subscription_status,
):
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    mock_stripe_subscription_status["sub_test_123"] = subscription_status

    acc = Account(
        github_installation_id=700,
        org_login="status-org",
        plan_tier="premium",
    )
    db.add(acc)
    await db.commit()

    body = stripe_event(
        event_type,
        event_id=f"evt_{event_type}_{subscription_status}",
        status="active" if subscription_status != "past_due" else "past_due",
        metadata={"org_login": "status-org"},
    )
    result = await stripe_webhook(
        make_request(body, sign(SECRET, body, int(time.time()))),
        db,
    )

    await db.refresh(acc)
    expected = (
        "premium"
        if subscription_status in {"active", "trialing"}
        else "free"
    )
    assert result == {"status": "success"}
    assert acc.plan_tier == expected
    assert acc.stripe_subscription_id == "sub_test_123"


@pytest.mark.asyncio
async def test_subscription_updated_without_status_downgrades(
    db,
    monkeypatch,
    mock_stripe_subscription_status,
):
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    mock_stripe_subscription_status["sub_test_123"] = "past_due"

    acc = Account(
        github_installation_id=701,
        org_login="missing-status-org",
        plan_tier="premium",
    )
    db.add(acc)
    await db.commit()

    body = stripe_event(
        "customer.subscription.updated",
        event_id="evt_missing_status",
        created=1_700_000_050,
        metadata={"org_login": "missing-status-org"},
    )
    result = await stripe_webhook(
        make_request(body, sign(SECRET, body, int(time.time()))),
        db,
    )

    await db.refresh(acc)
    assert result == {"status": "success"}
    assert acc.plan_tier == "free"
    assert acc.stripe_status_event_created == 1_700_000_050


@pytest.mark.asyncio
async def test_stale_subscription_event_does_not_regrant_premium(
    db,
    monkeypatch,
    mock_stripe_subscription_status,
):
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    mock_stripe_subscription_status["sub_test_123"] = "past_due"

    acc = Account(
        github_installation_id=702,
        org_login="stale-event-org",
        plan_tier="premium",
    )
    db.add(acc)
    await db.commit()

    newer = stripe_event(
        "customer.subscription.updated",
        event_id="evt_newer_past_due",
        created=200,
        status="past_due",
        metadata={"org_login": "stale-event-org"},
    )
    await stripe_webhook(
        make_request(newer, sign(SECRET, newer, int(time.time()))),
        db,
    )
    await db.refresh(acc)
    assert acc.plan_tier == "free"
    assert acc.stripe_status_event_created == 200

    # A stale event must not regrant Premium while Stripe still says past_due.
    stale = stripe_event(
        "customer.subscription.updated",
        event_id="evt_stale_active",
        created=100,
        status="active",
        metadata={"org_login": "stale-event-org"},
    )
    result = await stripe_webhook(
        make_request(stale, sign(SECRET, stale, int(time.time()))),
        db,
    )
    await db.refresh(acc)
    assert result == {"status": "success"}
    assert acc.plan_tier == "free"
    assert acc.stripe_status_event_created == 200

    # Once Stripe's current state becomes active, a later event can grant Premium.
    mock_stripe_subscription_status["sub_test_123"] = "active"

    later = stripe_event(
        "customer.subscription.updated",
        event_id="evt_later_active",
        created=300,
        status="active",
        metadata={"org_login": "stale-event-org"},
    )
    await stripe_webhook(
        make_request(later, sign(SECRET, later, int(time.time()))),
        db,
    )
    await db.refresh(acc)
    assert acc.plan_tier == "premium"
    assert acc.stripe_status_event_created == 300


@pytest.mark.asyncio
async def test_same_timestamp_events_use_current_stripe_status(
    db,
    monkeypatch,
    mock_stripe_subscription_status,
):
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    acc = Account(
        github_installation_id=703,
        org_login="same-timestamp-org",
        plan_tier="free",
    )
    db.add(acc)
    await db.commit()

    created = 1_700_000_000

    first = stripe_event(
        "customer.subscription.updated",
        event_id="evt_same_timestamp_active",
        created=created,
        status="active",
        metadata={"org_login": "same-timestamp-org"},
    )
    await stripe_webhook(
        make_request(first, sign(SECRET, first, int(time.time()))),
        db,
    )

    await db.refresh(acc)
    assert acc.plan_tier == "premium"
    assert acc.stripe_status_event_created == created

    # A distinct event with the same timestamp arrives. Stripe's current
    # state is now past_due, so entitlement must be downgraded.
    mock_stripe_subscription_status["sub_test_123"] = "past_due"

    second = stripe_event(
        "customer.subscription.updated",
        event_id="evt_same_timestamp_past_due",
        created=created,
        status="active",
        metadata={"org_login": "same-timestamp-org"},
    )
    result = await stripe_webhook(
        make_request(second, sign(SECRET, second, int(time.time()))),
        db,
    )

    await db.refresh(acc)
    assert result == {"status": "success"}
    assert acc.plan_tier == "free"
    assert acc.stripe_status_event_created == created


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "created",
    [
        None,
        -1,
        True,
        "1700000000",
    ],
    ids=[
        "missing-timestamp",
        "negative-timestamp",
        "boolean-timestamp",
        "string-timestamp",
    ],
)
async def test_invalid_subscription_event_timestamp_does_not_change_entitlement(
    db, monkeypatch, created
):
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)

    acc = Account(
        github_installation_id=704,
        org_login="invalid-timestamp-org",
        plan_tier="premium",
        stripe_status_event_created=100,
    )
    db.add(acc)
    await db.commit()

    event = stripe_event(
        "customer.subscription.updated",
        event_id=f"evt_invalid_timestamp_{created}",
        created=created,
        status="past_due",
        metadata={"org_login": "invalid-timestamp-org"},
    )
    result = await stripe_webhook(
        make_request(event, sign(SECRET, event, int(time.time()))),
        db,
    )

    await db.refresh(acc)
    assert result == {"status": "success"}
    assert acc.plan_tier == "premium"
    assert acc.stripe_status_event_created == 100
