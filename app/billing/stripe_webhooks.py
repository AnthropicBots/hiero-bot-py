# app/billing/stripe_webhooks.py — Stripe subscription webhook receiver

from __future__ import annotations

import hashlib
import hmac
import json
import time

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.gating import tier_for_subscription_status
from app.db.database import get_db
from app.db.models import Account, StripeEvent
from app.utils.logger import get_logger
from app.utils.settings import settings

log = get_logger("billing.stripe")
router = APIRouter(prefix="/webhooks", tags=["billing"])


def verify_stripe_signature(payload_bytes: bytes, sig_header: str, secret: str) -> bool:
    if not secret:
        return not settings.is_production
    if not sig_header:
        return False

    try:
        elements = dict(item.split("=", 1) for item in sig_header.split(","))
        t = elements.get("t")
        v1 = elements.get("v1")

        if not t or not v1:
            return False

        # Reject old signatures (> 5 minutes)
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
    """Resolve an account by subscription association or checkout metadata."""
    subscription_id = data_obj.get("id")

    if isinstance(subscription_id, str) and subscription_id:
        result = await db.execute(
            select(Account).where(
                Account.stripe_subscription_id == subscription_id
            )
        )
        account = result.scalar_one_or_none()
        if account is not None:
            return account

    metadata = data_obj.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    org_login = metadata.get("org_login") or data_obj.get(
        "client_reference_id"
    )
    installation_id = metadata.get("installation_id")

    if installation_id is not None:
        try:
            installation_id_int = int(installation_id)
        except (ValueError, TypeError):
            installation_id_int = None

        if installation_id_int is not None:
            result = await db.execute(
                select(Account).where(
                    Account.github_installation_id == installation_id_int
                )
            )
            account = result.scalar_one_or_none()
            if account is not None:
                return account

    if isinstance(org_login, str) and org_login:
        result = await db.execute(
            select(Account).where(Account.org_login == org_login)
        )
        return result.scalar_one_or_none()

    return None


async def _claim_event(
    db: AsyncSession,
    event_id: str,
    event_type: str,
) -> bool:
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
            select(StripeEvent.id).where(StripeEvent.id == event_id)
        )
        if existing:
            return False
        raise

    return True


def _stripe_event_created(event: dict) -> int | None:
    """Stripe event `created` is a unix timestamp and the ordering key."""
    created = event.get("created")
    if isinstance(created, bool) or not isinstance(created, int) or created < 0:
        return None
    return created


async def _retrieve_subscription_details(
    subscription_id: str,
) -> tuple[str, int]:
    """Retrieve the current status and creation time of a Stripe subscription."""
    if not settings.stripe_secret_key:
        log.error("STRIPE_SECRET_KEY is not configured")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stripe API is not properly configured.",
        )

    try:
        client = stripe.StripeClient(settings.stripe_secret_key)
        subscription = await client.v1.subscriptions.retrieve_async(
            subscription_id
        )
    except Exception as exc:
        log.exception(
            "Unable to retrieve Stripe subscription %s",
            subscription_id,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Unable to verify subscription with Stripe.",
        ) from exc

    subscription_status = getattr(subscription, "status", None)
    created = getattr(subscription, "created", None)

    if (
        not isinstance(subscription_status, str)
        or isinstance(created, bool)
        or not isinstance(created, int)
        or created < 0
    ):
        log.error(
            "Stripe returned invalid subscription details for %s",
            subscription_id,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Stripe returned an invalid subscription response.",
        )

    return subscription_status, created


