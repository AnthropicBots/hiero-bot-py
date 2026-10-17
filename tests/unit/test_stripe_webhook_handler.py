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
async def test_checkout_completed_does_not_grant_premium(db, monkeypatch):
    """Checkout completion alone must not grant premium — subscription event required."""
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    acc = Account(github_installation_id=601, org_login="acme-direct", plan_tier="free")
    db.add(acc)
    await db.commit()

    body = stripe_event("checkout.session.completed", metadata={"org_login": "acme-direct"})
    request = make_request(body, sign(SECRET, body, int(time.time())))

    result = await stripe_webhook(request, db)

    assert result == {"status": "success"}
    await db.refresh(acc)
    # Account must remain free until a subscription.created/updated with
    # status=active or status=trialing is received.
    assert acc.plan_tier == "free"


@pytest.mark.asyncio
async def test_subscription_created_active_upgrades_account(db, monkeypatch):
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    acc = Account(github_installation_id=610, org_login="sub-active-org", plan_tier="free")
    db.add(acc)
    await db.commit()

    body = stripe_event(
        "customer.subscription.created",
        metadata={"org_login": "sub-active-org"},
        status="active",
    )
    request = make_request(body, sign(SECRET, body, int(time.time())))

    result = await stripe_webhook(request, db)

    assert result == {"status": "success"}
    await db.refresh(acc)
    assert acc.plan_tier == "premium"


@pytest.mark.asyncio
async def test_subscription_updated_past_due_downgrades_account(db, monkeypatch):
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    acc = Account(github_installation_id=611, org_login="sub-pastdue-org", plan_tier="premium")
    db.add(acc)
    await db.commit()

    body = stripe_event(
        "customer.subscription.updated",
        metadata={"org_login": "sub-pastdue-org"},
        status="past_due",
    )
    request = make_request(body, sign(SECRET, body, int(time.time())))

    result = await stripe_webhook(request, db)

    assert result == {"status": "success"}
    await db.refresh(acc)
    assert acc.plan_tier == "free"


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
        "customer.subscription.created",
        event_id="evt_duplicate_test",
        metadata={"org_login": "duplicate-org"},
        status="active",
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
        "customer.subscription.created",
        event_id="evt_distinct_1",
        metadata={"org_login": "distinct-events-org"},
        status="active",
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
    # Checkout completion alone must not grant premium.
    assert acc.plan_tier == "free"


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


