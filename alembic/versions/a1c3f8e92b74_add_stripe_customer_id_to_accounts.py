
# app/billing/stripe_webhooks.py — Stripe subscription webhook receiver

from __future__ import annotations

import hashlib
import hmac
import json
import time

import stripe as stripe_sdk
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.db.models import Account, StripeEvent
from app.utils.logger import get_logger
from app.utils.settings import settings

log = get_logger("billing.stripe")

router = APIRouter(prefix="/webhooks", tags=["billing"])

# Stripe subscription statuses that grant premium access.
# Only fully active or trialing subscriptions should unlock premium features.
_ACTIVE_STATUSES = frozenset({"active", "trialing"})

# Subscription events must be matched to an account before being claimed.
_SUBSCRIPTION_EVENTS = frozenset({
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
})


def _tier_for_subscription_status(sub_status: str | None) -> str:
    """
    Map a Stripe subscription status to an internal plan tier.

    Only active and trialing subscriptions grant premium access.
    Every other status maps to the free tier.
    """
    if sub_status in _ACTIVE_STATUSES:
        return "premium"

    return "free"


# Sentinel returned when a configured Stripe API lookup fails.
#
# None means no API key configured (dev/test).
# _LOOKUP_FAILED means the live lookup failed and stale event data
# must not be trusted.
_LOOKUP_FAILED = object()


async def _fetch_live_subscription_status(
    subscription_id: str | None,
) -> str | None | object:
    """
    Fetch the current subscription status directly from Stripe.

    Returns:
        str: Live subscription status.
        None: No API key configured (dev/test).
        _LOOKUP_FAILED: Stripe API request failed.
    """
    if not subscription_id:
        return None

    if not settings.stripe_secret_key:
        return None

    try:
        stripe_sdk.api_key = settings.stripe_secret_key

        sub = await stripe_sdk.Subscription.retrieve_async(
            subscription_id
        )

        return sub.status  # type: ignore[attr-defined]

    except Exception as exc:
        log.warning(
            "Could not fetch live subscription status for %s from Stripe: %s — "
            "returning 400 so Stripe retries rather than applying stale event data.",
            subscription_id,
            exc,
        )

        return _LOOKUP_FAILED


