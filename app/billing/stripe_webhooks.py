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

# Subscription events must be matched to an account before being claimed,
# so that unmatched events return 400 and Stripe retries them rather than
# permanently acknowledging an event we could not act on.
_SUBSCRIPTION_EVENTS = frozenset({
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
})


def _tier_for_subscription_status(sub_status: str | None) -> str:
    """Map a Stripe subscription status to an internal plan tier.

    Only ``active`` and ``trialing`` subscriptions grant premium access.
    Every other status (``incomplete``, ``incomplete_expired``,
    ``past_due``, ``unpaid``, ``canceled``, ``paused``, or unknown)
    maps to the free tier.
    """
    if sub_status in _ACTIVE_STATUSES:
        return "premium"
    return "free"


# Sentinel returned by _fetch_live_subscription_status when the Stripe API key
# is configured but the live lookup fails.  Distinct from None (which means
# "no key configured — dev/test mode") so callers can make the right choice:
#   None        → no key; safe to fall back to event-embedded status
#   _LOOKUP_FAILED → key present but call failed; do NOT fall back to stale
#                    event data — return 400 so Stripe retries
_LOOKUP_FAILED = object()


async def _fetch_live_subscription_status(
    subscription_id: str | None,
) -> str | None | object:
    """Fetch the *current* subscription status directly from Stripe.

    Stripe does not guarantee that webhook events arrive in the order they
    were generated.  Trusting the status embedded in an event body risks
    overwriting a newer (correct) tier with a stale one.  By fetching the
    live subscription we always act on the current source-of-truth.

    Return values:
    - ``str``           — the live status fetched from Stripe (use this).
    - ``None``          — no API key configured (dev/test); caller may fall
                          back to the event-embedded status safely.
    - ``_LOOKUP_FAILED``— key is configured but the API call failed; the
                          caller must NOT fall back to the event-embedded
                          status because it may be stale.  Return 400 so
                          Stripe retries the event later.
    """
    if not subscription_id:
        return None
    if not settings.stripe_secret_key:
        # Dev/test environment — no key configured; caller falls back to event status.
        return None
    try:
        stripe_sdk.api_key = settings.stripe_secret_key
        sub = await stripe_sdk.Subscription.retrieve_async(subscription_id)
        return sub.status  # type: ignore[attr-defined]
    except Exception as exc:
        log.warning(
            "Could not fetch live subscription status for %s from Stripe: %s — "
            "returning 400 so Stripe retries rather than applying stale event data.",
            subscription_id,
            exc,
        )
        return _LOOKUP_FAILED


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
    # Account resolution order:
    # 1. metadata.installation_id  — GitHub installation ID (integer)
    # 2. metadata.org_login or client_reference_id — GitHub org login
    # 3. customer  — Stripe customer ID, stored on the Account at checkout
    #
    # INTEGRATION INVARIANT: whoever creates the Stripe Checkout Session
    # must set account-identifying metadata so that the checkout.session.completed
    # event can be matched here and the Stripe customer ID stored on the Account.
    # Use subscription_data.metadata (NOT session-level metadata) for identifiers
    # that must survive through to customer.subscription.* events, because Stripe
    # does NOT automatically copy session metadata to the resulting Subscription.
    # Recommended: set client_reference_id=org_login on the Checkout Session, and
    # also set subscription_data.metadata={"org_login": org_login} so subscription
    # events carry the identifier directly without relying on the customer-ID fallback.
    metadata = data_obj.get("metadata", {})
    org_login = metadata.get("org_login") or data_obj.get("client_reference_id")
    inst_id = metadata.get("installation_id")
    stripe_customer_id = data_obj.get("customer")

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

    if org_login:
        stmt = select(Account).where(Account.org_login == org_login)
        res = await db.execute(stmt)
        account = res.scalar_one_or_none()
        if account:
            return account

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
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event_id = event.get("id")
    event_type = event.get("type", "")
    data_obj = (event.get("data") or {}).get("object", {})

    log.info(
        "Received Stripe webhook event: %s (%s)",
        event_type,
        event_id or "missing-id",
    )

    if event_type not in (
        "checkout.session.completed",
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        return {"status": "success"}

    # For subscription events, resolve both the account and the live subscription
    # status BEFORE claiming the event.  Returning 400 before claiming ensures
    # Stripe will retry any event we cannot safely act on:
    #   • No account match  → 400 (checkout hasn't stored the customer ID yet)
    #   • Live lookup failed → 400 (stale event data must not update tier)
    # Both checks must precede _claim_event so the event remains retryable.
    account = None
    live_result = None  # set for subscription events below; None = not applicable
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
                detail="Account not found for this subscription event; will retry.",
            )

        # Fail closed in production if the Stripe API key is absent.
        # A correctly signed webhook that we cannot verify against live Stripe
        # data must not silently update billing state from potentially stale
        # event-embedded status.  In dev/test (non-production) the fallback is
        # intentional and safe.
        if settings.is_production and not settings.stripe_secret_key:
            log.error(
                "STRIPE_SECRET_KEY is not configured in production — "
                "cannot verify live subscription status for %s (%s)",
                event_type,
                event_id,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Stripe API key not configured; cannot safely process subscription event.",
            )

        # Fetch the live subscription status from Stripe to guard against
        # out-of-order event delivery.  Stripe explicitly does not guarantee
        # that events arrive in the order they were generated, so the status
        # embedded in this event body may be stale.
        #
        # _fetch_live_subscription_status returns:
        #   str            → live status; use it.
        #   None           → no API key (dev/test); fall back to event-embedded status.
        #   _LOOKUP_FAILED → key configured but call failed; return 400 so Stripe
        #                    retries rather than applying potentially stale data.
        #
        # NOTE: this product does not support multiple concurrent Stripe
        # subscriptions per account.  If the subscription model ever changes,
        # revisit this handler to avoid erroneously revoking Premium when a
        # secondary subscription is deleted while a primary one remains active.
        # Acquire a row-level lock on the account before fetching live status.
        # This serializes concurrent subscription events for the same account:
        # a second concurrent request will block at this line until the first
        # request commits (releasing the lock), then re-read the committed tier
        # before applying its own live-status result.
        #
        # Lock guarantee: PostgreSQL (production) — SELECT ... FOR UPDATE holds
        # the row lock for the duration of the transaction, which covers the
        # live Stripe lookup and the subsequent tier update.  SQLite (tests)
        # ignores FOR UPDATE; sequential correctness is verified by the unit
        # tests but true concurrent serialization requires a multi-connection
        # integration test against the production engine, which is out of scope
        # for this unit suite and tracked as a follow-up task.
        await db.refresh(account, with_for_update=True)

        subscription_id = data_obj.get("id")
        live_result = await _fetch_live_subscription_status(subscription_id)

        if live_result is _LOOKUP_FAILED:
            # API key is configured but the live lookup failed.  Do not apply
            # the event's embedded (potentially stale) status.  Return 400 so
            # Stripe retries after its backoff.  The event is not claimed, so
            # the retry will be processed as a fresh delivery.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Stripe API unavailable; could not verify subscription status. Will retry.",
            )

    claimed = await _claim_event(db, event_id, event_type)
    if not claimed:
        log.info("Ignoring duplicate Stripe webhook event: %s", event_id)
        return {"status": "success"}

    # For checkout events, account not yet resolved; resolve it now.
    if account is None:
        account = await _get_account(db, data_obj)

    if event_type == "checkout.session.completed":
        # Checkout session completed means the customer entered payment
        # details, but the subscription may still be incomplete on Stripe's
        # side.  Do not grant premium here — wait for the subscription event
        # with status=active or status=trialing to establish eligibility.
        # Store the Stripe customer ID so subsequent subscription events can
        # be matched even if subscription metadata is missing.
        if account is None:
            log.warning(
                "checkout.session.completed (%s) could not be matched to any account — "
                "no org_login or installation_id in event metadata. Ignoring.",
                event_id,
            )
        else:
            stripe_customer_id = data_obj.get("customer")
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

    elif event_type in _SUBSCRIPTION_EVENTS:
        # live_result was resolved before claiming (above).
        # live_result is None here only when no API key is configured (dev/test).
        # _LOOKUP_FAILED was already handled above and never reaches this point.

        if live_result is None:
            # No API key — dev/test environment.  Fall back to event-embedded status.
            sub_status = data_obj.get("status")
            status_source = "event"
            log.debug(
                "No Stripe API key configured; using event-embedded status %r for subscription %s",
                sub_status,
                subscription_id,
            )
        else:
            sub_status = live_result
            status_source = "live"

        if event_type == "customer.subscription.deleted":
            # For deletion, trust the live status: if the subscription is truly
            # gone or canceled, downgrade.  If the live status is somehow active
            # (e.g. the deletion event is a phantom or the sub was recreated),
            # do not downgrade — let the live status decide.
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
        else:
            new_tier = _tier_for_subscription_status(sub_status)
            old_tier = account.plan_tier
            account.plan_tier = new_tier
            log.info(
                "Account ID %d (%s): subscription %s (status=%s, source=%s) → plan_tier %s→%s",
                account.id,
                account.org_login,
                event_type.split(".")[-1],
                sub_status,
                status_source,
                old_tier,
                new_tier,
            )

    await db.commit()

    return {"status": "success"}