@pytest.mark.asyncio
async def test_unmatched_subscription_event_returns_400(db, monkeypatch, caplog):
    """An unmatched subscription event must return 400 so Stripe retries it."""
    import logging

    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    body = stripe_event(
        "customer.subscription.created",
        event_id="evt_unmatched_999",
        status="active",
        org_login="org-that-does-not-exist",
    )
    sig = sign(SECRET, body, int(time.time()))
    request = make_request(body, sig)

    with caplog.at_level(logging.WARNING, logger="hiero.billing.stripe"), pytest.raises(HTTPException) as exc:
        await stripe_webhook(request, db=db)

    assert exc.value.status_code == 400
    assert any("could not be matched" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_customer_id_fallback_activates_account(db, monkeypatch):
    """checkout stores stripe_customer_id; later subscription event matches via customer ID."""
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    acc = Account(github_installation_id=620, org_login="cust-id-org", plan_tier="free")
    db.add(acc)
    await db.commit()

    # Step 1: checkout.session.completed — matched by org_login, stores customer ID.
    checkout_body = stripe_event(
        "checkout.session.completed",
        event_id="evt_checkout_cust_id",
        metadata={"org_login": "cust-id-org"},
        customer="cus_test_abc123",
    )
    await stripe_webhook(
        make_request(checkout_body, sign(SECRET, checkout_body, int(time.time()))),
        db=db,
    )
    await db.refresh(acc)
    assert acc.stripe_customer_id == "cus_test_abc123"
    assert acc.plan_tier == "free"  # not yet activated

    # Step 2: subscription event carries only customer ID (no metadata).
    sub_body = stripe_event(
        "customer.subscription.created",
        event_id="evt_sub_cust_id",
        customer="cus_test_abc123",
        status="active",
    )
    result = await stripe_webhook(
        make_request(sub_body, sign(SECRET, sub_body, int(time.time()))),
        db=db,
    )

    assert result == {"status": "success"}
    await db.refresh(acc)
    assert acc.plan_tier == "premium"


@pytest.mark.asyncio
async def test_subscription_before_checkout_returns_400_and_retries(db, monkeypatch):
    """If subscription event arrives before checkout stores customer ID, return 400 so Stripe retries."""
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    acc = Account(
        github_installation_id=621,
        org_login="race-org",
        plan_tier="free",
        stripe_customer_id=None,
    )
    db.add(acc)
    await db.commit()

    # Subscription event arrives first — carries only customer ID, no metadata.
    # Account has no stripe_customer_id yet so _get_account returns None.
    sub_body = stripe_event(
        "customer.subscription.created",
        event_id="evt_sub_race",
        customer="cus_race_xyz",
        status="active",
    )
    with pytest.raises(HTTPException) as exc:
        await stripe_webhook(
            make_request(sub_body, sign(SECRET, sub_body, int(time.time()))),
            db=db,
        )
    assert exc.value.status_code == 400

    # Event must NOT have been claimed — Stripe can retry it.
    from sqlalchemy import select as sa_select

    from app.db.models import StripeEvent
    claimed = await db.scalar(
        sa_select(StripeEvent.id).where(StripeEvent.id == "evt_sub_race")
    )
    assert claimed is None

    # Now checkout arrives and stores the customer ID.
    checkout_body = stripe_event(
        "checkout.session.completed",
        event_id="evt_checkout_race",
        metadata={"org_login": "race-org"},
        customer="cus_race_xyz",
    )
    await stripe_webhook(
        make_request(checkout_body, sign(SECRET, checkout_body, int(time.time()))),
        db=db,
    )
    await db.refresh(acc)
    assert acc.stripe_customer_id == "cus_race_xyz"

    # Stripe retries delivery of the SAME event ID — verify it was not
    # claimed on the failed attempt and is processed successfully now.
    sub_retry_body = stripe_event(
        "customer.subscription.created",
        event_id="evt_sub_race",  # same ID as the failed attempt
        customer="cus_race_xyz",
        status="active",
    )
    result = await stripe_webhook(
        make_request(sub_retry_body, sign(SECRET, sub_retry_body, int(time.time()))),
        db=db,
    )
    assert result == {"status": "success"}
    await db.refresh(acc)
    assert acc.plan_tier == "premium"

    # Confirm the event was claimed exactly once (on the successful retry).
    claimed_after = await db.scalar(
        sa_select(StripeEvent.id).where(StripeEvent.id == "evt_sub_race")
    )
    assert claimed_after == "evt_sub_race"


@pytest.mark.asyncio
async def test_out_of_order_events_do_not_overwrite_newer_state(db, monkeypatch):
    """Older stale events must not overwrite newer billing state.

    Stripe does not guarantee event delivery order.  A stale ``past_due``
    event arriving after a newer ``active`` event has been processed must not
    downgrade the account.  The handler fetches the live subscription status
    from Stripe; in this test that is patched to return ``"active"``
    (the current truth), so the stale embedded ``past_due`` is ignored.
    """
    from app.billing import stripe_webhooks as billing_module

    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    acc = Account(
        github_installation_id=630,
        org_login="out-of-order-org",
        plan_tier="free",
    )
    db.add(acc)
    await db.commit()

    async def _live_active(_sid):
        return "active"

    # Step 1: newer event (active) arrives first.
    active_body = stripe_event(
        "customer.subscription.updated",
        event_id="evt_newer_active",
        metadata={"org_login": "out-of-order-org"},
        id="sub_ooo_test",
        status="active",
    )
    monkeypatch.setattr(billing_module, "_fetch_live_subscription_status", _live_active)
    result = await stripe_webhook(
        make_request(active_body, sign(SECRET, active_body, int(time.time()))), db=db
    )
    assert result == {"status": "success"}
    await db.refresh(acc)
    assert acc.plan_tier == "premium"

    # Step 2: older event (past_due) arrives late with a different event ID.
    # Live lookup still returns "active" — the stale embedded status is ignored.
    stale_body = stripe_event(
        "customer.subscription.updated",
        event_id="evt_older_past_due",
        metadata={"org_login": "out-of-order-org"},
        id="sub_ooo_test",
        status="past_due",
    )
    monkeypatch.setattr(billing_module, "_fetch_live_subscription_status", _live_active)
    result = await stripe_webhook(
        make_request(stale_body, sign(SECRET, stale_body, int(time.time()))), db=db
    )
    assert result == {"status": "success"}
    await db.refresh(acc)
    assert acc.plan_tier == "premium"


@pytest.mark.asyncio
async def test_stripe_api_failure_returns_400_and_does_not_update_tier(db, monkeypatch):
    """If the live Stripe API call fails, return 400 (retryable) — do not apply stale event data.

    A stale ``active`` event body must not restore Premium if the subscription
    is actually canceled.  When the live lookup fails, the handler returns 400
    so Stripe retries later, leaving the account tier unchanged.
    """
    from app.billing import stripe_webhooks as billing_module
    from app.billing.stripe_webhooks import _LOOKUP_FAILED

    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    acc = Account(
        github_installation_id=640,
        org_login="api-fail-org",
        plan_tier="free",
    )
    db.add(acc)
    await db.commit()

    # Patch live lookup to simulate a Stripe API failure.
    async def _live_fail(_sid):
        return _LOOKUP_FAILED

    monkeypatch.setattr(billing_module, "_fetch_live_subscription_status", _live_fail)

    body = stripe_event(
        "customer.subscription.updated",
        event_id="evt_api_fail",
        metadata={"org_login": "api-fail-org"},
        id="sub_api_fail",
        status="active",  # stale event body — should NOT be trusted
    )
    with pytest.raises(HTTPException) as exc:
        await stripe_webhook(
            make_request(body, sign(SECRET, body, int(time.time()))), db=db
        )
    assert exc.value.status_code == 400

    # Account tier must be unchanged — we did not apply the stale event data.
    await db.refresh(acc)
    assert acc.plan_tier == "free"

    # Event must NOT have been claimed — Stripe must be able to retry it.
    from sqlalchemy import select as sa_select

    from app.db.models import StripeEvent
    claimed = await db.scalar(
        sa_select(StripeEvent.id).where(StripeEvent.id == "evt_api_fail")
    )
    assert claimed is None


@pytest.mark.asyncio
async def test_stripe_api_failure_on_deletion_returns_400(db, monkeypatch):
    """A Stripe API failure during subscription.deleted must return 400, not downgrade the account."""
    from app.billing import stripe_webhooks as billing_module
    from app.billing.stripe_webhooks import _LOOKUP_FAILED

    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    acc = Account(
        github_installation_id=641,
        org_login="del-fail-org",
        plan_tier="premium",
    )
    db.add(acc)
    await db.commit()

    async def _live_fail(_sid):
        return _LOOKUP_FAILED

    monkeypatch.setattr(billing_module, "_fetch_live_subscription_status", _live_fail)

    body = stripe_event(
        "customer.subscription.deleted",
        event_id="evt_del_fail",
        metadata={"org_login": "del-fail-org"},
        id="sub_del_fail",
    )
    with pytest.raises(HTTPException) as exc:
        await stripe_webhook(
            make_request(body, sign(SECRET, body, int(time.time()))), db=db
        )
    assert exc.value.status_code == 400

    # Account must remain premium — live status could not be confirmed.
    await db.refresh(acc)
    assert acc.plan_tier == "premium"


@pytest.mark.asyncio
async def test_no_api_key_in_dev_falls_back_to_event_status(db, monkeypatch):
    """In non-production (dev/test), missing Stripe API key falls back to event-embedded status."""
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    monkeypatch.setattr(settings, "stripe_secret_key", None)
    monkeypatch.setattr(settings, "environment", "development")
    acc = Account(
        github_installation_id=642,
        org_login="no-key-org",
        plan_tier="free",
    )
    db.add(acc)
    await db.commit()

    body = stripe_event(
        "customer.subscription.created",
        event_id="evt_no_key",
        metadata={"org_login": "no-key-org"},
        id="sub_no_key",
        status="active",
    )
    result = await stripe_webhook(
        make_request(body, sign(SECRET, body, int(time.time()))), db=db
    )
    assert result == {"status": "success"}
    await db.refresh(acc)
    # In dev/test, event-embedded status is trusted — account upgrades to premium.
    assert acc.plan_tier == "premium"


@pytest.mark.asyncio
async def test_production_without_api_key_returns_500(db, monkeypatch):
    """In production, a missing Stripe API key must fail closed — never trust event-embedded status."""
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    monkeypatch.setattr(settings, "stripe_secret_key", None)
    monkeypatch.setattr(settings, "environment", "production")
    acc = Account(
        github_installation_id=644,
        org_login="prod-no-key-org",
        plan_tier="free",
    )
    db.add(acc)
    await db.commit()

    body = stripe_event(
        "customer.subscription.created",
        event_id="evt_prod_no_key",
        metadata={"org_login": "prod-no-key-org"},
        id="sub_prod_no_key",
        status="active",
    )
    with pytest.raises(HTTPException) as exc:
        await stripe_webhook(
            make_request(body, sign(SECRET, body, int(time.time()))), db=db
        )
    # Must fail closed — 500 tells Stripe to retry; account must not be upgraded.
    assert exc.value.status_code == 500

    await db.refresh(acc)
    assert acc.plan_tier == "free"


@pytest.mark.asyncio
async def test_concurrent_subscription_events_last_write_wins_live_status(db, monkeypatch):
    """Concurrent webhook deliveries for the same account must not leave a stale tier.

    Simulates two events processing concurrently: both fetch the live status,
    but the live status seen by each may differ if a subscription change occurs
    between their lookups.  The row-level lock on the account ensures that
    the second writer always sees the committed state of the first before
    applying its own result — preventing a slower stale write from overwriting
    a faster correct one.

    This test exercises the sequential case (lock enforces ordering) rather
    than true concurrency, which requires a multi-connection setup beyond unit
    tests.  It verifies that the final tier reflects the last live status
    written under lock, not a racing in-memory update.
    """

    from app.billing import stripe_webhooks as billing_module

    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)

    acc = Account(
        github_installation_id=650,
        org_login="concurrent-org",
        plan_tier="free",
    )
    db.add(acc)
    await db.commit()

    call_count = 0

    async def _live_first_active_then_canceled(_sid):
        nonlocal call_count
        call_count += 1
        # First call (Event A, stale active event): live says active.
        # Second call (Event B, deletion event): live says canceled.
        return "active" if call_count == 1 else "canceled"

    monkeypatch.setattr(
        billing_module, "_fetch_live_subscription_status", _live_first_active_then_canceled
    )

    # Event A: stale active event — live lookup returns "active".
    body_a = stripe_event(
        "customer.subscription.updated",
        event_id="evt_concurrent_a",
        metadata={"org_login": "concurrent-org"},
        id="sub_concurrent",
        status="active",
    )
    result_a = await stripe_webhook(
        make_request(body_a, sign(SECRET, body_a, int(time.time()))), db=db
    )
    assert result_a == {"status": "success"}
    await db.refresh(acc)
    assert acc.plan_tier == "premium"

    # Event B: deletion event — live lookup returns "canceled".
    body_b = stripe_event(
        "customer.subscription.deleted",
        event_id="evt_concurrent_b",
        metadata={"org_login": "concurrent-org"},
        id="sub_concurrent",
    )
    result_b = await stripe_webhook(
        make_request(body_b, sign(SECRET, body_b, int(time.time()))), db=db
    )
    assert result_b == {"status": "success"}
    await db.refresh(acc)
    # Event B's live status (canceled) must win — account must be downgraded.
    assert acc.plan_tier == "free"

    # Both events must have been claimed exactly once.
    from sqlalchemy import select as sa_select

    from app.db.models import StripeEvent
    claimed_a = await db.scalar(
        sa_select(StripeEvent.id).where(StripeEvent.id == "evt_concurrent_a")
    )
    assert claimed_a == "evt_concurrent_a"

    claimed_b = await db.scalar(
        sa_select(StripeEvent.id).where(StripeEvent.id == "evt_concurrent_b")
    )
    assert claimed_b == "evt_concurrent_b"


@pytest.mark.asyncio
async def test_subscription_deleted_uses_live_status_before_downgrading(db, monkeypatch):
    """subscription.deleted must verify via live status before downgrading."""
    from app.billing import stripe_webhooks as billing_module

    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    acc = Account(
        github_installation_id=643,
        org_login="del-live-org",
        plan_tier="premium",
    )
    db.add(acc)
    await db.commit()

    # Live status confirms the subscription is actually canceled — downgrade is correct.
    async def _live_canceled(_sid):
        return "canceled"

    monkeypatch.setattr(
        billing_module, "_fetch_live_subscription_status", _live_canceled
    )

    body = stripe_event(
        "customer.subscription.deleted",
        event_id="evt_del_live",
        metadata={"org_login": "del-live-org"},
        id="sub_del_live",
    )
    result = await stripe_webhook(
        make_request(body, sign(SECRET, body, int(time.time()))), db=db
    )
    assert result == {"status": "success"}
    await db.refresh(acc)
    assert acc.plan_tier == "free"