def verify_stripe_signature(
    payload_bytes: bytes,
    sig_header: str,
    secret: str,
) -> bool:
    """Verify Stripe webhook signature using HMAC-SHA256."""

    if not secret:
        return not settings.is_production

    if not sig_header:
        return False

    try:
        elements = dict(
            item.split("=", 1)
            for item in sig_header.split(",")
        )

        t = elements.get("t")
        v1 = elements.get("v1")

        if not t or not v1:
            return False

        # Reject old signatures (> 5 minutes).
        if abs(time.time() - int(t)) > 300:
            return False

        signed_payload = f"{t}.".encode() + payload_bytes

        computed_sig = hmac.new(
            secret.encode("utf-8"),
            signed_payload,
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(computed_sig, v1)

    except Exception:
        return False


async def _get_account(
    db: AsyncSession,
    data_obj: dict,
) -> Account | None:
    """
    Resolve account using:
    1. GitHub installation ID.
    2. Organization login.
    3. Stripe customer ID.
    """

    metadata = data_obj.get("metadata", {})

    org_login = (
        metadata.get("org_login")
        or data_obj.get("client_reference_id")
    )

    inst_id = metadata.get("installation_id")
    stripe_customer_id = data_obj.get("customer")

    # 1. Find by GitHub installation ID.
    if inst_id:
        try:
            inst_int = int(inst_id)

            stmt = select(Account).where(
                Account.github_installation_id == inst_int
            )

            res = await db.execute(stmt)
            account = res.scalar_one_or_none()

            if account:
                return account

        except (ValueError, TypeError):
            pass

    # 2. Find by organization login.
    if org_login:
        stmt = select(Account).where(
            Account.org_login == org_login
        )

        res = await db.execute(stmt)
        account = res.scalar_one_or_none()

        if account:
            return account

    # 3. Find by Stripe customer ID.
    if stripe_customer_id:
        stmt = select(Account).where(
            Account.stripe_customer_id == stripe_customer_id
        )

        res = await db.execute(stmt)
        account = res.scalar_one_or_none()

        if account:
            return account

    return None


async def _claim_event(
    db: AsyncSession,
    event_id: str,
    event_type: str,
) -> bool:
    """Prevent duplicate Stripe webhook processing."""

    if not event_id:
        log.warning("Stripe webhook event has no event ID")
        return False

    try:
        async with db.begin_nested():
            db.add(
                StripeEvent(
                    id=event_id,
                    event_type=event_type,
                )
            )

            await db.flush()

    except IntegrityError:
        existing = await db.scalar(
            select(StripeEvent.id).where(
                StripeEvent.id == event_id
            )
        )

        if existing:
            return False

        raise

    return True


@router.post("/stripe")
async def stripe_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Receive and process Stripe subscription webhooks."""

    body = await request.body()

    sig_header = request.headers.get("Stripe-Signature", "")

    # Production must have a webhook signing secret.
    if settings.is_production and not settings.stripe_webhook_secret:
        log.error(
            "STRIPE_WEBHOOK_SECRET is not configured in production"
        )

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stripe webhook endpoint is not properly configured.",
        )

    # Verify webhook signature.
    if not verify_stripe_signature(
        body,
        sig_header,
        settings.stripe_webhook_secret or "",
    ):
        log.warning("Invalid or missing Stripe webhook signature")

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid signature",
        )

    # Parse JSON payload.
    try:
        event = json.loads(body.decode("utf-8"))

    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON payload",
        )

    event_id = event.get("id")
    event_type = event.get("type", "")

    data_obj = (
        event.get("data") or {}
    ).get("object", {})

    log.info(
        "Received Stripe webhook event: %s (%s)",
        event_type,
        event_id or "missing-id",
    )

    # Ignore unsupported events.
    if event_type not in (
        "checkout.session.completed",
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        return {"status": "success"}

    # Resolve account and live subscription status BEFORE claiming.
    # This ensures Stripe can retry events that cannot be safely processed.
    account = None
    live_result = None
    subscription_id: str | None = None

    if event_type in _SUBSCRIPTION_EVENTS:

        account = await _get_account(db, data_obj)

        if not account:
            log.warning(
                "Stripe webhook %s (%s) could not be matched to any account — "
                "returning 400 so Stripe will retry. "
                "This is expected if the subscription event arrived before "
                "checkout.session.completed stored the customer ID.",
                event_type,
                event_id,
            )

            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Account not found for this subscription event; will retry."
                ),
            )

        # Fail closed in production if the Stripe API key is absent.
        # A correctly signed webhook that we cannot verify against live Stripe
        # data must not silently update billing state from potentially stale
        # event-embedded status.
        #
        # In dev/test (non-production), the fallback is intentional.
        if settings.is_production and not settings.stripe_secret_key:
            log.error(
                "STRIPE_SECRET_KEY is not configured in production — "
                "cannot verify live subscription status for %s (%s)",
                event_type,
                event_id,
            )

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    "Stripe API key not configured; "
                    "cannot safely process subscription event."
                ),
            )

        # Fetch live subscription status to guard against out-of-order events.
        subscription_id = data_obj.get("id")

        live_result = await _fetch_live_subscription_status(
            subscription_id
        )

        if live_result is _LOOKUP_FAILED:
            # Do not apply potentially stale event data.
            # Return 400 so Stripe retries.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Stripe API unavailable; could not verify "
                    "subscription status. Will retry."
                ),
            )

    # Claim event only after required validation succeeds.
    claimed = await _claim_event(
        db,
        event_id,
        event_type,
    )

    if not claimed:
        log.info(
            "Ignoring duplicate Stripe webhook event: %s",
            event_id,
        )

        return {"status": "success"}

    # For checkout events, resolve account now.
    if account is None:
        account = await _get_account(db, data_obj)

    # Handle checkout completion.
    if event_type == "checkout.session.completed":

        # Checkout completion alone must not grant premium access.
        # Wait for an active/trialing subscription event.
        if account is None:
            log.warning(
                "checkout.session.completed (%s) could not be matched "
                "to any account — no org_login or installation_id "
                "in event metadata. Ignoring.",
                event_id,
            )

        else:
            stripe_customer_id = data_obj.get("customer")

            # Store Stripe customer ID.
            if stripe_customer_id and not account.stripe_customer_id:
                account.stripe_customer_id = stripe_customer_id

                log.info(
                    "Stored Stripe customer ID %s on Account ID %d (%s)",
                    stripe_customer_id,
                    account.id,
                    account.org_login,
                )

            log.info(
                "checkout.session.completed for Account ID %d (%s) — "
                "awaiting subscription activation before granting premium",
                account.id,
                account.org_login,
            )

    # Handle subscription events.
    elif event_type in _SUBSCRIPTION_EVENTS:

        # live_result is None only when no API key is configured
        # in development/test.
        if live_result is None:
            sub_status = data_obj.get("status")
            status_source = "event"

            log.debug(
                "No Stripe API key configured; using event-embedded "
                "status %r for subscription %s",
                sub_status,
                subscription_id,
            )

        else:
            sub_status = live_result
            status_source = "live"

        # Handle deleted subscriptions.
        if event_type == "customer.subscription.deleted":

            if sub_status not in _ACTIVE_STATUSES:
                account.plan_tier = "free"

                log.info(
                    "Downgraded Account ID %d (%s) to free tier "
                    "(subscription deleted, live_status=%s, source=%s)",
                    account.id,
                    account.org_login,
                    sub_status,
                    status_source,
                )

            else:
                log.info(
                    "Ignoring subscription.deleted for Account ID %d (%s): "
                    "live status is %r — not downgrading.",
                    account.id,
                    account.org_login,
                    sub_status,
                )

        # Handle created and updated subscriptions.
        else:
            new_tier = _tier_for_subscription_status(sub_status)
            old_tier = account.plan_tier

            account.plan_tier = new_tier

            log.info(
                "Account ID %d (%s): subscription %s "
                "(status=%s, source=%s) → plan_tier %s→%s",
                account.id,
                account.org_login,
                event_type.split(".")[-1],
                sub_status,
                status_source,
                old_tier,
                new_tier,
            )

    # Save account changes and claimed event.
    await db.commit()

    return {"status": "success"}