@router.post("/stripe")
async def stripe_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    body = await request.body()
    sig_header = request.headers.get("Stripe-Signature", "")

    if settings.is_production and not settings.stripe_webhook_secret:
        log.error("STRIPE_WEBHOOK_SECRET is not configured in production")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stripe webhook endpoint is not properly configured.",
        )

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

    try:
        event = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        ) from exc

    if not isinstance(event, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Stripe event payload",
        )

    event_id = event.get("id")
    event_type = event.get("type", "")
    event_data = event.get("data")
    data_obj = (
        event_data.get("object")
        if isinstance(event_data, dict)
        else None
    )

    if not isinstance(event_id, str) or not event_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing Stripe event ID",
        )

    if not isinstance(event_type, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Stripe event type",
        )

    if not isinstance(data_obj, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Stripe event object",
        )

    log.info(
        "Received Stripe webhook event: %s (%s)",
        event_type,
        event_id,
    )

    supported_events = {
        "checkout.session.completed",
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    }

    if event_type not in supported_events:
        return {"status": "success"}

    # Checkout completion alone must never grant Premium.
    # Claiming this event is safe because it does not change entitlement.
    if event_type == "checkout.session.completed":
        claimed = await _claim_event(db, event_id, event_type)
        if not claimed:
            log.info("Ignoring duplicate Stripe webhook event: %s", event_id)
            return {"status": "success"}

        await db.commit()
        return {"status": "success"}

    subscription_id = data_obj.get("id")
    if not isinstance(subscription_id, str) or not subscription_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing Stripe subscription ID",
        )

    # Avoid unnecessary API calls for already processed events.
    existing_event = await db.scalar(
        select(StripeEvent.id).where(StripeEvent.id == event_id)
    )
    if existing_event is not None:
        log.info("Ignoring duplicate Stripe webhook event: %s", event_id)
        return {"status": "success"}

    # Resolve the account before contacting Stripe. Do not claim the event
    # until reconciliation succeeds so failed requests can be retried.
    account = await _get_account(db, data_obj)

    if account is None:
        log.warning(
            "Cannot resolve account for Stripe subscription %s, event %s",
            subscription_id,
            event_id,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Account for this Stripe subscription is not available yet.",
        )

    account_id = account.id
    account_org_login = account.org_login
    initial_subscription_id = account.stripe_subscription_id
    initial_subscription_created = account.stripe_subscription_created
    # End the read transaction before making network requests.
    await db.rollback()

    current_status, incoming_created = await _retrieve_subscription_details(
        subscription_id
    )
    plan_tier = tier_for_subscription_status(current_status)

    # Recover a missing timestamp for the previously associated subscription
    # outside the transaction. Never infer ordering from a missing value.
    recovered_existing_created = None

    if (
        initial_subscription_id
        and initial_subscription_id != subscription_id
        and initial_subscription_created is None
    ):
        _, recovered_existing_created = await _retrieve_subscription_details(
            initial_subscription_id
        )

    # Lock and re-read the account so the replacement decision uses its
    # latest persisted subscription association.
    result = await db.execute(
        select(Account)
        .where(Account.id == account_id)
        .with_for_update()
    )
    account = result.scalar_one_or_none()

    if account is None:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Account is not available yet.",
        )
    event_created = _stripe_event_created(event)
    # Ignore older events for the currently associated subscription.
    # Equal timestamps remain eligible for reconciliation against Stripe.
    if (
        account.stripe_subscription_id == subscription_id
        and event_created is not None
        and account.stripe_status_event_created is not None
        and event_created < account.stripe_status_event_created
    ):
        claimed = await _claim_event(db, event_id, event_type)
        if not claimed:
            await db.rollback()
            return {"status": "success"}

        log.info(
            "Ignoring stale Stripe event %s for subscription %s: "
            "event timestamp %d is older than processed timestamp %d",
            event_id,
            subscription_id,
            event_created,
            account.stripe_status_event_created,
        )
        await db.commit()
        return {"status": "success"}
    # A different subscription can replace the current association only
    # when its immutable Stripe creation timestamp is strictly newer.
    if (
        account.stripe_subscription_id is not None
        and account.stripe_subscription_id != subscription_id
    ):
        existing_created = account.stripe_subscription_created

        if existing_created is None:
            # Use the recovered timestamp only if the subscription
            # association is unchanged from the initial account lookup.
            if (
                account.stripe_subscription_id == initial_subscription_id
                and recovered_existing_created is not None
            ):
                existing_created = recovered_existing_created
                account.stripe_subscription_created = existing_created
            else:
                existing_subscription_id = account.stripe_subscription_id

                log.error(
                    "Cannot order Stripe subscriptions for account %s: "
                    "existing subscription %s has no creation timestamp",
                    account_org_login,
                    existing_subscription_id,
                )
                await db.rollback()
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=(
                        "Current subscription ordering is unavailable; "
                        "retry later."
                    ),
                )

        # Creation timestamps have second-level precision. Equal timestamps
        # cannot establish which subscription should replace the other.
        if incoming_created <= existing_created:
            claimed = await _claim_event(db, event_id, event_type)
            if not claimed:
                await db.rollback()
                return {"status": "success"}

            log.info(
                "Ignoring event %s for subscription %s: it is not newer "
                "than the subscription currently associated with account %s",
                event_id,
                subscription_id,
                account_org_login,
            )
            await db.commit()
            return {"status": "success"}

    # Claim the event only after account resolution and successful
    # reconciliation. The unique Stripe event ID protects against retries.
    claimed = await _claim_event(db, event_id, event_type)
    if not claimed:
        await db.rollback()
        log.info("Ignoring duplicate Stripe webhook event: %s", event_id)
        return {"status": "success"}

    previous_subscription_id = account.stripe_subscription_id
    previous_event_created = account.stripe_status_event_created

    account.stripe_subscription_id = subscription_id
    account.stripe_subscription_created = incoming_created
    account.plan_tier = plan_tier

    if previous_subscription_id != subscription_id:
        if event_created is not None:
            # Use the incoming event timestamp for the new subscription.
            account.stripe_status_event_created = event_created
        elif previous_subscription_id is not None:
            # Do not carry the previous subscription's timestamp forward.
            # Without a valid timestamp, stale-event ordering is unavailable.
            account.stripe_status_event_created = None
        # If no subscription was previously associated, preserve the
        # existing timestamp when the incoming event timestamp is invalid.
    elif event_created is not None:
        # Keep event timestamps monotonic for the same subscription.
        account.stripe_status_event_created = (
            event_created
            if previous_event_created is None
            else max(previous_event_created, event_created)
        )

    await db.commit()

    log.info(
        "Reconciled Account ID %d (%s) to %s using Stripe subscription %s",
        account_id,
        account_org_login,
        plan_tier,
        subscription_id,
    )

    return {"status": "success"}
